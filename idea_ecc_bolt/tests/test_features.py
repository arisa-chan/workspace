"""Tests for the two API-capability features:
(1) arbitrary / random bolt coordinates, (2) angled loads resolved into
    horizontal + vertical force components.
"""
import math
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eccbolt.analysis import analyze
from eccbolt.icr import solve_icr
from eccbolt.iom import IomBuilder, member_load_components
from eccbolt.model import (INCH, KIP, ConnectionInput, Plate,
                           plate_overlap, random_bolt_group,
                           random_bolt_group_in_connection)
from eccbolt.presets import crawford_kulak_example


# ---------------------------------------------------------------------------
# 1 - arbitrary (random) bolt coordinates
# ---------------------------------------------------------------------------
def test_random_pattern_generation():
    pts = random_bolt_group(9, 0.25, 0.35, 0.05, seed=7)
    assert len(pts) == 9
    for i in range(9):
        for j in range(i + 1, 9):
            assert math.hypot(pts[i][0] - pts[j][0],
                              pts[i][1] - pts[j][1]) >= 0.05 - 1e-12
    # not a regular grid: pairwise distance set is not a single value
    d = sorted({round(math.hypot(a[0] - b[0], a[1] - b[1]), 6)
                for i, a in enumerate(pts) for b in pts[i + 1:]})
    assert len(d) > 3


def test_random_pattern_stays_inside_plate_overlap():
    ci = ConnectionInput(
        bolt_diameter=0.019,
        plate_a=Plate("A", width=0.30, height=0.40),
        plate_b=Plate("B", width=0.20, height=0.26, cx=0.04, cy=0.03))
    x0, x1, y0, y1 = plate_overlap(ci)
    pts = random_bolt_group_in_connection(ci, 8, 0.025, seed=5)
    margin = ci.bolt_diameter
    for x, y in pts:
        assert x0 + margin - 1e-12 <= x <= x1 - margin + 1e-12
        assert y0 + margin - 1e-12 <= y <= y1 - margin + 1e-12
    assert len(pts) == 8


def test_random_pattern_iom_roundtrip():
    """The exported IOM BoltGrid keeps the arbitrary coordinates exactly."""
    from eccbolt.iom import parse_open_model
    ci = crawford_kulak_example()
    ci.bolts_xy = random_bolt_group(7, 0.22, 0.30, 0.06, seed=3)
    root = parse_open_model(IomBuilder(ci).build())
    bg = root.find("Connections/ConnectionData/BoltGrids/BoltGrid")
    got = [(float(p.find("X").text), float(p.find("Z").text))
           for p in bg.find("Positions")]
    assert got == [(pytest.approx(x), pytest.approx(y)) for x, y in ci.bolts_xy]


def test_random_pattern_solves_and_runs():
    ci = crawford_kulak_example()
    ci.bolts_xy = random_bolt_group(8, 0.24, 0.32, 0.055, seed=11)
    a = analyze(ci)
    assert a.icr.converged and a.icr.C > 0
    # full REST pipeline (mock service) with the irregular group
    from eccbolt.idea_service import IdeaWorkflow
    from eccbolt.mock_service import MockIdeaService
    with tempfile.TemporaryDirectory() as td:
        with MockIdeaService() as svc:
            wf = IdeaWorkflow(base_url=svc.url, logger=lambda s: None)
            wf.connect()
            res = wf.analyse(ci, out_dir=td, save_project=False,
                             save_report=False)
            wf.close()
    assert res.ok and len(res.bolts) == 8


