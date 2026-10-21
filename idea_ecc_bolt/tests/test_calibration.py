"""Tests for the user-adjustable C&K parameters and the Calibrate command."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eccbolt.analysis import analyze
from eccbolt.benchmarks import (ExperimentalCase, experimental_sets,
                                select_experimental_cases)
from eccbolt.calibration import (calibrate_ck_parameters,
                                 calibrate_fea_parameters, calibrate_selected)
from eccbolt.icr import bolt_force_ratio, solve_from_eccentricity
from eccbolt.model import BoltMaterial, INCH
from eccbolt.presets import crawford_kulak_example


def row(n, b_in):
    return [(0.0, (i - (n - 1) / 2) * b_in * INCH) for i in range(n)]


def test_ck_parameters_are_user_adjustable():
    """Changing mu/lambda must change the load-deformation law and the group C."""
    base = solve_from_eccentricity(row(4, 3), (0, -1), 8 * INCH)
    soft = solve_from_eccentricity(row(4, 3), (0, -1), 8 * INCH,
                                   mu=100.0, lam=0.4)
    stiff = solve_from_eccentricity(row(4, 3), (0, -1), 8 * INCH,
                                    mu=1200.0, lam=0.8)
    assert base.C != pytest.approx(soft.C)
    assert base.C != pytest.approx(stiff.C)
    assert bolt_force_ratio(0.005, mu=100.0, lam=0.4) != pytest.approx(
        bolt_force_ratio(0.005, mu=1200.0, lam=0.8))


def test_analysis_uses_the_bolt_material_parameters():
    ci = crawford_kulak_example()
    c0 = analyze(ci).icr.C
    ci.bolt_mat.mu *= 2.0
    ci.bolt_mat.ck_lambda = 0.35
    c1 = analyze(ci).icr.C
    assert c0 != pytest.approx(c1)


def test_calibration_reduces_the_relative_error():
    res = calibrate_ck_parameters(max_iter=80)
    assert res.n_cases == 10
    assert res.mu > 0 and res.ck_lambda > 0
    assert res.rmse <= res.initial_rmse + 1e-12
    assert res.mean_ratio == pytest.approx(
        sum(m / p for _, m, _, p in res.cases)
        / len(res.cases), rel=1e-9)
    text = res.format()
    assert "mu" in text and "lambda" in text and "RMSE" in text


def test_calibration_applies_to_a_connection_input():
    res = calibrate_ck_parameters(max_iter=40)
    ci = crawford_kulak_example()
    res.apply(ci)
    assert ci.bolt_mat.mu == pytest.approx(res.mu)
    assert ci.bolt_mat.ck_lambda == pytest.approx(res.ck_lambda)
    assert ci.bolt_mat.mu > 0


def test_default_parameters_are_still_the_original_ck_values():
    bm = BoltMaterial()
    assert bm.mu * INCH == pytest.approx(10.0)
    assert bm.ck_lambda == pytest.approx(0.55)
    assert bm.lambda_ == bm.ck_lambda


def test_experimental_sets_and_sample_selection():
    sets = experimental_sets()
    assert "badawi_1983" in sets
    assert "crawford_kulak_1968" in sets
    assert "denavit_2021" in sets
    assert len(sets["crawford_kulak_1968"]) == 8
    assert len(sets["denavit_2021"]) == 22
    cases = select_experimental_cases({
        "crawford_kulak_1968": ["B1", "B2"],
        "badawi_1983": ["S13"],
    })
    assert {c.name for c in cases} == {"B1", "B2", "S13"}
    assert {c.set_key for c in cases} == {"crawford_kulak_1968",
                                          "badawi_1983"}
    # selecting a whole set works as well
    assert len(select_experimental_cases({"sarkar_1992": None})) == 5


def test_selection_is_honoured_by_calibration():
    res = calibrate_ck_parameters(selection={
        "crawford_kulak_1968": ["B1", "B2"]}, max_iter=20)
    assert res.n_cases == 2
    assert {c[0] for c in res.cases} == {"B1", "B2"}
    out = calibrate_selected({"crawford_kulak_1968": ["B1"]},
                             include_ck=True, include_fea=False,
                             max_iter=10)
    assert "ck" in out and out["ck"].n_cases == 1


def test_fea_calibration_factor(monkeypatch):
    cases = select_experimental_cases({"denavit_2021": ["STD1", "STD2"]})

    def fake_fea(self, **kwargs):
        return {"ok": True, "capacity": self.failure_load * 1.25,
                "raw_capacity": self.failure_load * 1.25}

    monkeypatch.setattr(ExperimentalCase, "fea_analysis", fake_fea)
    res = calibrate_fea_parameters(cases=cases)
    assert res.n_used == 2
    assert res.factor == pytest.approx(0.8)
    assert res.rmse < res.initial_rmse
    ci = crawford_kulak_example()
    res.apply(ci)
    assert ci.fem.capacity_factor == pytest.approx(0.8)
