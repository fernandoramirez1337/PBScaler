import math
import time
import logging
import os
import numpy as np
import schedule
from copy import deepcopy
import networkx as nx
import scipy.stats
import joblib
from config.Config import Config
from util.GA import *
import warnings
from util.KubernetesClient import KubernetesClient
from util.PrometheusClient import PrometheusClient

warnings.filterwarnings("ignore")

logger = logging.getLogger('pbscaler')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

def coast_time(func):
    def fun(*args, **kwargs):
        t = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - t
        logger.debug(f'func {func.__name__} coast time:{elapsed:.8f} s')
        return result
    return fun

CONF = 0.05
# anomaly detection window -- 15 seconds
AB_CHECK_INTERVAL = 15
# waste detection window -- 120 seconds
WASTE_CHECK_INTERVAL = 120
ALPHA = 0.2
BETA = 0.9
K = 2


class PBScaler:
    def __init__(self, config: Config, simulation_model_path: str):
        # the prometheus client && k8s client
        self.config = config
        self.prom_util = PrometheusClient(config)
        self.k8s_util = KubernetesClient(config)
        # simulation environment
        logger.info(f'INIT: Loading model from {simulation_model_path}')
        model_exists = os.path.isfile(simulation_model_path)
        logger.info(f'INIT: Model file exists={model_exists}')
        if model_exists:
            mtime = time.ctime(os.path.getmtime(simulation_model_path))
            logger.info(f'INIT: Model file mtime={mtime}')
        self.predictor = joblib.load(simulation_model_path)
        # args
        self.SLO = config.SLO
        self.max_num = config.max_pod
        self.min_num = config.min_pod
        logger.info(f'INIT: SLO={self.SLO}ms, max_pod={self.max_num}, min_pod={self.min_num}, namespace={config.namespace}')
        # microservices
        self.mss = self.k8s_util.get_svcs_without_state()
        logger.info(f'INIT: Discovered {len(self.mss)} services: {self.mss}')
        self.roots = None
        self.svc_counts = None


    @coast_time
    def anomaly_detect(self):
        """ SLO check
        """
        # get service count
        self.svc_counts = self.k8s_util.get_svcs_counts()
        logger.debug(f'ANOMALY: Current replica counts: {self.svc_counts}')
        ab_calls = self.get_abnormal_calls()
        # abnormal
        if len(ab_calls) > 0:
            logger.info(f'ANOMALY: Detected {len(ab_calls)} abnormal calls, triggering root cause analysis')
            self.root_analysis(ab_calls=ab_calls)
        else:
            logger.debug('ANOMALY: No abnormal calls detected')

    @coast_time
    def waste_detection(self):
        """waste detection
        """
        # check the front SLO
        ab_calls = self.get_abnormal_calls()
        if len(ab_calls) > 0:
            logger.debug('WASTE: Skipping waste detection — abnormal calls present')
            return
        # check qps
        cur_time = int(round(time.time()))
        self.prom_util.set_time_range(cur_time - 60, cur_time)
        now_qps_df = self.prom_util.get_svc_qps_range()

        self.prom_util.set_time_range(cur_time - 300, cur_time - 60)
        old_qps_df = self.prom_util.get_svc_qps_range()

        # select the waste pods
        waste_mss = []

        '''Hypothesis µ testing
        a: now_qps µ
        b: old_qps µ0
        H0: µ >= µ0
        H1: µ < µ0
        '''
        for svc in self.mss:
            if svc in now_qps_df.columns and svc in old_qps_df.columns:
                t, p = scipy.stats.ttest_ind(now_qps_df[svc], old_qps_df[svc] * BETA, equal_var=False)
                flagged = t < 0 and p <= CONF
                logger.debug(f'WASTE: {svc} t_stat={t:.4f} p_value={p:.4f} flagged={flagged}')
                if flagged:
                    waste_mss.append(svc)
        logger.info(f'WASTE: Waste candidates (before min_pod filter): {waste_mss}')
        self.roots = list(filter(lambda ms: self.svc_counts[ms] > self.min_num, waste_mss))
        logger.info(f'WASTE: Waste roots (after min_pod filter): {self.roots}')
        if len(self.roots) != 0:
            self.choose_action(option='reduce')

    def get_abnormal_calls(self):
        # get all call latency last 1min
        begin = int(round((time.time() - 60)))
        end = int(round(time.time()))
        self.prom_util.set_time_range(begin, end)
        # slo Hypothesis testing
        ab_calls = []
        call_latency = self.prom_util.get_call_latency()
        threshold = self.SLO * (1 + ALPHA / 2)
        logger.debug(f'ANOMALY: Retrieved {len(call_latency)} call edges, threshold={threshold:.1f}ms')
        for call, latency in call_latency.items():
            is_abnormal = latency > threshold
            logger.debug(f'ANOMALY:   {call}: latency={latency:.1f}ms abnormal={is_abnormal}')
            if is_abnormal:
                ab_calls.append(call)
        logger.info(f'ANOMALY: {len(ab_calls)} abnormal calls out of {len(call_latency)} total')
        return ab_calls

    @coast_time
    def root_analysis(self, ab_calls):
        """ locate the root cause
        1. build the abnormal subgraph
        2. calculate pr score
        3. sort and return the root cause
        Args:
            :param ab_calls: abnormal call edge
            :param n: top n root causes
        """
        ab_dg, personal_array = self.build_abnormal_subgraph(ab_calls)
        nodes = [node for node in ab_dg.nodes]
        edges_info = [(u, v, d.get('weight', 0)) for u, v, d in ab_dg.edges(data=True)]
        logger.info(f'PAGERANK: Abnormal subgraph — nodes={nodes}, edges={edges_info}')
        logger.info(f'PAGERANK: Topology potential (personalization): {personal_array}')
        if len(nodes) == 1:
            res = [(nodes[0], 1)]
        else:
            res = nx.pagerank(ab_dg, alpha=0.85, personalization=personal_array, max_iter=1000)
            res = sorted(res.items(), key=lambda x: x[1], reverse=True)
        logger.info(f'PAGERANK: Full ranking: {res}')
        res = [ms for ms, _ in res]
        self.roots = list(filter(lambda root: self.svc_counts[root] + 1 < self.max_num, res))[:K]
        logger.info(f'PAGERANK: Selected roots (after max_pod filter, top-K={K}): {self.roots}')
        if len(self.roots) == 0:
            logger.warning('PAGERANK: No roots remain after filtering — all at max_pod or no candidates')
        # trigger the choose_action
        if len(self.roots) != 0:
            self.choose_action('add')

    @coast_time
    def choose_action(self, option='add'):
        """ choose action
        Args:
            :param option: [add, reduce], add or reduce the replica
        """
        mss = deepcopy(self.mss)
        roots = deepcopy(self.roots)
        r = deepcopy(self.svc_counts)
        workloads = []

        dim = len(roots)
        if option == 'add':
            logger.info(f'GA_OPT: Begin scale OUT — roots={roots}, current_replicas={{t: r[t] for t in roots}}')
            min_array, max_array = [r[t]+1 for t in self.roots if r[t] < self.max_num] , [self.max_num] * dim
        elif option == 'reduce':
            logger.info(f'GA_OPT: Begin scale IN — roots={roots}, current_replicas={{t: r[t] for t in roots}}')
            min_array, max_array = [self.min_num] * dim, [r[t] for t in roots if r[t] > self.min_num]
            thesold_array = np.array(max_array) - 1
            min_array = np.maximum(min_array, thesold_array)
        else:
            raise NotImplementedError()
        logger.info(f'GA_OPT: dim={dim}, min_array={min_array}, max_array={max_array}')
        qps = self.prom_util.get_svc_qps()
        for ms in mss:
            if ms + '&qps' in qps.keys():
                workloads.append(qps[ms + '&qps'])
            else:
                workloads.append(0)
        logger.debug(f'GA_OPT: Workloads vector: {workloads}')
        '''
        optimizing with genetic algorithms
        only optimize the root services
        TODO: base the root score
        '''
        opter = GA(self.config.simulation_model, dim, min_array, max_array, 'max', size_pop=50, max_iter=5, prob_cross=0.9, prob_mut=0.01, precision=1, encoding='BG', selectStyle='tour', recStyle='xovdp', mutStyle='mutbin', seed=1)

        opter.set_env(workloads, mss, roots, r)
        res = opter.evolve()

        if hasattr(opter, 'obj_trace'):
            best_gen = np.argmax(opter.obj_trace[:, [1]])
            best_fitness = opter.obj_trace[best_gen, 1]
            logger.info(f'GA_OPT: GA result — proposed replicas={res}, best_fitness={best_fitness:.4f}, best_gen={best_gen}')
        else:
            logger.info(f'GA_OPT: GA result — proposed replicas={res}')

        actions = deepcopy(self.svc_counts)
        for i in range(dim):
            svc = self.roots[i]
            actions[svc] = res[i]

        logger.info(f'GA_OPT: Final actions (all services → target replicas): {actions}')
        self.execute_task(actions)

    def build_abnormal_subgraph(self, ab_calls):
        """
            1. collect metrics for all abnormal services
            2. build the abnormal subgraph with abnormal calls
            3. weight the c by Pearson correlation coefficient
        """
        ab_sets = set()
        for ab_call in ab_calls:
            ab_sets.update(ab_call.split('_'))
        for skip in ('unknown', 'istio-ingressgateway', 'loadgenerator'):
            ab_sets.discard(skip)
        ab_mss = list(ab_sets)
        ab_mss.sort()
        begin = int(round((time.time() - 60)))
        end = int(round(time.time()))
        self.prom_util.set_time_range(begin, end)
        ab_metric_df = self.prom_util.get_svc_metric_range()
        ab_svc_latency_df = self.prom_util.get_svc_p90_latency_range()
        ab_svc_latency_df = ab_svc_latency_df[[col for col in ab_svc_latency_df.columns if col in ab_mss]]

        ab_dg = nx.DiGraph()
        ab_dg.add_nodes_from(ab_mss)
        edges = []
        for ab_call in ab_calls:
            edge = ab_call.split('_')
            if 'unknown' in edge or 'istio-ingressgateway' in edge or 'loadgenerator' in edge:
                continue
            metric_df = ab_metric_df[[col for col in ab_metric_df.columns if col.startswith(edge[1])]]
            edges.append((edge[0], edge[1], self.cal_weight(ab_svc_latency_df[edge[0]], metric_df)))
        ab_dg.add_weighted_edges_from(edges)

        # calculate topology potential
        anomaly_score_map = {}
        for node in ab_mss:
            e_latency_array = ab_svc_latency_df[node]
            ef = e_latency_array[e_latency_array > self.SLO].count()
            anomaly_score_map[node] = ef
        personal_array = self.cal_topology_potential(ab_dg, anomaly_score_map)

        return ab_dg, personal_array

    def cal_weight(self, latency_array, metric_df):
        max_corr = 0
        for col in metric_df.columns:
            temp = abs(metric_df[col].corr(latency_array))
            if temp > max_corr:
                max_corr = temp
        return max_corr

    def cal_topology_potential(self, ab_DG, anomaly_score_map: dict):
        personal_array = {}
        for node in ab_DG.nodes:
            # calculate topological potential
            sigma = 1
            potential = anomaly_score_map[node]
            pre_nodes = ab_DG.predecessors(node)
            for pre_node in pre_nodes:
                potential += (anomaly_score_map[pre_node] * math.exp(-1 * math.pow(1/sigma, 2)))
                for pre2_node in ab_DG.predecessors(pre_node):
                    if pre2_node != node:
                        potential += (anomaly_score_map[pre2_node] * math.exp(-1 * math.pow(2 / sigma, 2)))
            personal_array[node] = potential
        return personal_array

    def execute_task(self, actions):
        for ms in self.mss:
            before = self.svc_counts.get(ms, '?')
            after = int(actions[ms])
            self.k8s_util.patch_scale(ms, after)
            logger.info(f'SCALE: {ms} {before} -> {after}')

    def start(self):
        logger.info("PBScaler is running...")
        schedule.clear()
        schedule.every(AB_CHECK_INTERVAL).seconds.do(self.anomaly_detect)
        schedule.every(WASTE_CHECK_INTERVAL).seconds.do(self.waste_detection)
        time_start = time.time()

        while True:
            time_c = time.time() - time_start
            if time_c > self.config.duration:
                logger.info(f'Duration {self.config.duration}s reached, stopping.')
                schedule.clear()
                break
            schedule.run_pending()
