"""
Mock Prometheus HTTP server for PBScaler offline testing.

Serves the same JSON envelope as a real Prometheus instance so that
PrometheusClient.execute_prom() works without any code changes.

Usage
-----
    server = MockPrometheusServer(scenario="single_bottleneck")
    server.start()                    # binds to 127.0.0.1 on a free port
    print(server.query_url)           # http://127.0.0.1:<port>/api/v1/query
    print(server.query_range_url)     # http://127.0.0.1:<port>/api/v1/query_range
    server.set_scenario("cascading_bottleneck")
    server.stop()

Query dispatch
--------------
PromQL is matched by keyword patterns rather than parsing, which is
sufficient because each PrometheusClient method issues a distinct query
shape.  The priority order in _dispatch_instant() / _dispatch_range()
ensures unambiguous routing.
"""

import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

from tests.mocks.scenarios import (
    CALL_EDGES,
    NAMESPACE,
    SLO_MS,
    SERVICES,
    SCENARIOS,
    ScenarioState,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_RNG = random.Random(42)


def _ts_series(start: float, end: float, step: int) -> List[float]:
    """Generate a list of unix timestamps matching Prometheus range output."""
    ts = math.ceil(start / step) * step
    result = []
    while ts <= end:
        result.append(float(ts))
        ts += step
    return result or [float(start)]


def _noisy(base: float, noise_pct: float = 0.05, n: int = 12) -> List[str]:
    """Return *n* string values around *base* with relative Gaussian noise."""
    sigma = base * noise_pct
    return [str(max(0.0, base + _RNG.gauss(0, sigma))) for _ in range(n)]


def _correlated_series(base: float, driver: List[float], scale: float) -> List[str]:
    """
    Produce values correlated with *driver* so Pearson weights are meaningful.

    The output tracks the driver (normalised) scaled by *scale*, plus noise.
    """
    driver_arr = np.array(driver, dtype=float)
    if driver_arr.std() == 0:
        return _noisy(base, n=len(driver))
    norm = (driver_arr - driver_arr.mean()) / driver_arr.std()
    vals = base + norm * scale * base + np.random.default_rng(0).normal(0, base * 0.03, len(driver))
    return [str(max(0.0, v)) for v in vals]


def _instant_result(metric: Dict[str, str], value: float) -> Dict:
    return {"metric": metric, "value": [int(time.time()), str(value)]}


def _range_result(metric: Dict[str, str], timestamps: List[float], values: List[str]) -> Dict:
    return {"metric": metric, "values": list(zip(timestamps, values))}


def _ok(result_type: str, result: List[Any]) -> bytes:
    body = {"status": "success", "data": {"resultType": result_type, "result": result}}
    return json.dumps(body).encode()


# ── Query dispatcher ──────────────────────────────────────────────────────────

class _Dispatcher:
    """Translates incoming PromQL queries into scenario-driven mock data."""

    def __init__(self) -> None:
        self._scenario: ScenarioState = SCENARIOS["normal_load"]
        self._lock = threading.Lock()

    def set_scenario(self, name: str) -> None:
        if name not in SCENARIOS:
            raise ValueError(f"Unknown scenario '{name}'. Available: {list(SCENARIOS)}")
        with self._lock:
            self._scenario = SCENARIOS[name]

    @property
    def scenario(self) -> ScenarioState:
        with self._lock:
            return self._scenario

    # ── Instant queries (/api/v1/query) ───────────────────────────────────────

    def dispatch_instant(self, query: str) -> bytes:
        sc = self.scenario
        now = int(time.time())

        # ── Call latency (source + destination pair) ──────────────────────────
        # Pattern: histogram_quantile … by (destination_workload, source_workload, le)
        if "source_workload" in query and "istio_request_duration_milliseconds_bucket" in query:
            result = []
            for src, dst in CALL_EDGES:
                lat = sc.latencies.get(dst, 80.0)
                result.append(_instant_result(
                    {"source_workload": src, "destination_workload": dst},
                    lat,
                ))
            return _ok("vector", result)

        # ── Per-service p90 / p50 / p99 latency ──────────────────────────────
        # Pattern: histogram_quantile … by (destination_workload, le)  ← no source_workload
        if "istio_request_duration_milliseconds_bucket" in query:
            result = []
            for svc in SERVICES:
                lat = sc.latencies.get(svc, 80.0)
                result.append(_instant_result({"destination_workload": svc}, lat))
            return _ok("vector", result)

        # ── Call-graph edges (TCP bytes) ──────────────────────────────────────
        if "istio_tcp_received_bytes_total" in query:
            result = []
            for src, dst in CALL_EDGES:
                result.append(_instant_result(
                    {"source_workload": src, "destination_workload": dst},
                    1000.0,
                ))
            return _ok("vector", result)

        # ── Call-graph edges (HTTP requests) ─────────────────────────────────
        if "istio_requests_total" in query and "source_workload, destination_workload" in query:
            result = []
            for src, dst in CALL_EDGES:
                result.append(_instant_result(
                    {"source_workload": src, "destination_workload": dst},
                    500.0,
                ))
            return _ok("vector", result)

        # ── QPS per service ───────────────────────────────────────────────────
        if "istio_requests_total" in query and "destination_workload" in query:
            result = []
            for svc in SERVICES:
                result.append(_instant_result(
                    {"destination_workload": svc},
                    sc.qps.get(svc, 10.0),
                ))
            return _ok("vector", result)

        # ── Pod-level CPU / memory (for get_svc_metric) ───────────────────────
        if "container_cpu_usage_seconds_total" in query:
            return _ok("vector", self._pod_instant("cpu_cores", sc, lambda s: sc.cpu_utilization.get(s, 0.3) * 0.5))
        if "container_spec_cpu_quota" in query:
            return _ok("vector", self._pod_instant("cpu_limit", sc, lambda _: 1.0))
        if "container_memory_usage_bytes" in query:
            return _ok("vector", self._pod_instant("mem_mb", sc, lambda _: 256.0))
        if "container_spec_memory_limit_bytes" in query:
            return _ok("vector", self._pod_instant("mem_limit_mb", sc, lambda _: 512.0))
        if "container_fs_usage_bytes" in query:
            return _ok("vector", self._pod_instant("fs_mb", sc, lambda _: 50.0))
        if "container_fs_write_seconds_total" in query:
            return _ok("vector", self._pod_instant("fs_write", sc, lambda _: 0.01))
        if "container_fs_read_seconds_total" in query:
            return _ok("vector", self._pod_instant("fs_read", sc, lambda _: 0.01))
        if "container_network_receive_bytes_total" in query:
            return _ok("vector", self._pod_instant("net_recv_kb", sc, lambda _: 100.0))
        if "container_network_transmit_bytes_total" in query:
            return _ok("vector", self._pod_instant("net_tx_kb", sc, lambda _: 100.0))

        # Fallback: empty result
        return _ok("vector", [])

    # ── Range queries (/api/v1/query_range) ───────────────────────────────────

    def dispatch_range(self, query: str, start: float, end: float, step: int) -> bytes:
        sc = self.scenario
        timestamps = _ts_series(start, end, step)
        n = len(timestamps)

        # ── Call p90 latency range ────────────────────────────────────────────
        if "source_workload" in query and "istio_request_duration_milliseconds_bucket" in query:
            result = []
            for src, dst in CALL_EDGES:
                lat = sc.latencies.get(dst, 80.0)
                result.append(_range_result(
                    {"source_workload": src, "destination_workload": dst,
                     "destination_workload_namespace": NAMESPACE},
                    timestamps,
                    _noisy(lat, n=n),
                ))
            return _ok("matrix", result)

        # ── Per-service p90 latency range ─────────────────────────────────────
        if "istio_request_duration_milliseconds_bucket" in query:
            result = []
            for svc in SERVICES:
                lat = sc.latencies.get(svc, 80.0)
                result.append(_range_result(
                    {"destination_workload": svc, "destination_workload_namespace": NAMESPACE},
                    timestamps,
                    _noisy(lat, n=n),
                ))
            return _ok("matrix", result)

        # ── QPS range ─────────────────────────────────────────────────────────
        if "istio_requests_total" in query and "destination_workload" in query:
            result = []
            for svc in SERVICES:
                result.append(_range_result(
                    {"destination_workload": svc},
                    timestamps,
                    _noisy(sc.qps.get(svc, 10.0), n=n),
                ))
            return _ok("matrix", result)

        # ── Aggregate vCPU / memory (get_resource_metric_range) ───────────────
        if "container_cpu_usage_seconds_total" in query and "by(pod)" not in query.replace(" ", ""):
            total_cpu = sum(sc.cpu_utilization.values()) * 0.5
            result = [_range_result({}, timestamps, _noisy(total_cpu, n=n))]
            return _ok("matrix", result)
        if "container_memory_usage_bytes" in query and "by(pod)" not in query.replace(" ", ""):
            result = [_range_result({}, timestamps, _noisy(1024.0, n=n))]
            return _ok("matrix", result)

        # ── Pod-level metrics range (get_svc_metric_range) ────────────────────
        #
        # For the bottleneck service, CPU values are correlated with its latency
        # so that cal_weight() returns a non-trivial Pearson coefficient.
        hot_svcs = set(sc.bottleneck_services())

        if "container_cpu_usage_seconds_total" in query:
            return _ok("matrix", self._pod_range_correlated(sc, timestamps, hot_svcs))

        if "container_spec_cpu_quota" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 1.0))
        if "container_memory_usage_bytes" in query and "rate" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 0.5))
        if "container_memory_usage_bytes" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 256.0))
        if "container_spec_memory_limit_bytes" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 512.0))
        if "container_fs_usage_bytes" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 50.0))
        if "container_fs_write_seconds_total" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 0.01))
        if "container_fs_read_seconds_total" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 0.01))
        if "container_network_receive_bytes_total" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 100.0))
        if "container_network_transmit_bytes_total" in query:
            return _ok("matrix", self._pod_range_flat(sc, timestamps, lambda _: 100.0))

        # ── Success rate ───────────────────────────────────────────────────────
        if "response_code" in query:
            result = []
            for svc in SERVICES:
                result.append(_range_result(
                    {"destination_workload": svc, "destination_workload_namespace": NAMESPACE},
                    timestamps,
                    _noisy(1.0, noise_pct=0.01, n=n),
                ))
            return _ok("matrix", result)

        return _ok("matrix", [])

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _pod_name(svc: str) -> str:
        return f"{svc}-mock-pod"

    def _pod_instant(self, _col: str, sc: ScenarioState, value_fn) -> List[Dict]:
        return [
            _instant_result({"pod": self._pod_name(svc)}, value_fn(svc))
            for svc in SERVICES
        ]

    def _pod_range_flat(self, sc: ScenarioState, timestamps: List[float], value_fn) -> List[Dict]:
        n = len(timestamps)
        return [
            _range_result(
                {"pod": self._pod_name(svc)},
                timestamps,
                _noisy(value_fn(svc), n=n),
            )
            for svc in SERVICES
        ]

    def _pod_range_correlated(self, sc: ScenarioState, timestamps: List[float], hot_svcs: set) -> List[Dict]:
        """
        CPU time series for hot (bottleneck) pods are correlated with their
        latency baseline so that cal_weight() returns a meaningful coefficient.
        """
        n = len(timestamps)
        result = []
        for svc in SERVICES:
            cpu_base = sc.cpu_utilization.get(svc, 0.3) * 0.5  # ~cores
            if svc in hot_svcs:
                # Generate latency-like driver then scale to CPU range
                lat = sc.latencies.get(svc, 80.0)
                lat_series = [float(v) for v in _noisy(lat, noise_pct=0.15, n=n)]
                values = _correlated_series(cpu_base, lat_series, scale=0.8)
            else:
                values = _noisy(cpu_base, n=n)
            result.append(_range_result({"pod": self._pod_name(svc)}, timestamps, values))
        return result


