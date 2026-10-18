"""
Ultimate-strength analysis of eccentrically loaded bolt groups by the
instantaneous-centre-of-rotation (ICR) method of

    S. F. Crawford and G. L. Kulak, "Eccentrically Loaded Bolted
    Connections", Journal of the Structural Division, ASCE,
    Vol. 97, No. ST3, March 1971, pp. 765-783.

Bolt load-deformation relationship (C&K eq. / Guide to Design Criteria eq. 13.8)

    R = Rult * (1 - exp(-mu * delta))**lambda      mu = 10 1/in, lambda = 0.55

with delta in inches and the deformation of the fastener farthest from the
instantaneous centre equal to delta_max = 0.34 in.  The connected plates are
assumed rigid; bolt deformations vary linearly with the distance from the ICR
and the bolt force acts perpendicular to the radius of rotation.

The ICR position (x0, y0) is found by solving the two force-equilibrium
equations (C&K eqs. 13.13/13.14) with a damped Newton iteration started from
the elastic instantaneous centre (Brandt, 1982).  The moment equation
(C&K eq. 13.12) then gives the ultimate load

    Pu = C * Rult ,      C = sum_i (R_i/Rult) * r_i / |Mp|

with Mp the moment about the ICR of a unit load.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

def cross2(a, b):
    """Scalar cross product (z component) of two 2-D vectors."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.ndim == 2:
        return a[:, 0] * b[1] - a[:, 1] * b[0]
    return a[0] * b[1] - a[1] * b[0]


MU = 10.0 / 0.0254          # 1/m   (10 per inch)
LAMBDA = 0.55
DELTA_MAX = 0.34 * 0.0254   # m     (0.34 in)


def bolt_force_ratio(delta: float) -> float:
    """R/Rult for a bolt deformation delta [m] (C&K load-deformation curve)."""
    if delta <= 0.0:
        return 0.0
    return (1.0 - math.exp(-MU * delta)) ** LAMBDA


@dataclass
class ICRResult:
    C: float                       # bolt-group coefficient  Pu = C * Rult
    ic: Tuple[float, float]        # instantaneous centre (in-plane, m)
    bolt_forces: np.ndarray        # (n,2) R_i/Rult at Pu (dimensionless, same units as Rult)
    bolt_magnitudes: np.ndarray    # (n,)  |R_i|/Rult at Pu (<= 0.9815)
    bolt_deformations: np.ndarray  # (n,) delta_i [m]
    iterations: int
    unbalanced: float              # residual |F| of the equilibrium check
    elastic_C: Optional[float] = None
    converged: bool = True
    log: List[str] = field(default_factory=list)


def _elastic_ic(bolts: np.ndarray, pdir: np.ndarray, ppoint: np.ndarray):
    """Elastic instantaneous centre (Brandt 1982): a = (-Py*J/(n*M0), Px*J/(n*M0))."""
    cg = bolts.mean(axis=0)
    rel = bolts - cg
    J = float((rel ** 2).sum())
    m0 = float(cross2(ppoint - cg, pdir))            # CCW+, per unit load
    if abs(m0) < 1e-12:
        return None, cg, J, m0
    ax = -pdir[1] * J / (len(bolts) * m0)
    ay = pdir[0] * J / (len(bolts) * m0)
    return cg + np.array([ax, ay]), cg, J, m0


def _state(bolts: np.ndarray, ic: np.ndarray, pdir: np.ndarray, ppoint: np.ndarray,
           delta_max: float):
    """For a trial ICR: bolt geometry, force ratios, unit-load moment, Pu/Rult."""
    d = bolts - ic
    r = np.linalg.norm(d, axis=1)
    rmax = r.max()
    delta = delta_max * r / rmax
    f = np.array([bolt_force_ratio(x) for x in delta])          # R_i/Rult
    mp = float(cross2(ppoint - ic, pdir))                       # unit-load moment (CCW+)
    if abs(mp) < 1e-14:
        return d, r, delta, f, mp, 0.0, np.zeros(2)
    beta = float((f * r).sum()) / abs(mp)                       # Pu / Rult
    sense = -np.sign(mp)                                        # rotation sense
    t = sense * np.stack([-d[:, 1], d[:, 0]], axis=1) / r[:, None]   # unit bolt-force dirs
    bolt_vec = f[:, None] * t                                   # R_i/Rult vectors
    resid = pdir * beta + bolt_vec.sum(axis=0)                  # force equilibrium
    return d, r, delta, f, mp, beta, resid


