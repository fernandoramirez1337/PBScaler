"""Integration tests for PBScalerKeff (Cap_3 sec:nivel3, sec:scaledown).

Verifies the three methods overridden by PBScalerKeff against the parent
PBScaler behavior:

  - cal_topology_potential: Nivel 3 amplifier (eq:toporank_mod)
  - _filter_waste_candidates: anti-scale-down (eq:scaledown)
  - _ga_extra_set_env_kwargs: wires Niveles 1 + 2 into the GA

These are unit tests for the override logic; the full pipeline (Prometheus,
K8s, predictor) is bypassed via __new__ instantiation, mirroring the pattern
used by test_naive_temporal_gate.py.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

import networkx as nx
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _make_keff(
    svc_counts: dict[str, int] | None = None,
    t_cold: dict[str, float] | None = None,
    pod_states: dict[str, list[dict]] | None = None,
    warmup_curve: str = "step",
    min_num: int = 1,
):
    """Construct a PBScalerKeff bypassing the heavy parent constructor."""
    from others.PBScalerKeff import PBScalerKeff

    keff = PBScalerKeff.__new__(PBScalerKeff)
    keff.mss = list((svc_counts or {}).keys())
    keff.svc_counts = dict(svc_counts or {})
    keff.min_num = min_num
    keff._t_cold = dict(t_cold or {})
    keff._warmup_curve = warmup_curve
    keff._pod_states = dict(pod_states or {})
    keff._alpha = 0.45
    keff._beta = 0.45
    keff._lambda_csp = 0.10
    keff.k8s_util = MagicMock()
    return keff


# Helper: pod factory aligned with time.time() so the step curve evaluation
# inside compute_keff sees the expected ages.
def _ready_pod():
    return {"ready": True, "creation_ts": time.time() - 3600.0}


def _warmup_pod(age_seconds: float):
    return {"ready": False, "creation_ts": time.time() - age_seconds}


class TestNivel3Amplifier:
    """Cap_3 eq:toporank_mod — phi_i' = phi_i * min(M, k_i / max(eps, k_eff_i))."""

    def test_phi_amplified_when_warmup(self):
        # checkout has 3 pods: 1 ready + 2 in warmup at t=0.1 with T_cold=10
        # k_eff = 1.0 + 0 + 0 = 1.0 (step curve), k_i = 3 -> amplifier = 3.0
        keff = _make_keff(
            svc_counts={"checkout": 3, "frontend": 1},
            t_cold={"checkout": 10.0, "frontend": 2.0},
            pod_states={
                "checkout": [_ready_pod(), _warmup_pod(0.1), _warmup_pod(0.1)],
                "frontend": [_ready_pod()],
            },
        )
        graph = nx.DiGraph()
        graph.add_node("checkout")
        graph.add_node("frontend")
        base_scores = {"checkout": 5.0, "frontend": 2.0}
        amplified = keff.cal_topology_potential(graph, base_scores)
        assert amplified["checkout"] == pytest.approx(15.0)  # 5.0 * 3
        assert amplified["frontend"] == pytest.approx(2.0)   # unchanged

    def test_phi_unchanged_when_all_ready(self):
        # k_eff = k_i for both services -> amplifier = 1.0
        keff = _make_keff(
            svc_counts={"checkout": 3, "frontend": 2},
            t_cold={"checkout": 10.0, "frontend": 2.0},
            pod_states={
                "checkout": [_ready_pod(), _ready_pod(), _ready_pod()],
                "frontend": [_ready_pod(), _ready_pod()],
            },
        )
        graph = nx.DiGraph()
        graph.add_node("checkout")
        graph.add_node("frontend")
        base_scores = {"checkout": 5.0, "frontend": 2.0}
        amplified = keff.cal_topology_potential(graph, base_scores)
        assert amplified["checkout"] == pytest.approx(5.0)
        assert amplified["frontend"] == pytest.approx(2.0)

    def test_amplifier_capped_at_M(self):
        # k_i = 5, all pods warming -> k_eff = 0 -> max(eps, 0) = 0.1
        # ratio = 5/0.1 = 50, capped at M=10
        keff = _make_keff(
            svc_counts={"checkout": 5},
            t_cold={"checkout": 100.0},
            pod_states={"checkout": [_warmup_pod(0.1)] * 5},
        )
        graph = nx.DiGraph()
        graph.add_node("checkout")
        base_scores = {"checkout": 1.0}
        amplified = keff.cal_topology_potential(graph, base_scores)
        assert amplified["checkout"] == pytest.approx(10.0)

    def test_k_i_zero_no_amplification(self):
        # Defensive case: k_i = 0 shouldn't divide-by-anything; return potential as-is
        keff = _make_keff(
            svc_counts={"checkout": 0},
            t_cold={"checkout": 10.0},
            pod_states={"checkout": []},
        )
        graph = nx.DiGraph()
        graph.add_node("checkout")
        amplified = keff.cal_topology_potential(graph, {"checkout": 4.0})
        assert amplified["checkout"] == pytest.approx(4.0)


