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
    else:
        raise NotImplementedError()


if __name__ == '__main__':
    config = Config()

    controller = initController('PBScaler', config)
    controller.start()

    # collect metrics
    from monitor import MetricCollect
    MetricCollect.collect(config, config.data_dir)
