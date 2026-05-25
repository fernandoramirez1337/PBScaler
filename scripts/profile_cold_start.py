"""profile_cold_start.py — measure T_cold,i per service (Cap_3 sec:obtain_tcold).

For each managed service in the namespace, scales the deployment by +1 N
times, measuring the wall-clock interval between pod creation and the
Ready condition becoming True. Reports per-service percentiles (P50, P95,
P99) plus min/max/mean, and writes a flat `t_cold_p95_seconds` dict
suitable for pasting into config.yaml temporal_gate.cold_times.

Assumes:
  - The benchmark application is already deployed in the target namespace
    (e.g., Online Boutique manifests applied via kubectl).
  - The current kubeconfig user has permission to scale deployments and
    list pods in the namespace.
  - Each service runs in a Deployment named after the service.

Usage:
  python scripts/profile_cold_start.py \\
      --namespace online-boutique \\
      --n-samples 30 \\
      --output t_cold_profile.json

  # Profile a single service:
  python scripts/profile_cold_start.py --services frontend --n-samples 5

The script restores each Deployment's original replica count after profiling.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from datetime import datetime, timezone

from kubernetes import client, config as k8s_config

logger = logging.getLogger("profile_cold_start")


def _list_svc_pods(core_v1, namespace: str, svc: str):
    """Pods whose name prefix matches the service."""
    pods = core_v1.list_namespaced_pod(namespace, watch=False)
    return [p for p in pods.items if svc in p.metadata.name]


def _wait_for_new_pod_ready(
    core_v1,
    namespace: str,
    svc: str,
    existing_names: set[str],
    timeout: float,
    poll_interval: float = 1.0,
) -> float | None:
    """Poll until a pod not in `existing_names` reaches Ready=True.

    Returns the duration from the pod's creation_timestamp to the Ready
    condition's last_transition_time, in seconds. Returns None on timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        for pod in _list_svc_pods(core_v1, namespace, svc):
            if pod.metadata.name in existing_names:
                continue
            conditions = pod.status.conditions or []
            for cond in conditions:
                if cond.type == "Ready" and cond.status == "True":
                    creation_ts = pod.metadata.creation_timestamp.timestamp()
                    ready_ts = cond.last_transition_time.timestamp()
                    return max(0.0, ready_ts - creation_ts)
        time.sleep(poll_interval)
    return None


def _wait_for_stable_replica_count(
    apps_v1, namespace: str, svc: str, target: int, timeout: float = 120.0
) -> bool:
    """Wait until both spec.replicas and status.ready_replicas equal target."""
    start = time.time()
    while time.time() - start < timeout:
        scale = apps_v1.read_namespaced_deployment_scale(svc, namespace)
        if scale.spec.replicas == target and (scale.status.replicas or 0) == target:
            return True
        time.sleep(1.0)
    return False


def profile_service(
    apps_v1,
    core_v1,
    namespace: str,
    svc: str,
    n_samples: int,
    timeout: float,
) -> dict | None:
    """Profile one service: scale up N times, measure new-pod Ready duration."""
    durations: list[float] = []
    initial_scale = apps_v1.read_namespaced_deployment_scale(svc, namespace)
    r0 = initial_scale.spec.replicas or 1
    logger.info(f"=== {svc} (initial replicas={r0}) ===")

    try:
        for i in range(n_samples):
            existing = {p.metadata.name for p in _list_svc_pods(core_v1, namespace, svc)}

            # Scale up by 1.
            target_up = r0 + 1
            apps_v1.patch_namespaced_deployment_scale(
                svc, namespace, {"spec": {"replicas": target_up}}
            )

            duration = _wait_for_new_pod_ready(
                core_v1, namespace, svc, existing, timeout
            )
            if duration is None:
                logger.warning(f"  sample {i + 1}/{n_samples}: TIMEOUT after {timeout}s")
            else:
                durations.append(duration)
                logger.info(f"  sample {i + 1}/{n_samples}: {duration:.2f}s")

            # Scale back down and wait for cluster to settle before next iteration.
            apps_v1.patch_namespaced_deployment_scale(
                svc, namespace, {"spec": {"replicas": r0}}
            )
            if not _wait_for_stable_replica_count(apps_v1, namespace, svc, r0):
                logger.warning(f"  {svc}: timed out waiting for replica count to settle at {r0}")
    finally:
        # Defensive: ensure original scale is restored even on KeyboardInterrupt.
        try:
            apps_v1.patch_namespaced_deployment_scale(
                svc, namespace, {"spec": {"replicas": r0}}
            )
        except Exception:
            logger.exception(f"failed to restore {svc} to {r0} replicas")

    if not durations:
        logger.error(f"{svc}: no successful samples")
        return None

    return {
        "n_samples": len(durations),
        "p50": _percentile(durations, 50),
        "p90": _percentile(durations, 90),
        "p95": _percentile(durations, 95),
        "p99": _percentile(durations, 99),
        "min": min(durations),
        "max": max(durations),
        "mean": statistics.mean(durations),
        "samples": durations,
    }


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile, matches numpy.percentile default."""
    if not values:
        return float("nan")
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Profile cold-start times per service.")
    parser.add_argument("--namespace", default="online-boutique")
    parser.add_argument("--kubeconfig", default="~/.kube/config")
    parser.add_argument(
        "--services", nargs="+",
        help="services to profile (default: all deployments in the namespace except loadgenerator)"
    )
    parser.add_argument("--n-samples", type=int, default=30,
                        help="repetitions per service (Cap_3 sec:obtain_tcold requires >=30)")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="per-sample timeout in seconds (long enough for Java cold starts)")
    parser.add_argument("--output", default="t_cold_profile.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.n_samples < 30:
        logger.warning(
            f"n_samples={args.n_samples} < 30; P95 estimate will be unstable per "
            f"Cap_3 sec:obtain_tcold (Hahn-Meeker)."
        )

    k8s_config.load_kube_config(config_file=os.path.expanduser(args.kubeconfig))
    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()

    if args.services:
        services = list(args.services)
    else:
        deployments = apps_v1.list_namespaced_deployment(args.namespace)
        services = sorted(
            d.metadata.name for d in deployments.items
            if d.metadata.name != "loadgenerator"
        )
    logger.info(f"Profiling {len(services)} services in {args.namespace}: {services}")

    results: dict[str, dict] = {}
    for svc in services:
        stats = profile_service(
            apps_v1, core_v1, args.namespace, svc,
            n_samples=args.n_samples, timeout=args.timeout,
        )
        if stats is not None:
            results[svc] = stats
            logger.info(
                f"  -> {svc}: P95={stats['p95']:.2f}s "
                f"(range {stats['min']:.2f}-{stats['max']:.2f}, "
                f"n={stats['n_samples']})"
            )

    output = {
        "namespace": args.namespace,
        "n_samples_requested": args.n_samples,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": results,
        # Flat T_cold dict for direct paste into config.yaml temporal_gate.cold_times.
        # Values rounded up to whole seconds (PBScaler temporal_gate uses ints).
        "t_cold_p95_seconds": {
            svc: max(1, int(stats["p95"] + 0.999))  # ceil to >= 1s
            for svc, stats in results.items()
        },
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Wrote {args.output} with {len(results)} services profiled")

    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
