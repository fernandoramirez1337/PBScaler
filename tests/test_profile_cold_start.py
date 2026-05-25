"""Tests for the pure helpers in scripts/profile_cold_start.

The K8s-facing functions are exercised end-to-end against a real cluster
and are not unit-tested here. Only the percentile helper is pure and worth
a sanity check.
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# scripts/ is a sibling of the package root; add it explicitly.
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from profile_cold_start import _percentile  # type: ignore


class TestPercentile:
    def test_empty_returns_nan(self):
        result = _percentile([], 95)
        assert result != result  # NaN is the only float that fails self-equality

    def test_single_value(self):
        assert _percentile([5.0], 95) == 5.0

    def test_midpoint(self):
        # Linear interpolation between sorted [1, 2, 3, 4]: p=50 -> 2.5
        assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)

    def test_extreme_percentiles(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(values, 0) == pytest.approx(1.0)
        assert _percentile(values, 100) == pytest.approx(5.0)

    def test_p95_realistic_distribution(self):
        # 30 samples; P95 should land near the top of the distribution
        values = list(range(1, 31))  # 1..30
        p95 = _percentile([float(v) for v in values], 95)
        # numpy.percentile with default linear interp gives 28.55 here
        assert p95 == pytest.approx(28.55, abs=0.01)

    def test_unsorted_input(self):
        # The implementation must sort internally
        assert _percentile([5, 1, 3, 2, 4], 50) == pytest.approx(3.0)
