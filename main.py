import os
import time
from config.Config import Config
import warnings
warnings.filterwarnings("ignore")


def initController(name: str, config: Config):
    if name == 'MicroScaler':
        from others.MicroScaler import MicroScaler
        return MicroScaler(config)
    elif name == 'SHOWAR':
        from others.Showar import Showar
        return Showar(config)
    elif name == 'KHPA':
        from others.KHPA import KHPA
        return KHPA(config)
    elif name == 'random':
        from others.RandomController import RandomController
        return RandomController(config)
    elif name == 'PBScaler':
        from PBScaler import PBScaler
        return PBScaler(config, config.simulation_model)
    elif name == 'NaiveTemporalGate':
        from others.NaiveTemporalGate import NaiveTemporalGate
        return NaiveTemporalGate(config)
    elif name == 'PBScaler-keff':
        from others.PBScalerKeff import PBScalerKeff
        return PBScalerKeff(config)
    else:
        raise NotImplementedError()


if __name__ == '__main__':
    config = Config()

    # PBSCALER_CONTROLLER selects the controller without source edits — useful
    # for Sprint 5 batch runs that vary the controller across the same cluster.
    # Default 'PBScaler' preserves prior behaviour.
    controller_name = os.environ.get('PBSCALER_CONTROLLER', 'PBScaler')
    controller = initController(controller_name, config)
    controller.start()

    # collect metrics
    from monitor import MetricCollect
    MetricCollect.collect(config, config.data_dir)
