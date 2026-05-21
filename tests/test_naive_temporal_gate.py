"""Tests for NaiveTemporalGate baseline (Cap_3:226-228).

Verifies the cooldown gate intercepts scale-up actions during T_cold,i
seconds after a previous scale-up, while leaving scale-down untouched.

These are unit tests for ``execute_task()`` only. The full PBScaler
pipeline (anomaly detection, GA, predictor, Prometheus, K8s) is bypassed
via ``__new__`` instantiation — the gate's logic is independent of all
of those.

Run with:
    pytest tests/test_naive_temporal_gate.py -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class TestNaiveTemporalGate(unittest.TestCase):
    """Unit tests for ``NaiveTemporalGate.execute_task()`` gating logic."""

    def _make_gate(
        self,
        cold_times: dict[str, int],
        svc_counts: dict[str, int],
    ):
        """Construct a NaiveTemporalGate with heavy dependencies bypassed.

        Uses ``__new__`` to skip the PBScaler constructor chain (predictor
        loading, Prometheus client, K8s client). Sets only the attributes
        that ``execute_task`` touches.
        """
        from others.NaiveTemporalGate import NaiveTemporalGate

        gate = NaiveTemporalGate.__new__(NaiveTemporalGate)
        gate._t_cold = cold_times
        gate._last_scale_up = {}
        gate.mss = list(svc_counts.keys())
        gate.svc_counts = dict(svc_counts)
        gate.k8s_util = MagicMock()
        return gate

    @patch("others.NaiveTemporalGate.time.time")
    def test_scale_up_blocked_during_cooldown(self, mock_time):
        """A second scale-up within T_cold must be blocked (not applied)."""
        gate = self._make_gate(
            cold_times={"checkoutservice": 60},
            svc_counts={"checkoutservice": 1},
        )

        mock_time.return_value = 1000.0
        gate.execute_task({"checkoutservice": 2})  # 1 → 2: scale-up allowed

        # 30 s later: cluster now at 2, attempt 2 → 3 (within 60 s window)
        mock_time.return_value = 1030.0
        gate.svc_counts["checkoutservice"] = 2
        gate.execute_task({"checkoutservice": 3})  # should be blocked

        # patch_scale called only once (the first, allowed scale-up)
        self.assertEqual(gate.k8s_util.patch_scale.call_count, 1)
        gate.k8s_util.patch_scale.assert_called_with("checkoutservice", 2)

    @patch("others.NaiveTemporalGate.time.time")
    def test_scale_up_allowed_after_cooldown(self, mock_time):
        """After T_cold has elapsed, a new scale-up must be applied."""
        gate = self._make_gate(
            cold_times={"checkoutservice": 60},
            svc_counts={"checkoutservice": 1},
        )

        mock_time.return_value = 1000.0
        gate.execute_task({"checkoutservice": 2})  # 1 → 2 at t=1000

        # 61 s later: cooldown expired, attempt 2 → 3
        mock_time.return_value = 1061.0
        gate.svc_counts["checkoutservice"] = 2
        gate.execute_task({"checkoutservice": 3})  # should apply

        # Both scale-ups applied
        self.assertEqual(gate.k8s_util.patch_scale.call_count, 2)
        first_args, _ = gate.k8s_util.patch_scale.call_args_list[0]
        second_args, _ = gate.k8s_util.patch_scale.call_args_list[1]
        self.assertEqual(first_args, ("checkoutservice", 2))
        self.assertEqual(second_args, ("checkoutservice", 3))

    @patch("others.NaiveTemporalGate.time.time")
    def test_scale_down_not_blocked(self, mock_time):
        """Scale-down within T_cold of a scale-up must still apply.

        Differential from PBScaler-k_eff: the naive gate covers only
        scale-up cooldown; preventing premature scale-down is the
        explicit add-on of PBScaler-k_eff (Cap_3:228, sec:scaledown).
        """
        gate = self._make_gate(
            cold_times={"checkoutservice": 60},
            svc_counts={"checkoutservice": 1},
        )

        mock_time.return_value = 1000.0
        gate.execute_task({"checkoutservice": 3})  # 1 → 3: scale-up

        # 10 s later (well inside cooldown), scale-down 3 → 1
        mock_time.return_value = 1010.0
        gate.svc_counts["checkoutservice"] = 3
        gate.execute_task({"checkoutservice": 1})  # scale-down: must apply

        self.assertEqual(gate.k8s_util.patch_scale.call_count, 2)
        first_args, _ = gate.k8s_util.patch_scale.call_args_list[0]
        second_args, _ = gate.k8s_util.patch_scale.call_args_list[1]
        self.assertEqual(first_args, ("checkoutservice", 3))
        self.assertEqual(second_args, ("checkoutservice", 1))


if __name__ == "__main__":
    unittest.main()
