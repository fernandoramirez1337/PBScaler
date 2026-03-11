"""
Scenario definitions for PBScaler mock testing.

Three scenarios exercise different branches of the three-phase pipeline:
  normal_load         -- no SLO violations; anomaly detector should be quiet
  single_bottleneck   -- one service saturated; PageRank should surface it
  cascading_bottleneck-- checkoutservice already scaled; paymentservice is now hot
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# ── Service topology (mirrors a minimal Online Boutique deployment) ───────────
NAMESPACE = "default"
SLO_MS = 200          # target p90 latency (ms) — matches config.yaml
ANOMALY_ALPHA = 0.2   # matches PBScaler.ALPHA
ANOMALY_THRESHOLD = SLO_MS * (1 + ANOMALY_ALPHA / 2)  # 220 ms

SERVICES: List[str] = [
    "cartservice",
    "checkoutservice",
    "frontend",
    "paymentservice",
    "productcatalogservice",
]

# Directed call graph edges (source → destination)
CALL_EDGES: List[Tuple[str, str]] = [
    ("frontend", "cartservice"),
    ("frontend", "productcatalogservice"),
    ("frontend", "checkoutservice"),
    ("checkoutservice", "paymentservice"),
]


@dataclass
class ScenarioState:
    """Complete description of a cluster snapshot for one scenario."""

    description: str
    # p90 latency in ms per destination service
    latencies: Dict[str, float]
    # current replica counts per service
    replicas: Dict[str, int]
    # requests-per-second per service
    qps: Dict[str, float]
    # 0–1 CPU utilisation per service (used to make Pearson correlation meaningful)
    cpu_utilization: Dict[str, float]

    def bottleneck_services(self) -> List[str]:
        return [s for s, lat in self.latencies.items() if lat > ANOMALY_THRESHOLD]

    def abnormal_call_edges(self) -> List[Tuple[str, str]]:
        """Call edges whose destination exceeds the anomaly threshold."""
        hot = set(self.bottleneck_services())
        return [(src, dst) for src, dst in CALL_EDGES if dst in hot]


# ── Scenario catalogue ────────────────────────────────────────────────────────

SCENARIOS: Dict[str, ScenarioState] = {
    "normal_load": ScenarioState(
        description="All services healthy — p90 well below SLO",
        latencies={
            "frontend": 80.0,
            "cartservice": 75.0,
            "productcatalogservice": 60.0,
            "checkoutservice": 90.0,
            "paymentservice": 85.0,
        },
        replicas={s: 2 for s in SERVICES},
        qps={
            "frontend": 50.0,
            "cartservice": 45.0,
            "productcatalogservice": 40.0,
            "checkoutservice": 30.0,
            "paymentservice": 30.0,
        },
        cpu_utilization={s: 0.30 for s in SERVICES},
    ),

    "single_bottleneck": ScenarioState(
        description="checkoutservice saturated — p90=350 ms, SLO=200 ms",
        latencies={
            "frontend": 130.0,
            "cartservice": 80.0,
            "productcatalogservice": 65.0,
            "checkoutservice": 350.0,   # ← bottleneck
            "paymentservice": 90.0,
        },
        replicas={s: 2 for s in SERVICES},
        qps={
            "frontend": 60.0,
            "cartservice": 55.0,
            "productcatalogservice": 50.0,
            "checkoutservice": 40.0,
            "paymentservice": 40.0,
        },
        cpu_utilization={
            "frontend": 0.40,
            "cartservice": 0.35,
            "productcatalogservice": 0.30,
            "checkoutservice": 0.92,    # ← saturated CPU
            "paymentservice": 0.35,
        },
    ),

    "cascading_bottleneck": ScenarioState(
        description=(
            "checkoutservice resolved by scaling (replicas=4); "
            "paymentservice is now the bottleneck (p90=280 ms)"
        ),
        latencies={
            "frontend": 100.0,
            "cartservice": 75.0,
            "productcatalogservice": 62.0,
            "checkoutservice": 110.0,   # resolved
            "paymentservice": 280.0,    # ← new bottleneck
        },
        replicas={
            "frontend": 2,
            "cartservice": 2,
            "productcatalogservice": 2,
            "checkoutservice": 4,       # was scaled up by previous cycle
            "paymentservice": 2,
        },
        qps={
            "frontend": 60.0,
            "cartservice": 55.0,
            "productcatalogservice": 50.0,
            "checkoutservice": 40.0,
            "paymentservice": 40.0,
        },
        cpu_utilization={
            "frontend": 0.35,
            "cartservice": 0.30,
            "productcatalogservice": 0.28,
            "checkoutservice": 0.50,    # resolved
            "paymentservice": 0.88,     # ← saturated
        },
    ),
}
