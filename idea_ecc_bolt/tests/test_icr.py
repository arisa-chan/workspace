"""Tests of the Crawford & Kulak (1971) ICR implementation."""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eccbolt.icr import (DELTA_MAX, bolt_force_ratio, solve_from_eccentricity,
                         solve_icr)
from table_ck import L, N, TABLE

IN = 0.0254


def row(n, b_in):
    return [(0.0, (i - (n - 1) / 2) * b_in * IN) for i in range(n)]


def test_load_deformation_curve():
    assert bolt_force_ratio(0.0) == 0.0
    assert bolt_force_ratio(DELTA_MAX) == pytest.approx(
        (1 - math.exp(-10 * 0.34)) ** 0.55, abs=1e-12)
    assert bolt_force_ratio(DELTA_MAX) == pytest.approx(0.9815, abs=1e-3)


def test_brandt_example_1():
    r = solve_from_eccentricity(row(3, 3), (0, -1), 4 * IN)
    assert r.C == pytest.approx(1.40, abs=0.01)
    assert r.elastic_C == pytest.approx(1.342, abs=0.005)
    assert r.converged


def test_brandt_example_2():
    bolts = [(x * IN, y * IN) for x in (-3, 3) for y in (-3, 0, 3)]
    r = solve_icr(bolts, (0.6, -0.8), (20 * IN, 5 * IN))
    assert r.C == pytest.approx(1.10, abs=0.01)
    assert r.elastic_C == pytest.approx(0.942, abs=0.005)


@pytest.mark.parametrize("b,l,n", [(3, 4, 3), (3, 16, 4), (3, 16, 6),
                                   (6, 24, 5), (3, 3, 12), (6, 36, 8)])
def test_ck_table_spot(b, l, n):
    ctab = TABLE[b][l][N.index(n)]
    r = solve_from_eccentricity(row(n, b), (0, -1), l * IN)
    assert r.C == pytest.approx(ctab, rel=0.02)


def test_full_table():
    worst = 0.0
    for b, rows in TABLE.items():
        for l, vals in rows.items():
            for n, ctab in zip(N, vals):
                r = solve_from_eccentricity(row(n, b), (0, -1), l * IN)
                worst = max(worst, abs(r.C - ctab) / ctab)
    assert worst < 0.025          # table is rounded to 3 significant digits


def test_scale_invariance():
    a = solve_from_eccentricity(row(5, 3), (0, -1), 12 * IN)
    b = solve_from_eccentricity(row(5, 6), (0, -1), 24 * IN)
    assert a.C == pytest.approx(b.C, rel=1e-9)


def test_concentric_limit():
    r = solve_from_eccentricity(row(6, 3), (0, -1), 0.0)
    assert r.C == pytest.approx(6.0, rel=1e-9)


def test_single_bolt():
    r = solve_from_eccentricity([(0.0, 0.0)], (0, -1), 1.0)
    assert r.C == pytest.approx(1.0)


def test_equilibrium_residual():
    r = solve_from_eccentricity(row(6, 3), (0, -1), 16 * IN)
    assert r.unbalanced < 1e-8
