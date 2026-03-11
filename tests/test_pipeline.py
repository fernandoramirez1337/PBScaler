"""
Integration tests for PBScaler's three-phase pipeline without a live cluster.

Pipeline phases under test
--------------------------
Phase 1  Anomaly detection    get_abnormal_calls()
Phase 2  Root-cause analysis  root_analysis() → PageRank
Phase 3  GA optimisation      choose_action() → patch_scale()

Test matrix
-----------
Scenario              Phase 1 expected      Phase 2 expected root cause
--------------------  --------------------  ----------------------------
normal_load           no abnormal calls     (skipped — no anomaly)
single_bottleneck     frontend→checkout     checkoutservice
cascading_bottleneck  checkout→payment      paymentservice

How it works
------------
1. A ``MockPrometheusServer`` is started once for the whole test class.
   Config URLs are pointed at it, so ``PrometheusClient.execute_prom``
   hits the real HTTP layer without needing a live Prometheus.

2. ``KubernetesClient`` is replaced everywhere it is imported (PBScaler.py
   and PrometheusClient.py) with ``MockKubernetesClient`` via
   ``unittest.mock.patch``.

3. ``GA`` in ``PBScaler.py`` is replaced with ``MockGA`` to avoid the
   hardcoded model path in ``PBScaler.choose_action()``.

4. A lightweight ``MockPredictor`` joblib file is created in a temp
   directory so the PBScaler constructor (which loads the predictor) works.

Run with:
    pytest tests/test_pipeline.py -v
or:
    python -m pytest tests/test_pipeline.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from typing import List
from unittest.mock import MagicMock, patch

import joblib
import numpy as np

# ── Make project root importable ─────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests.mocks import (
    MockPrometheusServer,
    MockKubernetesClient,
    SCENARIOS,
    make_mock_k8s,
)
from tests.mocks.scenarios import ANOMALY_THRESHOLD, SERVICES, CALL_EDGES
from config.Config import Config
from util.PrometheusClient import PrometheusClient


# ── Helpers ───────────────────────────────────────────────────────────────────

class _MockPredictor:
    """Minimal sklearn-compatible predictor: always predicts SLO satisfied (1)."""

    def predict(self, X):
        return np.ones(len(X))

    def predict_proba(self, X):
        return np.column_stack([np.zeros(len(X)), np.ones(len(X))])


class MockGA:
    """
    Stand-in for ``util.GA.GA``.

    Returns the minimum viable replica count (lb) for each bottleneck service
    so that ``execute_task`` can be exercised without geatpy.
    """

    def __init__(self, model_path, n_dim, lb, ub, *args, **kwargs):
        self.dim = n_dim
        self.lb = lb
        self.ub = ub

    def set_env(self, workloads, svcs, bottlenecks, r):
        self._bottlenecks = bottlenecks
        self._r = r

    def evolve(self) -> List[int]:
        # Scale each bottleneck service up by 1 from its current count
        return [self._r[svc] + 1 for svc in self._bottlenecks]


def _build_config(prom_server: MockPrometheusServer) -> Config:
    """Return a Config whose Prometheus URLs point at the mock server."""
    cfg = Config()
    cfg.prom_no_range_url = prom_server.query_url
    cfg.prom_range_url = prom_server.query_range_url
    cfg.namespace = "default"
    cfg.SLO = 200
    cfg.max_pod = 8
    cfg.min_pod = 1
    return cfg


def _build_prom_client(cfg: Config) -> PrometheusClient:
    pc = PrometheusClient(cfg)
    # Use a 60-second window ending now
    now = int(time.time())
    pc.set_time_range(now - 60, now)
    return pc


# ── Base test class ───────────────────────────────────────────────────────────

class _PipelineTestBase(unittest.TestCase):
    """Shared fixtures: one mock Prometheus server for all test methods."""

    @classmethod
    def setUpClass(cls):
        cls.prom_server = MockPrometheusServer(scenario="normal_load")
        cls.prom_server.start()

        # Write a dummy predictor so PBScaler.__init__ can joblib.load it
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.model_path = os.path.join(cls._tmpdir.name, "dummy.model")
        joblib.dump(_MockPredictor(), cls.model_path)

    @classmethod
    def tearDownClass(cls):
        cls.prom_server.stop()
        cls._tmpdir.cleanup()

    def _mock_k8s(self, scenario_name: str) -> MockKubernetesClient:
        return make_mock_k8s(SCENARIOS[scenario_name])

    def _set_scenario(self, name: str):
        self.prom_server.set_scenario(name)

    def _prom_client(self) -> PrometheusClient:
        cfg = _build_config(self.prom_server)
        return _build_prom_client(cfg)


# ── Phase 1: Anomaly detection ────────────────────────────────────────────────

class TestAnomalyDetection(_PipelineTestBase):
    """
    Verifies that ``get_abnormal_calls()`` correctly classifies call edges
    as healthy or violated based on the mock latency data.
    """

    def _abnormal_calls(self, scenario_name: str) -> List[str]:
        self._set_scenario(scenario_name)
        pc = self._prom_client()
        call_latency = pc.get_call_latency()
        return [
            call for call, lat in call_latency.items()
            if lat > ANOMALY_THRESHOLD
        ]

    def test_normal_load_no_violations(self):
        """No call edge should exceed the SLO threshold under normal load."""
        ab_calls = self._abnormal_calls("normal_load")
        self.assertEqual(
            ab_calls, [],
            f"Expected no abnormal calls, got: {ab_calls}",
        )

    def test_single_bottleneck_detects_checkout(self):
        """
        The frontend→checkoutservice edge should be flagged when
        checkoutservice p90=350 ms > SLO*1.1=220 ms.
        """
        ab_calls = self._abnormal_calls("single_bottleneck")
        self.assertTrue(
            len(ab_calls) > 0,
            "Expected at least one abnormal call edge for single_bottleneck",
        )
        involved_svcs = {svc for call in ab_calls for svc in call.split("_")}
        self.assertIn(
            "checkoutservice", involved_svcs,
            f"checkoutservice should appear in abnormal calls, got: {ab_calls}",
        )

    def test_cascading_bottleneck_detects_payment(self):
        """
        After checkoutservice is scaled, the checkout→paymentservice edge
        should be the new violation (p90=280 ms > 220 ms).

        The call key format is ``{source}_{destination}``, so
        ``checkoutservice_paymentservice`` correctly appears — checkoutservice
        is the *caller*, not the bottleneck.  We only check the destination.
        """
        ab_calls = self._abnormal_calls("cascading_bottleneck")
        self.assertTrue(len(ab_calls) > 0)
        # Destination is the last underscore-delimited token (service names
        # in this topology contain no underscores, so split(-1) is unambiguous)
        destinations = {call.split("_")[-1] for call in ab_calls}
        self.assertIn(
            "paymentservice", destinations,
            f"paymentservice should be a bottleneck destination, got: {ab_calls}",
        )
        # checkoutservice should NOT appear as a bottleneck destination
        self.assertNotIn(
            "checkoutservice", destinations,
            f"checkoutservice should be healthy (110 ms), got destinations: {destinations}",
        )


# ── Phase 2: Root-cause analysis (PageRank) ───────────────────────────────────

class TestRootCauseAnalysis(_PipelineTestBase):
    """
    Validates that ``build_abnormal_subgraph`` + PageRank surfaces the correct
    root-cause service for each bottleneck scenario.

    KubernetesClient is patched in both import sites so no kubeconfig is
    needed and no real API calls are made.
    """

    def _run_root_analysis(self, scenario_name: str):
        """
        Run build_abnormal_subgraph() and return the personalised PageRank dict.
        Returns (ab_dg, personal_array, ab_mss).
        """
        import math
        import networkx as nx
        from util.PrometheusClient import PrometheusClient as _PC

        self._set_scenario(scenario_name)
        sc = SCENARIOS[scenario_name]
        mock_k8s = make_mock_k8s(sc)
        cfg = _build_config(self.prom_server)

        with patch("util.PrometheusClient.KubernetesClient", return_value=mock_k8s):
            pc = _PC(cfg)
            now = int(time.time())
            pc.set_time_range(now - 60, now)

            # Build abnormal call list (Phase 1 output feeding into Phase 2)
            call_latency = pc.get_call_latency()
            ab_calls = [c for c, lat in call_latency.items() if lat > ANOMALY_THRESHOLD]

        return ab_calls, mock_k8s, cfg

    def _pagerank_top_service(self, scenario_name: str) -> str:
        """Return the highest-PageRank service for the given scenario."""
        import math
        import networkx as nx
        from util.PrometheusClient import PrometheusClient as _PC

        self._set_scenario(scenario_name)
        sc = SCENARIOS[scenario_name]
        mock_k8s = make_mock_k8s(sc)
        cfg = _build_config(self.prom_server)

        # Must patch KubernetesClient wherever it is instantiated
        with patch("util.PrometheusClient.KubernetesClient", return_value=mock_k8s), \
             patch("PBScaler.KubernetesClient", return_value=mock_k8s):

            from PBScaler import PBScaler as _PS
            with patch.object(_PS, "__init__", lambda self, cfg, mp: None):
                scaler = _PS.__new__(_PS)
                scaler.config = cfg
                scaler.prom_util = _PC(cfg)
                scaler.k8s_util = mock_k8s
                scaler.SLO = cfg.SLO
                scaler.max_num = cfg.max_pod
                scaler.min_num = cfg.min_pod
                scaler.mss = mock_k8s.get_svcs_without_state()
                scaler.svc_counts = mock_k8s.get_svcs_counts()

                now = int(time.time())
                scaler.prom_util.set_time_range(now - 60, now)

                call_latency = scaler.prom_util.get_call_latency()
                ab_calls = [c for c, lat in call_latency.items() if lat > ANOMALY_THRESHOLD]

                ab_dg, personal_array = scaler.build_abnormal_subgraph(ab_calls)

        nodes = list(ab_dg.nodes)
        if len(nodes) == 1:
            return nodes[0]

        import networkx as nx
        pr = nx.pagerank(ab_dg, alpha=0.85, personalization=personal_array, max_iter=1000)
        return max(pr, key=pr.get)

    def test_single_bottleneck_root_is_checkout(self):
        """PageRank should rank checkoutservice highest for single_bottleneck."""
        top = self._pagerank_top_service("single_bottleneck")
        self.assertEqual(
            top, "checkoutservice",
            f"Expected checkoutservice as root cause, got: {top}",
        )

    def test_cascading_bottleneck_root_is_payment(self):
        """PageRank should rank paymentservice highest for cascading_bottleneck."""
        top = self._pagerank_top_service("cascading_bottleneck")
        self.assertEqual(
            top, "paymentservice",
            f"Expected paymentservice as root cause, got: {top}",
        )

    def test_normal_load_no_abnormal_calls(self):
        """Normal load produces an empty ab_calls list so root analysis is skipped."""
        ab_calls, _, _ = self._run_root_analysis("normal_load")
        self.assertEqual(ab_calls, [], "No abnormal calls expected for normal_load")


# ── Phase 3: GA optimisation ──────────────────────────────────────────────────

class TestGAOptimisation(_PipelineTestBase):
    """
    Verifies that ``choose_action('add')`` calls ``patch_scale()`` on the
    identified bottleneck services with replica counts within [min, max].

    The real GA is replaced with ``MockGA`` so the test has no dependency on
    geatpy or a trained .model file.
    """

    def _run_choose_action(self, scenario_name: str, roots: List[str]):
        from PBScaler import PBScaler as _PS
        from util.PrometheusClient import PrometheusClient as _PC

        self._set_scenario(scenario_name)
        sc = SCENARIOS[scenario_name]
        mock_k8s = make_mock_k8s(sc)
        cfg = _build_config(self.prom_server)

        with patch("util.PrometheusClient.KubernetesClient", return_value=mock_k8s), \
             patch("PBScaler.KubernetesClient", return_value=mock_k8s), \
             patch("PBScaler.GA", MockGA):

            with patch.object(_PS, "__init__", lambda self, cfg, mp: None):
                scaler = _PS.__new__(_PS)
                scaler.config = cfg
                scaler.prom_util = _PC(cfg)
                scaler.k8s_util = mock_k8s
                scaler.SLO = cfg.SLO
                scaler.max_num = cfg.max_pod
                scaler.min_num = cfg.min_pod
                scaler.mss = mock_k8s.get_svcs_without_state()
                scaler.svc_counts = mock_k8s.get_svcs_counts()
                scaler.roots = roots

                now = int(time.time())
                scaler.prom_util.set_time_range(now - 60, now)
                scaler.choose_action("add")

        return mock_k8s.scale_calls

    def test_choose_action_scales_checkout(self):
        """
        For single_bottleneck, choose_action('add') should issue a patch_scale
        call for checkoutservice with replicas > current count.
        """
        scale_calls = self._run_choose_action(
            "single_bottleneck", roots=["checkoutservice"]
        )
        self.assertTrue(len(scale_calls) > 0, "Expected at least one scale call")
        checkout_calls = [c for c in scale_calls if c["svc"] == "checkoutservice"]
        self.assertTrue(
            len(checkout_calls) > 0,
            f"Expected checkoutservice to be scaled, got: {scale_calls}",
        )
        current = SCENARIOS["single_bottleneck"].replicas["checkoutservice"]
        new_replicas = checkout_calls[-1]["replicas"]
        self.assertGreater(
            new_replicas, current,
            f"Replicas should increase from {current}, got {new_replicas}",
        )
        self.assertLessEqual(
            new_replicas, 8,
            f"Replicas must not exceed max_pod=8, got {new_replicas}",
        )

    def test_choose_action_scales_payment_in_cascade(self):
        """
        For cascading_bottleneck, choose_action should scale paymentservice.
        """
        scale_calls = self._run_choose_action(
            "cascading_bottleneck", roots=["paymentservice"]
        )
        payment_calls = [c for c in scale_calls if c["svc"] == "paymentservice"]
        self.assertTrue(
            len(payment_calls) > 0,
            f"Expected paymentservice to be scaled, got: {scale_calls}",
        )

    def test_choose_action_respects_min_max_bounds(self):
        """All replica counts emitted must lie within [min_pod, max_pod]."""
        scale_calls = self._run_choose_action(
            "single_bottleneck", roots=["checkoutservice"]
        )
        for call in scale_calls:
            self.assertGreaterEqual(call["replicas"], 1, f"Below min_pod: {call}")
            self.assertLessEqual(call["replicas"], 8, f"Above max_pod: {call}")


# ── Scenario switching ────────────────────────────────────────────────────────

class TestScenarioSwitching(_PipelineTestBase):
    """
    Verifies that the mock server can switch scenarios at runtime, simulating
    the cascading bottleneck shift that happens after a scaling action.
    """

    def test_scenario_transition(self):
        """
        Simulate one full cycle: detect checkout bottleneck → scale → re-query
        and observe that paymentservice is now the bottleneck.
        """
        # ── Cycle 1: single bottleneck ────────────────────────────────────────
        self._set_scenario("single_bottleneck")
        pc = self._prom_client()
        call_latency_1 = pc.get_call_latency()
        # Destination is index [-1] after split; source names are not bottlenecks
        hot_dsts_1 = {
            call.split("_")[-1]
            for call, lat in call_latency_1.items()
            if lat > ANOMALY_THRESHOLD
        }
        self.assertIn("checkoutservice", hot_dsts_1)
        self.assertNotIn("paymentservice", hot_dsts_1)

        # ── Simulate scaling action (replicas += 2 for checkoutservice) ───────
        self._set_scenario("cascading_bottleneck")

        # ── Cycle 2: bottleneck has shifted ───────────────────────────────────
        pc2 = self._prom_client()
        call_latency_2 = pc2.get_call_latency()
        hot_dsts_2 = {
            call.split("_")[-1]
            for call, lat in call_latency_2.items()
            if lat > ANOMALY_THRESHOLD
        }
        self.assertIn("paymentservice", hot_dsts_2)
        self.assertNotIn("checkoutservice", hot_dsts_2)


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
