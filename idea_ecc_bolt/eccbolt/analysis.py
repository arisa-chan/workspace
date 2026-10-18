"""High level analytical evaluation of a connection input (C&K ICR method)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np

from .icr import ICRResult, elastic_bolt_forces, solve_icr
from .model import ConnectionInput


@dataclass
class AnalysisOutput:
    ci: ConnectionInput
    icr: ICRResult
    rult: float                       # N, ultimate shear resistance of one bolt
    Pu: float                         # N, ultimate load of the group (C&K)
    utilization: float                # P / Pu
    service_forces: np.ndarray        # (n,2) bolt forces [N] at applied load P
    service_magnitudes: np.ndarray    # (n,) [N]
    elastic_forces: np.ndarray        # (n,2) elastic-method bolt forces per unit P
    moment: float                     # N.m, M = P*(e cos(theta) + h sin(theta))
    load_point: tuple
    notes: List[str] = field(default_factory=list)


def analyze(ci: ConnectionInput) -> AnalysisOutput:
    bolts = np.asarray(ci.bolts_xy, float)
    pdir = ci.load.direction
    h = ci.load.h_load
    if h is None:
        h = ci.plate_b.cy + ci.plate_b.height / 2
    ppoint = (ci.load.e, h)
    rult = ci.bolt_rult()
    icr = solve_icr(ci.bolts_xy, pdir, ppoint, delta_max=ci.bolt_mat.delta_max)
    Pu = icr.C * rult
    # R_i/Rult at Pu is independent of the load level; at the applied load P the
    # force in bolt i is (R_i/Rult) * P/C  [N].  Pu = C*Rult is reached when the
    # bolt farthest from the ICR is at delta_max, i.e. at 0.9815*Rult.
    service = (icr.bolt_forces * (ci.load.P / icr.C) if icr.C > 0
               else np.zeros_like(icr.bolt_forces))
    t = np.radians(ci.load.theta)
    e_eff = ci.load.e * math_cos(t) + h * math_sin(t)
    elastic = elastic_bolt_forces(ci.bolts_xy, pdir, e_eff) * ci.load.P
    notes = []
    if not icr.converged:
        notes.append("ICR iteration did not fully converge - results approximate.")
    return AnalysisOutput(ci=ci, icr=icr, rult=rult, Pu=Pu,
                          utilization=ci.load.P / Pu if Pu else float("nan"),
                          service_forces=service,
                          service_magnitudes=np.linalg.norm(service, axis=1),
                          elastic_forces=elastic,
                          moment=ci.load.moment_about_centroid() if ci.load.h_load is not None
                          else ci.load.P * e_eff,
                          load_point=ppoint, notes=notes)


def math_cos(t):
    import math
    return math.cos(t)


def math_sin(t):
    import math
    return math.sin(t)
