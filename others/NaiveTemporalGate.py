"""NaiveTemporalGate baseline (Cap_3:226-228).

Wraps PBScaler with a temporal cooldown that blocks new scale-up actions
on service S_i for T_cold,i seconds after the previous scale-up on S_i.
Scale-down is not blocked; that is the differential value that
PBScaler-k_eff explicitly handles via its anti-scale-down mechanism
(Cap_3:228).
"""

from __future__ import annotations

import logging
import time

from config.Config import Config
from PBScaler import PBScaler

logger = logging.getLogger('pbscaler')


class NaiveTemporalGate(PBScaler):
    """PBScaler subclass that gates scale-up by a per-service cooldown.

    Inherits the full PBScaler pipeline (anomaly detection, root-cause
    analysis via TopoRank, GA optimization). Only `execute_task()` is
    overridden so the gate intercepts the final apply step.

    Cooldown values come from `config.temporal_gate_cold_times` (loaded
    from `temporal_gate.cold_times` in config.yaml). Services not listed
    have cooldown 0 (never blocked).
    """

    def __init__(self, config: Config) -> None:
        super().__init__(config, config.simulation_model)
        self._t_cold: dict[str, int] = config.temporal_gate_cold_times
        self._last_scale_up: dict[str, float] = {}
        logger.info(
            f'INIT: NaiveTemporalGate active with cold_times for '
            f'{len(self._t_cold)} services'
        )

    def execute_task(self, actions: dict[str, int]) -> None:
        """Apply scaling actions, skipping scale-ups still in cooldown."""
        now = time.time()
        for ms in self.mss:
            before_raw = self.svc_counts.get(ms, 0)
            try:
                before = int(before_raw)
            except (TypeError, ValueError):
                before = 0
            after = int(actions[ms])

            if after > before:
                t_cold = self._t_cold.get(ms, 0)
                last = self._last_scale_up.get(ms, 0.0)
                elapsed = now - last if last else float('inf')
                if elapsed < t_cold:
                    logger.info(
                        f'GATE: blocked scale-up {ms} {before} -> {after} '
                        f'(elapsed={elapsed:.1f}s < t_cold={t_cold}s)'
                    )
                    continue
                self._last_scale_up[ms] = now

            self.k8s_util.patch_scale(ms, after)
            logger.info(f'SCALE: {ms} {before_raw} -> {after}')