# ---------------------------------------------------------------------------
# 1b - angled load through the full REST pipeline (mock service)
# ---------------------------------------------------------------------------
def test_mock_pipeline_handles_angled_load():
    """The mock service must recover the same C as the direct ICR solution
    when the load is applied inclined, purely from the force components."""
    from eccbolt.idea_service import IdeaWorkflow
    from eccbolt.mock_service import MockIdeaService
    ci = ConnectionInput()
    ci.bolts_xy = [(x * INCH, y * INCH) for x in (-3, 3) for y in (-3, 0, 3)]
    from eccbolt.model import LoadSpec
    ci.load = LoadSpec(P=10 * KIP, theta=math.degrees(math.atan2(0.6, 0.8)),
                       e=20 * INCH, h_load=5 * INCH)
    direct = analyze(ci).icr.C
    with tempfile.TemporaryDirectory() as td:
        with MockIdeaService() as svc:
            wf = IdeaWorkflow(base_url=svc.url, logger=lambda s: None)
            wf.connect()
            res = wf.analyse(ci, out_dir=td, save_project=False,
                             save_report=False)
            wf.close()
    cap = res.raw["connectionCapacity"]
    assert cap["coefficientC"] == pytest.approx(direct, rel=1e-6)
    assert res.capacity_ck == pytest.approx(direct * ci.bolt_rult(), rel=1e-9)


def test_reported_bolt_forces_satisfy_equilibrium():
    """The bolt forces read back from the service must balance the applied load
    and must respect the C&K load-deformation limit (R <= 0.9815*Rult at Pu)."""
    from eccbolt.idea_service import IdeaWorkflow
    from eccbolt.mock_service import MockIdeaService
    ci = crawford_kulak_example()
    ana = analyze(ci)
    with tempfile.TemporaryDirectory() as td:
        with MockIdeaService() as svc:
            wf = IdeaWorkflow(base_url=svc.url, logger=lambda s: None)
            wf.connect()
            res = wf.analyse(ci, out_dir=td, save_project=False,
                             save_report=False)
            wf.close()
    exact = ci.load.P * ana.icr.bolt_magnitudes / ana.icr.C
    for bolt, f in zip(res.bolts, exact):
        assert bolt.shear_v == pytest.approx(f, rel=1e-6)
    # the bolt forces must take the applied load into equilibrium
    fx = sum(b.shear_components[0] for b in res.bolts)
    fz = sum(b.shear_components[1] for b in res.bolts)
    px, pz = ci.load.direction
    assert fx == pytest.approx(-ci.load.P * px, abs=1e-6)
    assert fz == pytest.approx(-ci.load.P * pz, abs=1e-6)
    assert max(b.shear_v for b in res.bolts) < ci.bolt_rult()
    assert res.applied_load == pytest.approx(ci.load.P)
    assert res.capacity == pytest.approx(ana.Pu, rel=0.03)


# ---------------------------------------------------------------------------
# 2 - angled load resolved into components
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("theta", [0.0, 30.0, 45.0, 90.0])
def test_angled_load_components(theta):
    ci = crawford_kulak_example()
    ci.load.theta = theta
    ci.load.h_load = 0.25
    a, c = member_load_components(ci)
    P, t = ci.load.P, math.radians(theta)
    px, pz = P * math.sin(t), -P * math.cos(t)          # global X, Z
    # global moment about the node = r x P with r = (e, 0, h)
    my_g = 0.25 * px - ci.load.e * pz
    # member local system: x = +Z, y = -Y, z = +X
    assert c["n"] == pytest.approx(pz)
    assert c["vz"] == pytest.approx(px)                 # in-plane horizontal
    assert c["vy"] == pytest.approx(0.0)
    assert c["my"] == pytest.approx(-my_g)
    assert c["mx"] == pytest.approx(0.0)
    assert c["mz"] == pytest.approx(0.0)
    # equilibrium between the two members
    for k in a:
        assert a[k] + c[k] == pytest.approx(0.0, abs=1e-9)


def test_angled_load_icr_matches_brandt_example_2():
    """Brandt Ex.2 is an angled-load case: (0.6, -0.8) => theta = 36.87 deg."""
    ci = ConnectionInput()
    ci.bolts_xy = [(x * INCH, y * INCH) for x in (-3, 3) for y in (-3, 0, 3)]
    from eccbolt.model import LoadSpec
    ci.load = LoadSpec(P=10 * KIP, theta=math.degrees(math.atan2(0.6, 0.8)),
                       e=20 * INCH, h_load=5 * INCH)
    a = analyze(ci)
    assert a.icr.C == pytest.approx(1.10, abs=0.01)
