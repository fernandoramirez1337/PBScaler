"""PBScaler-keff controller (Cap_3 sec:nivel1, sec:nivel2, sec:nivel3, sec:scaledown).

Extends PBScaler with effective-capacity awareness via four modifications:

  Nivel 1 (sec:nivel1)   GA non-bottleneck features use k_eff_i(t) instead of k_i.
  Nivel 2 (sec:nivel2)   GA fitness adds the ColdStartPenalty term (eq:fitness_new).
  Nivel 3 (sec:nivel3)   cal_topology_potential amplifies phi_i by k_i / k_eff_i.
  Anti-SD (sec:scaledown) waste_detection requires k_eff_i - 1 >= min_pod.

Niveles 1 and 2 are wired through the GA set_env extension hook in the
parent; this subclass supplies pod_states, T_cold, and the warmup curve.
Nivel 3 and the anti-scale-down are implemented via overridden methods.
"""

from __future__ import annotations

import logging

from config.Config import Config
from PBScaler import PBScaler
from util.EffectiveCapacity import compute_keff, fetch_pod_states

logger = logging.getLogger('pbscaler.keff')


# Bounds for the Nivel 3 amplifier (Cap_3 eq:toporank_mod).
AMPLIFIER_MAX = 10.0
KEFF_FLOOR = 0.1


class PBScalerKeff(PBScaler):
    def __init__(self, config: Config) -> None:
        super().__init__(config, config.simulation_model)
        self._t_cold: dict[str, float] = dict(config.keff_t_cold)
        self._warmup_curve: str = config.keff_warmup_curve
        # Cap_3 eq:fitness_new weights (config-driven so the lambda sensitivity
        # sweep can vary lambda_csp per run without editing source).
        self._alpha: float = config.keff_alpha
        self._beta: float = config.keff_beta
        self._lambda_csp: float = config.keff_lambda_csp
        # Per-cycle cache of pod states keyed by service.
        self._pod_states: dict[str, list[dict]] = {}
        logger.info(
            f'INIT_KEFF: warmup_curve={self._warmup_curve} '
            f'alpha={config.keff_alpha} beta={config.keff_beta} '
            f'lambda_csp={config.keff_lambda_csp} '
            f'services_with_t_cold={len(self._t_cold)}'
        )

    # ---- Pod state cache ---------------------------------------------------

    def _refresh_pod_states(self) -> None:
        """Refresh pod states for all managed services from the K8s API."""
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

    def _keff_for(self, svc: str) -> float:
        """Current k_eff for a service from the cached pod states."""
        pods = self._pod_states.get(svc, [])
        t_cold = self._t_cold.get(svc, 0.0)
        if t_cold <= 0.0:
            # No T_cold known; treat all pods as ready (k_eff = nominal count).
            return float(self.svc_counts.get(svc, sum(1 for p in pods if p.get('ready'))))
        return compute_keff(pods, t_cold, self._warmup_curve)

    # ---- Decision loop hooks (refresh state before parent runs) ------------

    def anomaly_detect(self):
        # svc_counts is set by the parent inside anomaly_detect; we still
        # need pod_states cached before cal_topology_potential is invoked
        # downstream by root_analysis -> build_abnormal_subgraph.
        self._refresh_pod_states()
        super().anomaly_detect()

    def waste_detection(self):
        self._refresh_pod_states()
        super().waste_detection()

    # ---- Nivel 3: TopoRank amplification (Cap_3 eq:toporank_mod) -----------

    def cal_topology_potential(self, ab_DG, anomaly_score_map):
        base = super().cal_topology_potential(ab_DG, anomaly_score_map)
        amplified: dict = {}
        for node, potential in base.items():
            k_i = self.svc_counts.get(node, 0) if self.svc_counts else 0
            if k_i == 0:
                amplified[node] = potential
                continue
            k_eff = self._keff_for(node)
            amplifier = min(AMPLIFIER_MAX, k_i / max(KEFF_FLOOR, k_eff))
            amplified[node] = potential * amplifier
            if amplifier > 1.01:
                logger.info(
                    f'KEFF_TOPO: {node} k_i={k_i} k_eff={k_eff:.2f} '
                    f'amplifier={amplifier:.2f} phi {potential:.3f} -> {amplified[node]:.3f}'
                )
        return amplified

    # ---- Anti-scale-down prematuro (Cap_3 eq:scaledown) --------------------

    def _filter_waste_candidates(self, waste_mss):
        allowed = []
        for ms in waste_mss:
            k_eff = self._keff_for(ms)
            if k_eff - 1.0 >= float(self.min_num):
                allowed.append(ms)
            else:
                logger.info(
                    f'KEFF_GATE: blocking scale-down of {ms} — '
                    f'k_eff={k_eff:.2f} k_eff-1 < min_pod={self.min_num}'
                )
        return allowed

    # ---- GA wiring: pass keff params (Niveles 1 + 2) -----------------------

    def _ga_extra_set_env_kwargs(self, mss):
        # mss here is the list of services PBScaler is optimizing this cycle.
        # pod_states_by_svc must cover all of them so non-bottleneck features
        # can use k_eff (Cap_3 sec:nivel1).
        return {
            "pod_states_by_svc": {
                svc: self._pod_states.get(svc, []) for svc in mss
            },
            "t_cold_by_svc": dict(self._t_cold),
            "warmup_curve": self._warmup_curve,
            "alpha": self._alpha,
            "beta": self._beta,
            "lambda_csp": self._lambda_csp,
        }
