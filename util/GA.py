import numpy as np
import logging
import joblib

from pymoo.core.problem import ElementwiseProblem
from pymoo.algorithms.soo.nonconvex.ga import GA as PymooGA
from pymoo.operators.crossover.sbx import SBX
from pymoo.operators.mutation.pm import PM
from pymoo.operators.sampling.rnd import IntegerRandomSampling
from pymoo.operators.repair.rounding import RoundingRepair
from pymoo.optimize import minimize
from pymoo.termination import get_termination
from pymoo.core.callback import Callback

LAMBDA = 0.5

logger = logging.getLogger('pbscaler.ga')


class _HistoryCallback(Callback):
    """Record mean and best fitness each generation."""

    def __init__(self):
        super().__init__()
        self.history = []

    def notify(self, algorithm):
        f_vals = algorithm.pop.get("F").flatten()
        # pymoo minimizes, so negate back for logging
        mean_fit = -np.mean(f_vals)
        best_fit = -np.min(f_vals)
        self.history.append((mean_fit, best_fit))


class GA:
    def __init__(self, model_path, n_dim, lb, ub, goal='max', size_pop=50,
                 max_iter=5, prob_cross=0.9, prob_mut=0.01, precision=1,
                 encoding='BG', selectStyle=None, recStyle=None,
                 mutStyle=None, seed=None):

        if seed is not None:
            np.random.seed(seed)
        self.predictor = joblib.load(model_path)

        self.dim = n_dim
        self.lb = lb
        self.ub = ub
        self.size_pop = size_pop
        self.max_iter = max_iter
        self.goal = goal
        self.pc = prob_cross
        self.pm = prob_mut

        self.obj_trace = np.zeros((self.max_iter, 2))

        logger.debug(f'GA_INIT: model_path={model_path}, dim={n_dim}, lb={lb}, ub={ub}')

    def set_env(self, workloads: list, svcs: list, bottlenecks: list, r: dict):
        if len(bottlenecks) != self.dim:
            raise Exception('the action dim must equal the length of bottlencks')
        self.workloads = workloads
        self.svcs = svcs
        self.bottlenecks = bottlenecks
        self.r = r

    def fitness(self, action):
        x = []
        index = 0
        for i in range(len(self.svcs)):
            svc = self.svcs[i]
            if svc in self.bottlenecks:
                x.extend([i, self.workloads[i], action[index]])
                index += 1
            else:
                x.extend([i, self.workloads[i], self.r[svc]])
        x = np.array(x).reshape(1, -1)
        R1 = self.predictor.predict(x).tolist()[0]
        R2 = (1 - (np.sum(action) / np.sum(self.ub)))
        combined = LAMBDA * R1 + (1 - LAMBDA) * R2
        logger.debug(f'GA_FITNESS: action={action}, R1(SLO)={R1:.4f}, R2(cost)={R2:.4f}, combined={combined:.4f}')
        return [combined]

    def evolve(self):
        ga_ref = self

        class _ReplicaProblem(ElementwiseProblem):
            def __init__(self):
                super().__init__(
                    n_var=ga_ref.dim,
                    n_obj=1,
                    xl=np.array(ga_ref.lb, dtype=float),
                    xu=np.array(ga_ref.ub, dtype=float),
                    vtype=int,
                )

            def _evaluate(self, x, out, *args, **kwargs):
                fit = ga_ref.fitness(x.astype(int))[0]
                # pymoo minimizes; negate for maximization
                out["F"] = -fit if ga_ref.goal == 'max' else fit

        problem = _ReplicaProblem()

        algorithm = PymooGA(
            pop_size=self.size_pop,
            sampling=IntegerRandomSampling(),
            crossover=SBX(prob=self.pc, eta=3.0, vtype=float, repair=RoundingRepair()),
            mutation=PM(prob=self.pm, eta=3.0, vtype=float, repair=RoundingRepair()),
            eliminate_duplicates=True,
        )

        callback = _HistoryCallback()

        result = minimize(
            problem,
            algorithm,
            termination=get_termination("n_gen", self.max_iter),
            seed=None,
            callback=callback,
            verbose=False,
        )

        # Build obj_trace from callback history
        for gen, (mean_f, best_f) in enumerate(callback.history):
            if gen < self.max_iter:
                self.obj_trace[gen, 0] = mean_f
                self.obj_trace[gen, 1] = best_f
                logger.info(f'GA_EVOLVE: gen={gen}, mean_fitness={mean_f:.4f}, best_fitness={best_f:.4f}')

        res = result.X.astype(int).tolist()
        best_gen = int(np.argmax(self.obj_trace[:, 1]))
        best_fitness = self.obj_trace[best_gen, 1]

        logger.info(f'GA_EVOLVE: Final — best_gen={best_gen}, best_fitness={best_fitness:.4f}, solution={res}')

        return res
