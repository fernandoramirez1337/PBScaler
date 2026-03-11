#!/usr/bin/env python3
"""
End-to-end dry-run of PBScaler's single_bottleneck scenario.

Wires PBScaler to the mock Prometheus server and mock K8s API from
tests/mocks/ and validates the three-phase algorithm pipeline:

  Phase 1  Anomaly detection   (t-test threshold check)
  Phase 2  Root-cause analysis  (PageRank on weighted DAG)
  Phase 3  GA scaling proposal  (MockGA → dry-run scaling actions)

Every phase logs its inputs and outputs.  Nothing is actually scaled.

Usage:
    python tests/run_single_bottleneck.py
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from copy import deepcopy
from typing import Dict, List
from unittest.mock import patch

# ── Ensure project root is importable ─────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Apply stubs BEFORE any project import touches kubernetes / geatpy / schedule
from tests.conftest import _stub_kubernetes, _stub_schedule, _stub_geatpy

_stub_kubernetes()
_stub_schedule()
_stub_geatpy()

# ── Project imports (safe now that stubs are in place) ────────────────────────
import networkx as nx
import numpy as np

from config.Config import Config
from tests.mocks import (
    SCENARIOS,
    MockPrometheusServer,
    MockKubernetesClient,
    make_mock_k8s,
)
from tests.mocks.scenarios import ANOMALY_THRESHOLD, SERVICES, CALL_EDGES, SLO_MS
from util.PrometheusClient import PrometheusClient

# ── Constants ─────────────────────────────────────────────────────────────────
SCENARIO = "single_bottleneck"
ALPHA = 0.2
SLO = 200
SEP = "=" * 72


# ── MockGA (same as in test_pipeline.py) ──────────────────────────────────────
class MockGA:
    """Returns current_replicas + 1 for each bottleneck service."""

    def __init__(self, model_path, n_dim, lb, ub, *args, **kwargs):
        self.dim = n_dim
        self.lb = lb
        self.ub = ub

    def set_env(self, workloads, svcs, bottlenecks, r):
        self._bottlenecks = bottlenecks
        self._r = r

    def evolve(self) -> List[int]:
        return [self._r[svc] + 1 for svc in self._bottlenecks]


# ── Logging helpers ───────────────────────────────────────────────────────────

def _header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def _kv(key: str, value, indent: int = 2) -> None:
    pad = " " * indent
    print(f"{pad}{key}: {value}")


def _table(rows: List[tuple], headers: tuple, indent: int = 4) -> None:
    """Print a simple ASCII table."""
    pad = " " * indent
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    fmt = pad + "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(pad + "  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))


# ── Phase 1: Anomaly Detection ───────────────────────────────────────────────

def phase1_anomaly_detection(prom_client: PrometheusClient) -> List[str]:
    """Reproduce get_abnormal_calls() with full logging."""

    _header("PHASE 1 — Anomaly Detection (Threshold Check)")

    threshold = SLO * (1 + ALPHA / 2)
    _kv("SLO", f"{SLO} ms")
    _kv("ALPHA", ALPHA)
    _kv("Anomaly threshold", f"SLO × (1 + α/2) = {threshold} ms")
    print()

    call_latency = prom_client.get_call_latency()

    print("  Call-edge p90 latencies:")
    rows = []
    for call, lat in sorted(call_latency.items()):
        flag = "⚠ ABNORMAL" if lat > threshold else "✓ ok"
        rows.append((call, f"{lat:.1f} ms", flag))
    _table(rows, ("Call Edge", "p90 Latency", "Status"))
    print()

    ab_calls = [call for call, lat in call_latency.items() if lat > threshold]

    _kv("Abnormal calls", ab_calls if ab_calls else "(none)")

    # ── Assertion ─────────────────────────────────────────────────────────────
    involved = {svc for call in ab_calls for svc in call.split("_")}
    ok = "checkoutservice" in involved and len(ab_calls) > 0
    print()
    if ok:
        print("  ✅ Phase 1 PASSED — checkoutservice detected as anomalous")
    else:
        print("  ❌ Phase 1 FAILED — expected checkoutservice in abnormal calls")
    return ab_calls


# ── Phase 2: Root-Cause Analysis (PageRank) ───────────────────────────────────

def phase2_root_cause(ab_calls: List[str], mock_k8s, prom_client: PrometheusClient, cfg: Config):
    """Run build_abnormal_subgraph + PageRank with full logging."""

    _header("PHASE 2 — Root-Cause Analysis (PageRank)")

    print("  Input: abnormal call edges from Phase 1")
    for c in ab_calls:
        src, dst = c.split("_")
        print(f"    {src} → {dst}")
    print()

    # Build PBScaler instance with manual wiring (bypass __init__)
    from PBScaler import PBScaler as _PS

    with patch("util.PrometheusClient.KubernetesClient", return_value=mock_k8s), \
         patch("PBScaler.KubernetesClient", return_value=mock_k8s):

        scaler = _PS.__new__(_PS)
        scaler.config = cfg
        scaler.prom_util = prom_client
        scaler.k8s_util = mock_k8s
        scaler.SLO = cfg.SLO
        scaler.max_num = cfg.max_pod
        scaler.min_num = cfg.min_pod
        scaler.mss = mock_k8s.get_svcs_without_state()
        scaler.svc_counts = mock_k8s.get_svcs_counts()

        ab_dg, personal_array = scaler.build_abnormal_subgraph(ab_calls)

    # ── Log subgraph ──────────────────────────────────────────────────────────
    print("  Abnormal subgraph:")
    _kv("Nodes", list(ab_dg.nodes), indent=4)
    edge_rows = [(u, v, f"{d['weight']:.4f}") for u, v, d in ab_dg.edges(data=True)]
    if edge_rows:
        print("    Edges (Pearson-weighted):")
        _table(edge_rows, ("Source", "Destination", "Weight"), indent=6)
    print()

    # ── Log topology potential ────────────────────────────────────────────────
    print("  Topology potential (personalization vector):")
    for node, pot in sorted(personal_array.items(), key=lambda x: -x[1]):
        _kv(node, f"{pot:.4f}", indent=4)
    print()

    # ── PageRank ──────────────────────────────────────────────────────────────
    nodes = list(ab_dg.nodes)
    if len(nodes) == 1:
        pr = {nodes[0]: 1.0}
    else:
        pr = nx.pagerank(ab_dg, alpha=0.85, personalization=personal_array, max_iter=1000)

    print("  PageRank scores:")
    ranked = sorted(pr.items(), key=lambda x: -x[1])
    pr_rows = [(svc, f"{score:.6f}", "★ ROOT CAUSE" if i == 0 else "") for i, (svc, score) in enumerate(ranked)]
    _table(pr_rows, ("Service", "PageRank", ""), indent=4)

    top_service = ranked[0][0]
    print()

    # ── Assertion ─────────────────────────────────────────────────────────────
    ok = top_service == "checkoutservice"
    if ok:
        print("  ✅ Phase 2 PASSED — checkoutservice identified as root cause")
    else:
        print(f"  ❌ Phase 2 FAILED — expected checkoutservice, got {top_service}")

    return scaler, top_service


# ── Phase 3: GA Scaling Proposal ──────────────────────────────────────────────

def phase3_ga_proposal(scaler, roots: List[str], mock_k8s, prom_client: PrometheusClient):
    """Run choose_action('add') with MockGA and log the dry-run proposal."""

    _header("PHASE 3 — GA Scaling Proposal (Dry-Run)")

    sc = SCENARIOS[SCENARIO]
    current_replicas = dict(sc.replicas)

    print("  Input:")
    _kv("Root-cause services", roots, indent=4)
    _kv("Current replicas", current_replicas, indent=4)
    _kv("Bounds", f"min_pod={scaler.min_num}, max_pod={scaler.max_num}", indent=4)
    print()

    # Wire up scaler for choose_action
    scaler.prom_util = prom_client
    scaler.k8s_util = mock_k8s
    scaler.roots = roots
    scaler.svc_counts = mock_k8s.get_svcs_counts()
    scaler.mss = mock_k8s.get_svcs_without_state()

    with patch("PBScaler.KubernetesClient", return_value=mock_k8s), \
         patch("util.PrometheusClient.KubernetesClient", return_value=mock_k8s), \
         patch("PBScaler.GA", MockGA):
        scaler.choose_action("add")

    # ── Log scaling proposal ──────────────────────────────────────────────────
    scale_calls = mock_k8s.scale_calls
    print("  MockGA proposed scaling actions (DRY-RUN — not executed on any cluster):")
    action_rows = []
    for call in scale_calls:
        svc = call["svc"]
        new = call["replicas"]
        old = current_replicas.get(svc, "?")
        delta = f"+{new - old}" if isinstance(old, int) and new > old else (f"{new - old}" if isinstance(old, int) else "?")
        action_rows.append((svc, old, new, delta))
    _table(action_rows, ("Service", "Before", "After", "Delta"), indent=4)
    print()

    # ── Assertion ─────────────────────────────────────────────────────────────
    checkout_calls = [c for c in scale_calls if c["svc"] == "checkoutservice"]
    bounds_ok = all(1 <= c["replicas"] <= 8 for c in scale_calls)
    scaled_up = len(checkout_calls) > 0 and checkout_calls[-1]["replicas"] > current_replicas["checkoutservice"]

    ok = scaled_up and bounds_ok
    if ok:
        print("  ✅ Phase 3 PASSED — checkoutservice scaled "
              f"{current_replicas['checkoutservice']} → {checkout_calls[-1]['replicas']} "
              f"(within [{scaler.min_num}, {scaler.max_num}])")
    else:
        reasons = []
        if not scaled_up:
            reasons.append("checkoutservice was not scaled up")
        if not bounds_ok:
            reasons.append("some replicas out of [min_pod, max_pod] bounds")
        print(f"  ❌ Phase 3 FAILED — {'; '.join(reasons)}")
    return ok


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print(SEP)
    print("  PBScaler End-to-End Pipeline Dry-Run")
    print(f"  Scenario: {SCENARIO}")
    print(f"  {SCENARIOS[SCENARIO].description}")
    print(SEP)

    # ── Boot mock infrastructure ──────────────────────────────────────────────
    prom_server = MockPrometheusServer(scenario=SCENARIO)
    prom_server.start()

    sc = SCENARIOS[SCENARIO]
    mock_k8s = make_mock_k8s(sc)

    cfg = Config()
    cfg.prom_no_range_url = prom_server.query_url
    cfg.prom_range_url = prom_server.query_range_url
    cfg.namespace = "default"
    cfg.SLO = SLO
    cfg.max_pod = 8
    cfg.min_pod = 1

    pc = PrometheusClient(cfg)
    now = int(time.time())
    pc.set_time_range(now - 60, now)

    results: Dict[str, bool] = {}

    try:
        # Phase 1
        ab_calls = phase1_anomaly_detection(pc)
        results["Phase 1 — Anomaly Detection"] = len(ab_calls) > 0 and any(
            "checkoutservice" in c for c in ab_calls
        )

        if not ab_calls:
            print("\n  ⛔ No abnormal calls — cannot proceed to Phase 2/3")
            results["Phase 2 — Root-Cause Analysis"] = False
            results["Phase 3 — GA Proposal"] = False
        else:
            # Phase 2
            scaler, top = phase2_root_cause(ab_calls, mock_k8s, pc, cfg)
            results["Phase 2 — Root-Cause Analysis"] = top == "checkoutservice"

            # Phase 3 (fresh mock_k8s so scale_calls starts empty)
            mock_k8s_3 = make_mock_k8s(sc)
            # Re-create prom client for phase 3 (fresh time window)
            pc3 = PrometheusClient(cfg)
            pc3.set_time_range(now - 60, now)
            p3_ok = phase3_ga_proposal(scaler, [top], mock_k8s_3, pc3)
            results["Phase 3 — GA Proposal"] = p3_ok

    finally:
        prom_server.stop()

    # ── Summary ───────────────────────────────────────────────────────────────
    _header("SUMMARY")
    all_ok = True
    for phase, passed in results.items():
        icon = "✅" if passed else "❌"
        print(f"  {icon} {phase}")
        if not passed:
            all_ok = False

    print()
    if all_ok:
        print("  🎉 DRY-RUN COMPLETE — all 3 phases passed")
        print("     PBScaler would scale checkoutservice (2 → 3 replicas).")
        print("     No actual scaling was performed.")
    else:
        print("  ⛔ DRY-RUN FAILED — see details above")

    print()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
