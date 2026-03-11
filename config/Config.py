import os
import time
import yaml


def getNowTime():
    return int(round(time.time()))


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Config():
    def __init__(self):
        config_path = os.path.join(_PROJECT_ROOT, 'config.yaml')
        with open(config_path, 'r') as f:
            cfg = yaml.safe_load(f)

        k8s = cfg.get('kubernetes', {})
        prom = cfg.get('prometheus', {})
        auto = cfg.get('autoscaler', {})
        out = cfg.get('output', {})

        self.namespace = os.environ.get('K8S_NAMESPACE', k8s.get('namespace', 'default'))
        self.k8s_config = os.path.expanduser(
            os.environ.get('K8S_CONFIG', k8s.get('kubeconfig', '~/.kube/config'))
        )
        self.k8s_yaml = os.path.expanduser(k8s.get('manifest', ''))

        self.SLO = int(auto.get('slo', 200))
        self.max_pod = int(auto.get('max_pod', 8))
        self.min_pod = int(auto.get('min_pod', 1))
        self.duration = int(auto.get('duration', 1200))

        raw_model = auto.get('simulation_model', '')
        self.simulation_model = (
            raw_model if os.path.isabs(raw_model)
            else os.path.join(_PROJECT_ROOT, raw_model)
        )

        self.prom_range_url = os.environ.get(
            'PROM_RANGE_URL', prom.get('range_url', 'http://localhost:9090/api/v1/query_range')
        )
        self.prom_no_range_url = os.environ.get(
            'PROM_QUERY_URL', prom.get('query_url', 'http://localhost:9090/api/v1/query')
        )
        self.step = int(prom.get('step', 5))

        raw_data_dir = out.get('data_dir', 'output')
        self.data_dir = (
            raw_data_dir if os.path.isabs(raw_data_dir)
            else os.path.join(_PROJECT_ROOT, raw_data_dir)
        )

        self.start = getNowTime()
        self.end = self.start + self.duration
