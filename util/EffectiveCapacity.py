"""Effective capacity for PBScaler-keff.

Implements the warmup function family that maps a replica's age since
creation to its fractional capacity contribution, and aggregates across
a service's pods to yield k_eff_i(t). See Cap_3 sec:warmup_functions and
eq:keff for the formal definitions.

Three curves model alternative hypotheses about how capacity grows from
0 (just created) to 1 (warm):

  - step:    f(t) = 0 for t < T_cold, else 1   (PBScaler baseline)
  - linear:  f(t) = min(1, t / T_cold)         (uniform growth)
  - sigmoid: f(t) = affine-normalized sigmoid  (Java/Spring Boot stacks)
"""

from __future__ import annotations

import math
import time
from typing import Callable, Iterable, Literal, TypedDict


CurveName = Literal["step", "linear", "sigmoid"]


class PodState(TypedDict):
    """Pod snapshot used to compute capacity contribution.

    creation_ts is epoch seconds; ready is True once the readiness probe
    has succeeded.
    """

    ready: bool
    creation_ts: float


def f_step(t: float, t_cold: float) -> float:
    """Step warmup. Cap_3 eq:fstep."""
    return 1.0 if t >= t_cold else 0.0


def f_linear(t: float, t_cold: float) -> float:
    """Linear warmup. Cap_3 eq:flin."""
    if t <= 0.0:
        return 0.0
    return min(1.0, t / t_cold)


def f_sigmoid(t: float, t_cold: float) -> float:
    """Sigmoid warmup with kappa = 10 / t_cold, affine-normalized. Cap_3 eq:fsig.

    The affine rescaling makes f(0) = 0 and f(t_cold) = 1 exact (up to
    floating-point error), matching the boundary conditions of f_step
    and f_linear so the three curves are interchangeable in eq:keff.

    For t > t_cold the unclamped formula climbs above 1.0 because the
    canonical sigmoid keeps growing past its midpoint; we clamp at 1.0
    so a single replica never reports capacity above its nominal value.
    """
    if t <= 0.0:
        return 0.0
    if t >= t_cold:
        return 1.0
    kappa = 10.0 / t_cold
    half = t_cold / 2.0
    num = _sigma(kappa * (t - half)) - _sigma(-kappa * half)
    den = _sigma(kappa * half) - _sigma(-kappa * half)
    return num / den


def _sigma(x: float) -> float:
    # Clamp to avoid math.exp overflow at extreme arguments.
    x_clamped = max(-50.0, min(50.0, x))
    return 1.0 / (1.0 + math.exp(-x_clamped))


_CURVES: dict[str, Callable[[float, float], float]] = {
    "step": f_step,
    "linear": f_linear,
    "sigmoid": f_sigmoid,
}


def compute_keff(
    pods: Iterable[PodState],
    t_cold: float,
    curve: CurveName,
    now: float | None = None,
) -> float:
    """Sum of per-pod capacity contributions. Cap_3 eq:keff.

    Ready pods contribute 1.0; pods still in warmup contribute
    f_curve(t_now - creation_ts, t_cold).

    Args:
        pods: iterable of PodState.
        t_cold: cold-start time for the service (seconds).
        curve: one of 'step', 'linear', 'sigmoid'.
        now: epoch seconds; defaults to time.time(). Tests pass a fixed
            value for determinism.

    Raises:
        ValueError: unknown curve name.
    """
    if curve not in _CURVES:
        raise ValueError(
            f"unknown curve {curve!r}; expected one of {sorted(_CURVES)}"
        )
    f = _CURVES[curve]
    t_now = time.time() if now is None else now
    total = 0.0
    for pod in pods:
        if pod["ready"]:
            total += 1.0
        else:
            age = t_now - pod["creation_ts"]
            total += f(age, t_cold)
    return total


def fetch_pod_states(
    core_api,
    namespace: str,
    svc: str,
) -> list[PodState]:
    """Fetch pod states for a single service from the K8s API.

    Matches pods by name prefix, consistent with KubernetesClient.get_svcs_counts.
    Tests bypass this by passing PodState dicts directly to compute_keff.
    """
    pod_list = core_api.list_namespaced_pod(namespace, watch=False)
    states: list[PodState] = []
    for pod in pod_list.items:
        name = pod.metadata.name or ""
        if svc not in name:
            continue
        ready = False
        for cond in (pod.status.conditions or []):
            if cond.type == "Ready" and cond.status == "True":
                ready = True
                break
        creation_ts = pod.metadata.creation_timestamp.timestamp()
        states.append({"ready": ready, "creation_ts": creation_ts})
    return states
