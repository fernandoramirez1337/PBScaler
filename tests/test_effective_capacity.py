"""Tests for util.EffectiveCapacity (Cap_3 sec:warmup_functions, eq:keff).

Validates the three warmup curves (step, linear, sigmoid) and the
compute_keff aggregation across a service's pods.
"""

import pytest

from util.EffectiveCapacity import (
    compute_keff,
    f_linear,
    f_sigmoid,
    f_step,
)


class TestFStep:
    """Step curve: 0 below T_cold, 1 at or above. Cap_3 eq:fstep."""

    T_COLD = 10.0

    def test_below_threshold_returns_zero(self):
        assert f_step(0.0, self.T_COLD) == 0.0
        assert f_step(5.0, self.T_COLD) == 0.0
        assert f_step(self.T_COLD - 1e-9, self.T_COLD) == 0.0

    def test_at_or_above_threshold_returns_one(self):
        assert f_step(self.T_COLD, self.T_COLD) == 1.0
        assert f_step(self.T_COLD + 5.0, self.T_COLD) == 1.0


class TestFLinear:
    """Linear curve: t/T_cold clamped to [0,1]. Cap_3 eq:flin."""

    T_COLD = 10.0

    def test_boundary_values(self):
        assert f_linear(0.0, self.T_COLD) == 0.0
        assert f_linear(self.T_COLD, self.T_COLD) == 1.0

    def test_midpoint_is_half(self):
        assert f_linear(self.T_COLD / 2, self.T_COLD) == pytest.approx(0.5)

    def test_clamped_above_t_cold(self):
        assert f_linear(self.T_COLD + 10.0, self.T_COLD) == 1.0

    def test_monotonic(self):
        prev = -1.0
        for t in [0.0, 1.0, 2.5, 5.0, 7.5, 9.0, 10.0]:
            v = f_linear(t, self.T_COLD)
            assert v >= prev
            prev = v


class TestFSigmoid:
    """Sigmoid curve: affine-normalized so f(0)=0, f(T_cold)=1. Cap_3 eq:fsig."""

    T_COLD = 10.0

    def test_boundary_values_exact(self):
        # The affine normalization guarantees these exactly (modulo FP error).
        assert f_sigmoid(0.0, self.T_COLD) == pytest.approx(0.0, abs=1e-9)
        assert f_sigmoid(self.T_COLD, self.T_COLD) == pytest.approx(1.0, abs=1e-9)

    def test_midpoint_is_half(self):
        # By symmetry of the canonical sigmoid around its midpoint.
        assert f_sigmoid(self.T_COLD / 2, self.T_COLD) == pytest.approx(0.5, abs=1e-6)

    def test_monotonic(self):
        prev = -1.0
        for t in [0.0, 1.0, 2.5, 5.0, 7.5, 9.0, 10.0]:
            v = f_sigmoid(t, self.T_COLD)
            assert v >= prev
            prev = v

    def test_slow_start_fast_finish_shape(self):
        # Cap_3: slow at start, accelerates toward T_cold/2, then saturates.
        # At T_cold/4 the curve is in the slow region; at 3T_cold/4 in fast.
        assert f_sigmoid(self.T_COLD / 4, self.T_COLD) < 0.5
        assert f_sigmoid(3 * self.T_COLD / 4, self.T_COLD) > 0.5

    def test_clamped_above_t_cold(self):
        # Regression: the unclamped affine sigmoid climbs above 1.0 for
        # t > T_cold (discovered by the k3d dynamic smoke). We clamp so a
        # single replica never contributes more than its nominal capacity.
        assert f_sigmoid(self.T_COLD + 0.1, self.T_COLD) == 1.0
        assert f_sigmoid(self.T_COLD * 5, self.T_COLD) == 1.0

    def test_clamped_below_zero(self):
        # Negative ages (clock skew) clamp to 0 like the other curves.
        assert f_sigmoid(-1.0, self.T_COLD) == 0.0


class TestComputeKeff:
    """compute_keff aggregates per-pod contributions. Cap_3 eq:keff."""

    T_COLD = 10.0
    NOW = 1_000_000.0

    def test_all_ready_returns_integer_count(self):
        pods = [
            {"ready": True, "creation_ts": self.NOW - 100.0},
            {"ready": True, "creation_ts": self.NOW - 50.0},
            {"ready": True, "creation_ts": self.NOW - 30.0},
        ]
        assert compute_keff(pods, self.T_COLD, "step", now=self.NOW) == pytest.approx(3.0)

    def test_all_warmup_step_returns_zero_below_threshold(self):
        pods = [
            {"ready": False, "creation_ts": self.NOW - 1.0},
            {"ready": False, "creation_ts": self.NOW - 2.0},
        ]
        assert compute_keff(pods, self.T_COLD, "step", now=self.NOW) == pytest.approx(0.0)

    def test_all_warmup_linear_fractional(self):
        # t=2 -> 0.2, t=5 -> 0.5; total 0.7
        pods = [
            {"ready": False, "creation_ts": self.NOW - 2.0},
            {"ready": False, "creation_ts": self.NOW - 5.0},
        ]
        assert compute_keff(pods, self.T_COLD, "linear", now=self.NOW) == pytest.approx(0.7)

    def test_mixed_ready_and_warmup(self):
        # 2 ready (=2.0) + 1 warming at t=5 (linear => 0.5) = 2.5
        pods = [
            {"ready": True, "creation_ts": self.NOW - 200.0},
            {"ready": True, "creation_ts": self.NOW - 150.0},
            {"ready": False, "creation_ts": self.NOW - 5.0},
        ]
        assert compute_keff(pods, self.T_COLD, "linear", now=self.NOW) == pytest.approx(2.5)

    def test_empty_pod_list(self):
        assert compute_keff([], self.T_COLD, "step", now=self.NOW) == pytest.approx(0.0)

    def test_invalid_curve_name_raises(self):
        pods = [{"ready": True, "creation_ts": self.NOW}]
        with pytest.raises(ValueError, match="curve"):
            compute_keff(pods, self.T_COLD, "exponential", now=self.NOW)  # type: ignore[arg-type]