def solve_icr(bolt_xy: Sequence[Tuple[float, float]],
              load_direction: Tuple[float, float],
              load_point: Tuple[float, float],
              delta_max: float = DELTA_MAX,
              tol: float = 1e-9, max_iter: int = 300) -> ICRResult:
    """Solve the C&K ultimate-strength problem for a bolt group.

    Parameters
    ----------
    bolt_xy        : in-plane bolt coordinates (any consistent length unit, m).
    load_direction : unit vector of the applied load (in plane).
    load_point     : any point on the load line of action (same unit).
    delta_max      : ultimate bolt deformation (same unit, default 0.34 in in m).

    Returns an :class:`ICRResult` with the coefficient C = Pu/Rult.
    """
    bolts = np.asarray(bolt_xy, dtype=float).reshape(-1, 2)
    pdir = np.asarray(load_direction, dtype=float)
    pdir = pdir / np.linalg.norm(pdir)
    ppoint = np.asarray(load_point, dtype=float)
    n = len(bolts)
    log: List[str] = []

    if n == 1:
        return ICRResult(C=1.0, ic=tuple(bolts[0]),
                         bolt_forces=np.array([-pdir]), bolt_magnitudes=np.array([1.0]),
                         bolt_deformations=np.array([delta_max]), iterations=0,
                         unbalanced=0.0, elastic_C=1.0, log=["single bolt: C = 1"])

    ic0, cg, J, m0 = _elastic_ic(bolts, pdir, ppoint)
    # elastic coefficient (Brandt): Ce = sum(d^2)/(|Mp|*dmax) about elastic IC
    elastic_C = None
    if ic0 is not None:
        de = bolts - ic0
        re = np.linalg.norm(de, axis=1)
        mpe = abs(float(cross2(ppoint - ic0, pdir)))
        elastic_C = float((de ** 2).sum() / (mpe * re.max()))
    if ic0 is None:
        # concentric load: uniform bolt forces
        log.append("concentric load: C = n (uniform bolt forces)")
        return ICRResult(C=float(n), ic=tuple(cg),
                         bolt_forces=np.tile(-pdir, (n, 1)),
                         bolt_magnitudes=np.ones(n),
                         bolt_deformations=np.full(n, delta_max),
                         iterations=0, unbalanced=0.0, elastic_C=float(n), log=log)

    ic = ic0.copy()
    beta = 0.0
    resid = np.zeros(2)
    it = 0
    step = 1.0
    for it in range(1, max_iter + 1):
        d, r, delta, f, mp, beta, resid = _state(bolts, ic, pdir, ppoint, delta_max)
        fnorm = float(np.linalg.norm(resid))
        log.append(f"iter {it:3d}  ic=({ic[0]:+.5f},{ic[1]:+.5f})  C={beta:.5f}  |F|={fnorm:.3e}")
        if fnorm < tol:
            break
        # numerical Jacobian of the equilibrium residual w.r.t. the ICR position
        jac = np.zeros((2, 2))
        h = max(1e-6, 1e-4 * max(1.0, float(np.linalg.norm(ic - cg))))
        for k in range(2):
            e = np.zeros(2)
            e[k] = h
            _, _, _, _, _, _, rp = _state(bolts, ic + e, pdir, ppoint, delta_max)
            _, _, _, _, _, _, rm = _state(bolts, ic - e, pdir, ppoint, delta_max)
            jac[:, k] = (rp - rm) / (2 * h)
        try:
            dx = np.linalg.solve(jac, -resid)
        except np.linalg.LinAlgError:
            dx = -resid * h
        # damped Newton (backtracking) to keep the iteration monotone
        acc = 1.0
        for _ in range(40):
            trial = ic + acc * dx
            _, _, _, _, _, b2, r2 = _state(bolts, trial, pdir, ppoint, delta_max)
            if b2 > 0 and np.linalg.norm(r2) < fnorm:
                break
            acc *= 0.5
        ic = ic + acc * dx
        step = acc
    else:  # pragma: no cover - non convergence
        log.append("WARNING: Newton iteration did not converge")

    d, r, delta, f, mp, beta, resid = _state(bolts, ic, pdir, ppoint, delta_max)
    sense = -np.sign(mp)
    t = sense * np.stack([-d[:, 1], d[:, 0]], axis=1) / r[:, None]
    # R_i / Rult at the ultimate load of the group.  The applied load at that
    # state is Pu = C * Rult, so the force in bolt i at any load P is
    #   F_i(P) = (R_i/Rult) * P/C   [same units as P]
    bolt_vec = f[:, None] * t
    converged = float(np.linalg.norm(resid)) < 1e-6 and beta > 0
    return ICRResult(C=float(beta), ic=(float(ic[0]), float(ic[1])),
                     bolt_forces=bolt_vec,
                     bolt_magnitudes=np.linalg.norm(bolt_vec, axis=1),
                     bolt_deformations=delta, iterations=it,
                     unbalanced=float(np.linalg.norm(resid)),
                     elastic_C=elastic_C, converged=converged, log=log)


def solve_from_eccentricity(bolt_xy: Sequence[Tuple[float, float]],
                            load_direction: Tuple[float, float],
                            eccentricity: float,
                            delta_max: float = DELTA_MAX) -> ICRResult:
    """Convenience wrapper: load acting at (signed) perpendicular distance
    `eccentricity` from the bolt-group centroid.  For a vertical load this is
    the classical horizontal eccentricity e of Crawford & Kulak (positive e =
    load line on the +x side of the centroid, producing a clockwise moment)."""
    pdir = np.asarray(load_direction, dtype=float)
    pdir = pdir / np.linalg.norm(pdir)
    bolts = np.asarray(bolt_xy, dtype=float).reshape(-1, 2)
    cg = bolts.mean(axis=0)
    n = np.array([-pdir[1], pdir[0]])        # rot90 CCW of the load direction
    ppoint = cg + eccentricity * n
    return solve_icr(bolt_xy, tuple(pdir), tuple(ppoint), delta_max)


def elastic_bolt_forces(bolt_xy: Sequence[Tuple[float, float]],
                        load_direction: Tuple[float, float],
                        eccentricity: float):
    """Classical elastic (vector) method: uniform shear + torsion about the
    centroid, J = sum(r^2).  Returns (n,2) bolt-force vectors per unit load."""
    bolts = np.asarray(bolt_xy, dtype=float).reshape(-1, 2)
    pdir = np.asarray(load_direction, dtype=float)
    pdir = pdir / np.linalg.norm(pdir)
    cg = bolts.mean(axis=0)
    rel = bolts - cg
    J = float((rel ** 2).sum())
    m0 = -eccentricity                        # moment per unit load (CCW+)
    n = len(bolts)
    forces = np.tile(-pdir, (n, 1)) / n       # reaction of bolts on ... per unit P
    if J > 0:
        forces += (m0 / J) * np.stack([-rel[:, 1], rel[:, 0]], axis=1) * -1.0
    return forces
