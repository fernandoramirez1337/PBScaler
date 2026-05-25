"""Unit tests for keff support in util.GA (Cap_3 sec:nivel1, sec:nivel2).

Verifies:
- _keff_enabled toggles correctly based on set_env arguments
- legacy fitness formula is preserved when keff is disabled
- keff-enabled fitness uses ALPHA/BETA/LAMBDA_CSP weights
- csp_max precomputation per Cap_3 eq:csp_max
- ColdStartPenalty per Cap_3 eq:csp (positive delta_k only)
- non-bottleneck feature uses observed k_eff when keff enabled
"""

import os
import sys
import tempfile
import time

import joblib
import numpy as np
import pytest

# Make project root importable when running pytest from the package root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from util.GA import GA, ALPHA, BETA, LAMBDA_CSP, OBJECTIVE_BALANCE


class _ConstantPredictor:
    """Returns a fixed value for any input. Lets tests control R1 exactly."""

    def __init__(self, value: float = 0.5):
        self.value = value

    def predict(self, X):
        return np.full(len(X), self.value)


@pytest.fixture
def predictor_path(tmp_path):
    """Write a ConstantPredictor to a joblib file in tmp_path."""
    path = tmp_path / "predictor.joblib"
    joblib.dump(_ConstantPredictor(0.5), path)
    return str(path)


@pytest.fixture
def ga(predictor_path):
    """A 2-bottleneck GA with bounds [1, 1] -- [5, 5]."""
    return GA(model_path=predictor_path, n_dim=2, lb=[1, 1], ub=[5, 5])


# Reference state used across tests.
WORKLOADS = [10.0, 20.0, 30.0]
SVCS = ["frontend", "checkout", "payment"]
BOTTLENECKS = ["checkout", "payment"]
R_CURRENT = {"frontend": 2, "checkout": 2, "payment": 1}
T_COLD = {"frontend": 2.0, "checkout": 5.0, "payment": 60.0}


class TestKeffToggle:
    def test_disabled_without_keff_args(self, ga):
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT)
        assert ga._keff_enabled() is False
        assert ga.csp_max == 0.0

    def test_disabled_when_only_one_arg(self, ga):
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []})
        assert ga._keff_enabled() is False

    def test_enabled_with_both_args(self, ga):
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []},
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        assert ga._keff_enabled() is True


class TestCspMax:
    def test_csp_max_per_eq_csp_max(self, ga):
        # CSP_max = sum (ub - lb) * T_cold over bottlenecks
        # = (5-1)*5 + (5-1)*60 = 20 + 240 = 260
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []},
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        assert ga.csp_max == pytest.approx(260.0)


class TestColdStartPenalty:
    def test_positive_delta_only(self, ga):
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []},
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        # action = [4, 3]; r = {checkout: 2, payment: 1}
        # delta_checkout = 4-2 = 2, delta_payment = 3-1 = 2
        # CSP = 2*5 + 2*60 = 10 + 120 = 130
        assert ga._cold_start_penalty([4, 3]) == pytest.approx(130.0)

    def test_negative_delta_treated_as_zero(self, ga):
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []},
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        # action = [1, 1]; r = {checkout: 2, payment: 1}
        # delta_checkout = max(0, 1-2) = 0; delta_payment = 0
        assert ga._cold_start_penalty([1, 1]) == pytest.approx(0.0)

    def test_mixed_delta(self, ga):
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []},
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        # action = [5, 1]; only checkout adds replicas
        # CSP = (5-2)*5 + max(0, 1-1)*60 = 15 + 0 = 15
        assert ga._cold_start_penalty([5, 1]) == pytest.approx(15.0)


class TestFitnessLegacy:
    def test_uses_objective_balance(self, ga):
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT)
        # R1 = 0.5 (constant predictor); R2 = 1 - sum(action)/sum(ub) = 1 - 4/10 = 0.6
        # combined = 0.5*0.5 + 0.5*0.6 = 0.25 + 0.30 = 0.55
        result = ga.fitness([2, 2])[0]
        assert result == pytest.approx(OBJECTIVE_BALANCE * 0.5 + (1 - OBJECTIVE_BALANCE) * 0.6)
        assert result == pytest.approx(0.55)


class TestFitnessKeff:
    def test_fitness_with_no_added_replicas_max_reward(self, ga):
        """When action <= current replicas everywhere, CSP=0 -> max keff reward."""
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []},
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        # action = [2, 1] matches current r; delta=0 => CSP_hat=0
        # R1 = 0.5; R2 = 1 - 3/10 = 0.7
        # combined = 0.45*0.5 + 0.45*0.7 + 0.10*(1 - 0) = 0.225 + 0.315 + 0.10 = 0.64
        result = ga.fitness([2, 1])[0]
        expected = ALPHA * 0.5 + BETA * 0.7 + LAMBDA_CSP * 1.0
        assert result == pytest.approx(expected)

    def test_fitness_csp_penalizes_added_warmup_cost(self, ga):
        """Adding replicas to high-T_cold services reduces fitness vs low-T_cold."""
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc={"checkout": [], "payment": []},
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        # Both actions add 3 replicas total but to different services.
        # action_cheap: [5, 1] -> +3 checkout (T=5), CSP = 3*5 = 15
        # action_pricey: [2, 4] -> +3 payment (T=60), CSP = 3*60 = 180
        f_cheap = ga.fitness([5, 1])[0]
        f_pricey = ga.fitness([2, 4])[0]
        assert f_cheap > f_pricey

    def test_non_bottleneck_feature_uses_keff_observed(self, ga, predictor_path):
        """When keff is enabled and pod_states are provided for a non-bottleneck
        service, the corresponding feature vector entry must be the observed k_eff
        rather than the nominal r[svc]."""

        captured: list[np.ndarray] = []

        class _RecordingPredictor:
            def predict(self_inner, X):
                captured.append(np.asarray(X))
                return np.full(len(X), 0.5)

        # Replace the predictor in place.
        ga.predictor = _RecordingPredictor()
        # compute_keff uses time.time() internally; align creation_ts to it
        # so the warmup pod is genuinely young (<T_cold) at evaluation time.
        now = time.time()
        # frontend (non-bottleneck): 2 pods, 1 ready + 1 in warmup at t=1 with
        # T_cold=2 (step curve) -> k_eff = 1.0 + 0 = 1.0 (vs r[frontend]=2).
        pod_states = {
            "frontend": [
                {"ready": True, "creation_ts": now - 100.0},
                {"ready": False, "creation_ts": now - 1.0},
            ],
            "checkout": [],
            "payment": [],
        }
        ga.set_env(WORKLOADS, SVCS, BOTTLENECKS, R_CURRENT,
                   pod_states_by_svc=pod_states,
                   t_cold_by_svc=T_COLD,
                   warmup_curve="step")
        ga.fitness([3, 2])
        x = captured[0].flatten()
        # Feature layout: [svc_idx, qps, k_value] x 3 services.
        # frontend at index 0 -> k_value at position 2.
        # Warmup pod age ~1s < T_cold=2 under step -> contributes 0.
        # Ready pod contributes 1.0. k_eff = 1.0 (vs legacy r[frontend]=2).
        assert x[2] == pytest.approx(1.0)
        assert x[2] != pytest.approx(2.0)
