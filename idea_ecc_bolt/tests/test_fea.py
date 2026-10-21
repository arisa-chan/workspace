"""Smoke test of the OpenSees FE fallback on a physical specimen."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("openseespy")

from eccbolt.benchmarks import experimental_cases  # noqa: E402
from eccbolt.fe_fallback import model_from_connection  # noqa: E402
from eccbolt.presets import crawford_kulak_example  # noqa: E402


def test_fea_model_contains_holes_clearance_and_nonlinear_plates():
    ci = crawford_kulak_example()
    model = model_from_connection(ci)
    assert model["hole_clearance"] == ci.bolt_hole_clearance
    assert model["bolt_diameter"] == ci.bolt_diameter
    assert model["plate_nonlinear"] is False   # safe default
    ci.fem.plate_nonlinear = True
    assert model_from_connection(ci)["plate_nonlinear"] is True
    assert model["mu"] == pytest.approx(ci.bolt_mat.mu)
    assert model["ck_lambda"] == pytest.approx(ci.bolt_mat.ck_lambda)
    ci.fem.mesh_min_element = 0.003
    ci.fem.mesh_max_element = 0.012
    model = model_from_connection(ci)
    assert model["mesh_min_element"] == pytest.approx(0.003)
    assert model["mesh_max_element"] == pytest.approx(0.012)


def test_fea_fallback_predicts_a_capacity_for_a_physical_specimen():
    case = experimental_cases()[0]
    res = case.fea_analysis(mesh=0.012)
    assert res.get("ok"), res.get("message")
    assert res.get("capacity") and res["capacity"] > 0
    assert res.get("failure_mode") in ("bolt", "plate")
    assert res.get("plate_model")
    # the bolt holes and clearance must be present in the returned model data
    assert res.get("hole_clearance") == pytest.approx(
        case.connection_input().bolt_hole_clearance)
    # broad engineering acceptance for the fallback (not a unit-test tolerance)
    ratio = case.failure_load / res["capacity"]
    assert 0.6 <= ratio <= 1.8
