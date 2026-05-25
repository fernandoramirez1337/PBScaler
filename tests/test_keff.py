"""Tests de integración para PBScalerKeff.

Cubren el wiring de la subclase: que `_ga_extra_set_env_kwargs` produzca
las claves esperadas con los valores correctos. La validación de las
fórmulas de la fitness y de las curvas vive en `test_ga_keff.py` y
`test_effective_capacity.py`. La pipeline completa (Prometheus, K8s,
predictor) se bypassa vía `__new__`, mismo patrón que
`test_naive_temporal_gate.py`.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _make_keff(
    svc_counts: dict[str, int] | None = None,
    t_cold: dict[str, float] | None = None,
    pod_states: dict[str, list[dict]] | None = None,
    warmup_curve: str = "step",
    min_num: int = 1,
):
    """Instancia PBScalerKeff salteando el constructor pesado del padre."""
    from others.PBScalerKeff import PBScalerKeff

    keff = PBScalerKeff.__new__(PBScalerKeff)
    keff.mss = list((svc_counts or {}).keys())
    keff.svc_counts = dict(svc_counts or {})
    keff.min_num = min_num
    keff._t_cold = dict(t_cold or {})
    keff._warmup_curve = warmup_curve
    keff._pod_states = dict(pod_states or {})
    keff.k8s_util = MagicMock()
    return keff


def _ready_pod():
    return {"ready": True, "creation_ts": time.time() - 3600.0}


class TestGaExtraKwargs:
    """Verifica que la subclase pase los parámetros keff al GA."""

    def test_returns_all_three_keys(self):
        keff = _make_keff(
            svc_counts={"checkout": 1},
            t_cold={"checkout": 5.0},
            pod_states={"checkout": [_ready_pod()]},
            warmup_curve="linear",
        )
        kwargs = keff._ga_extra_set_env_kwargs(["checkout"])
        assert set(kwargs.keys()) == {
            "pod_states_by_svc", "t_cold_by_svc", "warmup_curve",
        }

    def test_pod_states_filtered_to_mss(self):
        keff = _make_keff(
            svc_counts={"checkout": 1, "payment": 1},
            t_cold={"checkout": 5.0, "payment": 60.0},
            pod_states={
                "checkout": [_ready_pod()],
                "payment": [_ready_pod()],
            },
        )
        kwargs = keff._ga_extra_set_env_kwargs(["checkout"])
        assert list(kwargs["pod_states_by_svc"].keys()) == ["checkout"]

    def test_warmup_curve_propagated(self):
        keff = _make_keff(
            svc_counts={"checkout": 1},
            warmup_curve="sigmoid",
        )
        kwargs = keff._ga_extra_set_env_kwargs(["checkout"])
        assert kwargs["warmup_curve"] == "sigmoid"


if __name__ == "__main__":
    unittest.main()