# ── HTTP handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    dispatcher: _Dispatcher  # set on the class by MockPrometheusServer

    def log_message(self, fmt, *args):
        pass  # suppress default access log; tests use assertions instead

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        query = params.get("query", [""])[0]

        if parsed.path == "/api/v1/query":
            body = self.dispatcher.dispatch_instant(query)
        elif parsed.path == "/api/v1/query_range":
            start = float(params.get("start", [time.time() - 60])[0])
            end = float(params.get("end", [time.time()])[0])
            step = int(params.get("step", [5])[0])
            body = self.dispatcher.dispatch_range(query, start, end, step)
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Public API ────────────────────────────────────────────────────────────────

class MockPrometheusServer:
    """
    Threaded mock Prometheus server.

    Parameters
    ----------
    scenario : str
        Initial scenario name (see ``tests/mocks/scenarios.py``).
    host : str
        Bind address (default: ``"127.0.0.1"``).
    port : int
        Bind port.  0 means "pick a free port" (default).
    """

    def __init__(self, scenario: str = "normal_load", host: str = "127.0.0.1", port: int = 0) -> None:
        self._dispatcher = _Dispatcher()
        self._dispatcher.set_scenario(scenario)

        # Attach dispatcher to the handler class via a subclass so the HTTPServer
        # can instantiate handlers without extra constructor arguments.
        handler_cls = type("_BoundHandler", (_Handler,), {"dispatcher": self._dispatcher})

        self._server = HTTPServer((host, port), handler_cls)
        actual_port = self._server.server_address[1]

        self.query_url = f"http://{host}:{actual_port}/api/v1/query"
        self.query_range_url = f"http://{host}:{actual_port}/api/v1/query_range"

        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> "MockPrometheusServer":
        """Start serving in a background daemon thread."""
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut down the server and join the background thread."""
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockPrometheusServer":
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()

    # ── Scenario control ──────────────────────────────────────────────────────

    def set_scenario(self, name: str) -> None:
        """Switch to a different scenario at runtime (thread-safe)."""
        self._dispatcher.set_scenario(name)

    @property
    def current_scenario(self) -> ScenarioState:
        return self._dispatcher.scenario
