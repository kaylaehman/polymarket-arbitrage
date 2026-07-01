import math, pytest
from core.directional.climate.base import gaussian_interval_prob, interval_from_market

def test_interval_from_market():
    assert interval_from_market("greater", 99.0, None) == (99.0, None)
    assert interval_from_market("less", None, 92.0) == (None, 92.0)
    assert interval_from_market("between", 98.0, 99.0) == (98.0, 99.0)

def test_gaussian_interval_prob_bucket():
    # mean exactly in a tight bucket -> ~the mass within +-0.5 sigma-ish
    p = gaussian_interval_prob(98.0, 99.0, mean=98.5, sigma=3.0)
    assert 0.10 < p < 0.20

def test_gaussian_interval_prob_open_upper():
    # P(X > 99) with mean 90, sigma 3 -> ~0 (far tail)
    assert gaussian_interval_prob(99.0, None, mean=90.0, sigma=3.0) < 0.01
    # P(X > 90) with mean 90 -> ~0.5
    assert abs(gaussian_interval_prob(90.0, None, mean=90.0, sigma=3.0) - 0.5) < 0.02

def test_gaussian_interval_prob_open_lower():
    # P(X <= 92) with mean 90 -> > 0.5
    assert gaussian_interval_prob(None, 92.0, mean=90.0, sigma=3.0) > 0.5