class TestAntiScaleDown:
    """Cap_3 eq:scaledown — scale-down allowed iff k_eff_i - 1 >= k_min_i."""

    def test_blocked_when_keff_minus_one_below_min(self):
        # k_eff = 1.0 (1 ready + 1 warming at t<T_cold), min_pod = 1
        # k_eff - 1 = 0 < 1 -> BLOCKED
        keff = _make_keff(
            svc_counts={"checkout": 2},
            t_cold={"checkout": 10.0},
            pod_states={"checkout": [_ready_pod(), _warmup_pod(0.1)]},
            min_num=1,
        )
        assert keff._filter_waste_candidates(["checkout"]) == []

    def test_allowed_when_keff_minus_one_at_or_above_min(self):
        # 3 ready pods, min_pod = 1 -> k_eff = 3.0, k_eff - 1 = 2 >= 1 -> ALLOWED
        keff = _make_keff(
            svc_counts={"checkout": 3},
            t_cold={"checkout": 10.0},
            pod_states={"checkout": [_ready_pod()] * 3},
            min_num=1,
        )
        assert keff._filter_waste_candidates(["checkout"]) == ["checkout"]

    def test_mixed_services_only_eligible_pass(self):
        # checkout has capacity; payment is in warmup
        keff = _make_keff(
            svc_counts={"checkout": 3, "payment": 2},
            t_cold={"checkout": 5.0, "payment": 60.0},
            pod_states={
                "checkout": [_ready_pod()] * 3,
                "payment": [_ready_pod(), _warmup_pod(1.0)],
            },
            min_num=1,
        )
        result = keff._filter_waste_candidates(["checkout", "payment"])
        assert result == ["checkout"]


class TestGaExtraKwargs:
    """Verifies the GA receives the keff params for Niveles 1 + 2 wiring."""

    def test_returns_expected_keys(self):
        keff = _make_keff(
            svc_counts={"checkout": 1},
            t_cold={"checkout": 5.0},
            pod_states={"checkout": [_ready_pod()]},
            warmup_curve="linear",
        )
        kwargs = keff._ga_extra_set_env_kwargs(["checkout"])
        assert set(kwargs.keys()) == {
            "pod_states_by_svc", "t_cold_by_svc", "warmup_curve",
            "alpha", "beta", "lambda_csp",
        }
        # weights flow from config (here the test-helper defaults)
        assert kwargs["lambda_csp"] == 0.10
        assert kwargs["alpha"] == 0.45

    def test_pod_states_filtered_to_mss(self):
        # Even though _pod_states has both, only the requested mss are passed.
        keff = _make_keff(
            svc_counts={"checkout": 1, "payment": 1},
            t_cold={"checkout": 5.0, "payment": 60.0},
            pod_states={
                "checkout": [_ready_pod()],
                "payment": [_ready_pod()],
            },
        )
        kwargs = keff._ga_extra_set_env_kwargs(["checkout"])
        assert list(kwargs["pod_states_by_svc"].keys()) == ["checkout"]

    def test_warmup_curve_propagated(self):
        keff = _make_keff(
            svc_counts={"checkout": 1},
            warmup_curve="sigmoid",
        )
        kwargs = keff._ga_extra_set_env_kwargs(["checkout"])
        assert kwargs["warmup_curve"] == "sigmoid"


if __name__ == "__main__":
    unittest.main()
