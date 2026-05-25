"""PBScaler-keff controller.

Subclase de PBScaler que inyecta el estado de pods al GA para que la
fitness use k_eff(t) en las features no-bottleneck (Cap_3 sec:nivel1) y
sume el término ColdStartPenalty (Cap_3 sec:nivel2). La modificación al
ranking topológico (sec:nivel3) y el filtro anti scale-down (sec:scaledown)
no están incluidos en esta versión del módulo.
"""

from __future__ import annotations

import logging

from config.Config import Config
from PBScaler import PBScaler
from util.EffectiveCapacity import fetch_pod_states

logger = logging.getLogger('pbscaler.keff')


class PBScalerKeff(PBScaler):
    def __init__(self, config: Config) -> None:
        super().__init__(config, config.simulation_model)
        self._t_cold: dict[str, float] = dict(config.keff_t_cold)
        self._warmup_curve: str = config.keff_warmup_curve
        self._pod_states: dict[str, list[dict]] = {}
        logger.info(
            f'INIT_KEFF: warmup_curve={self._warmup_curve} '
            f'alpha={config.keff_alpha} beta={config.keff_beta} '
            f'lambda_csp={config.keff_lambda_csp} '
            f'services_with_t_cold={len(self._t_cold)}'
        )

    def _refresh_pod_states(self) -> None:
        """Refresca el snapshot de pods de todos los servicios gestionados."""
        states: dict[str, list[dict]] = {}
        for svc in self.mss:
            try:
                states[svc] = fetch_pod_states(
                    self.k8s_util.core_api,
                    self.k8s_util.namespace,
                    svc,
                )
            except Exception:
                logger.exception(f'KEFF: fetch_pod_states failed for {svc}')
                states[svc] = []
        self._pod_states = states

    def anomaly_detect(self):
        # Refrescar antes que el ciclo del padre llegue a choose_action,
        # que lee self._pod_states vía _ga_extra_set_env_kwargs.
        self._refresh_pod_states()
        super().anomaly_detect()

    def _ga_extra_set_env_kwargs(self, mss):
        return {
            "pod_states_by_svc": {
                svc: self._pod_states.get(svc, []) for svc in mss
            },
            "t_cold_by_svc": dict(self._t_cold),
            "warmup_curve": self._warmup_curve,
        }
