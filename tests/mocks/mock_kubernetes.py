"""
In-memory mock of KubernetesClient.

Implements the same public interface used by PBScaler and PrometheusClient so
both callers work without modification when KubernetesClient is patched.

Replica state is mutable so tests can verify that PBScaler's execute_task()
actually applied the GA-computed scaling actions.
"""

from typing import Dict, List, Optional
from tests.mocks.scenarios import SERVICES, ScenarioState


# ── Namespace objects that mirror the kubernetes Python client's return types ─

class _Scale:
    def __init__(self, replicas: int) -> None:
        self.spec = type("Spec", (), {"replicas": replicas})()


class _DeploymentStatus:
    def __init__(self, replicas: int) -> None:
        self.ready_replicas = replicas
        self.replicas = replicas


class _DeploymentItem:
    def __init__(self, name: str, replicas: int) -> None:
        self.metadata = type("Meta", (), {"name": name})()
        self.spec = type("Spec", (), {"replicas": replicas})()
        self.status = _DeploymentStatus(replicas)


class _DeploymentList:
    def __init__(self, items: List[_DeploymentItem]) -> None:
        self.items = items


class _PodItem:
    def __init__(self, name: str) -> None:
        self.metadata = type("Meta", (), {"name": name})()


class _PodList:
    def __init__(self, items: List[_PodItem]) -> None:
        self.items = items


# ── MockKubernetesClient ──────────────────────────────────────────────────────

class MockKubernetesClient:
    """
    Drop-in replacement for ``util.KubernetesClient.KubernetesClient``.

    Parameters
    ----------
    scenario : ScenarioState
        Initial state (replica counts are copied from ``scenario.replicas``).
    namespace : str
        Namespace returned by metadata queries.
    """

    def __init__(self, scenario: ScenarioState, namespace: str = "default") -> None:
        self.namespace = namespace
        # Mutable replica store — patched by patch_scale() calls
        self._replicas: Dict[str, int] = dict(scenario.replicas)
        # Track every scale call for assertion in tests
        self.scale_calls: List[Dict] = []

    # ── KubernetesClient public interface ─────────────────────────────────────

    def get_svcs(self) -> List[str]:
        return sorted(self._replicas.keys())

    def get_svcs_without_state(self) -> List[str]:
        stateful = {"redis", "rabbitmq", "mongo", "mysql"}
        return sorted(s for s in self._replicas if not any(kw in s for kw in stateful))

    def get_svcs_counts(self) -> Dict[str, int]:
        return dict(self._replicas)

    def get_svc_count(self, svc: str) -> int:
        return self._replicas.get(svc, 1)

    def all_avaliable(self) -> bool:
        return True

    def svcs_avaliable(self, svcs: List[str]) -> bool:
        return True

    def patch_scale(self, svc: str, count: int) -> None:
        self.scale_calls.append({"svc": svc, "replicas": count})
        self._replicas[svc] = count

    def update_yaml(self) -> None:
        pass  # no-op in test context

    # ── Kubernetes client API wrappers (used indirectly via KubernetesClient) ─

    # The apps_api and core_api attributes are accessed directly by some code
    # paths via KubernetesClient. Expose thin shims so the attribute look-ups
    # succeed without importing the kubernetes library.

    @property
    def apps_api(self):
        return _AppsApiShim(self._replicas, self.namespace)

    @property
    def core_api(self):
        return _CoreApiShim(self._replicas, self.namespace)


class _AppsApiShim:
    def __init__(self, replicas: Dict[str, int], namespace: str) -> None:
        self._replicas = replicas
        self._namespace = namespace

    def list_namespaced_deployment(self, namespace: str):
        items = [_DeploymentItem(svc, r) for svc, r in self._replicas.items()]
        return _DeploymentList(items)

    def read_namespaced_deployment_scale(self, svc: str, namespace: str):
        return _Scale(self._replicas.get(svc, 1))

    def patch_namespaced_deployment_scale(self, svc: str, namespace: str, body: Dict) -> None:
        self._replicas[svc] = body["spec"]["replicas"]


class _CoreApiShim:
    def __init__(self, replicas: Dict[str, int], namespace: str) -> None:
        self._replicas = replicas
        self._namespace = namespace

    def list_namespaced_pod(self, namespace: str, watch: bool = False):
        # One mock pod per service
        items = [_PodItem(f"{svc}-mock-pod") for svc in self._replicas]
        return _PodList(items)


# ── Factory ───────────────────────────────────────────────────────────────────

def make_mock_k8s(scenario: ScenarioState, namespace: str = "default") -> MockKubernetesClient:
    """Convenience factory matching MockPrometheusServer's API style."""
    return MockKubernetesClient(scenario=scenario, namespace=namespace)
