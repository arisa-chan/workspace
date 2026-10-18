"""
OpenSeesPy fallback analysis for an eccentrically loaded bolted lap joint.

This module is the **fallback** used when the IDEA StatiCa Connection REST API
does not return usable results (solver failure, singularity, dropped bolt
group, ...).  It performs a finite-element analysis of the same connection and
reports the same quantities the IDEA workflow reports:

* the force in **every bolt** (in-plane shear components and resultant),
* the **capacity** of the connection (the applied load at which the most
  heavily loaded bolt reaches its ultimate shear resistance).

Model
-----
* both plates are meshed with 8-node solid bricks (``stdBrick``) in the
  connection plane (X-Z), one element through the thickness, separated by a
  small clearance;
* every bolt is a ``twoNodeLink`` between the two plates at the bolt position
  with three elastic springs: two in-plane **shear** springs (global X and Z)
  and one stiff **axial** spring (global Y);
* the supporting plate is fixed along its perimeter (the surrounding
  structure); the load is applied to the loaded plate at the true load point,
  so the eccentricity is carried by the plate itself.

Solution scheme
---------------
The plate response is linear and the only nonlinearity is the bolt
load-deformation law ``R = Rult (1 - exp(-10 d))^0.55``.  The model is therefore
solved by a **secant iteration** on the bolt stiffnesses, with one linear
OpenSees solve per iteration:

1. solve the FE model at load ``P`` with the current shear stiffnesses;
2. read the relative displacement ``d_i`` of every bolt and its resistance
   ``R_i = R(d_i)``;
3. scale the load by ``Rult / max R_i`` (so the critical bolt is at its
   ultimate) and update ``k_i = R_i / d_i``;
4. repeat until the load no longer changes.

This avoids the convergence problems of a full Newton iteration on a
badly-conditioned spring-shell model while still using the finite-element
results (plate flexibility included) for both the forces and the capacity.

The module runs standalone (``python -m eccbolt.fe_fallback model.json out.json``)
so that it can be executed by an interpreter that is compatible with the
installed OpenSeesPy binary (the Windows wheel is built for one Python minor
version only).  :mod:`eccbolt.fe_runner` takes care of that.
"""
from __future__ import annotations

import json
import math
import sys
from typing import Dict, List, Tuple

MU = 10.0 / 0.0254          # 10 per inch -> 1/m
LAMBDA = 0.55
CK_DELTA_MAX = 0.34 * 0.0254


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def bolt_ratio(delta: float) -> float:
    """R/Rult on the Crawford & Kulak curve."""
    if delta <= 0.0:
        return 0.0
    return (1.0 - math.exp(-MU * delta)) ** LAMBDA


def _grid_lines(lo: float, hi: float, extra: List[float], target: float
                ) -> List[float]:
    """1-D mesh lines between lo..hi, forced through every `extra` coordinate."""
    n = max(2, int(math.ceil((hi - lo) / max(target, 1e-3))))
    lines = {lo + (hi - lo) * i / n for i in range(n + 1)}
    for e in extra:
        if lo < e < hi:
            lines.add(float(e))
    return sorted(lines)


class _FeModel:
    """The OpenSees model (built once, re-solved with different stiffnesses)."""

    def __init__(self, model: Dict):
        import openseespy.opensees as ops
        self.ops = ops
        self.model = model
        pa, pb = model["plate_a"], model["plate_b"]
        self.bolts = [(float(x), float(z)) for x, z in model["bolts"]]
        t_a, t_b = float(pa["t"]), float(pb["t"])
        gap = float(model.get("gap", 0.0002))
        self.E = float(model.get("E", 210.0e9))
        self.nu = float(model.get("nu", 0.3))
        self.y_a0, self.y_a1 = -t_a / 2, t_a / 2
        self.y_b0, self.y_b1 = t_a / 2 + gap, t_a / 2 + gap + t_b
        self.load = model["load"]
        self.target = float(model.get("mesh", max(pa["w"], pa["h"]) / 10.0))
        self.pa, self.pb = pa, pb

    # -- geometry -------------------------------------------------------
    def _outline(self, plate):
        cx, cy = float(plate.get("cx", 0.0)), float(plate.get("cy", 0.0))
        w, hgt = float(plate["w"]), float(plate["h"])
        return cx - w / 2, cx + w / 2, cy - hgt / 2, cy + hgt / 2

    def build(self) -> None:
        ops = self.ops
        bolts = self.bolts
        ax0, ax1, az0, az1 = self._outline(self.pa)
        bx0, bx1, bz0, bz1 = self._outline(self.pb)
        xs_a = _grid_lines(ax0, ax1, [b[0] for b in bolts], self.target)
        zs_a = _grid_lines(az0, az1, [b[1] for b in bolts], self.target)
        xs_b = _grid_lines(bx0, bx1, [b[0] for b in bolts], self.target)
        zs_b = _grid_lines(bz0, bz1, [b[1] for b in bolts], self.target)

        ops.wipe()
        ops.model("basic", "-ndm", 3, "-ndf", 3)
        ops.nDMaterial("ElasticIsotropic", 1, self.E, self.nu, 0.0)

        self.next_node = [1]
        self.next_elem = [1]

        def add_node(x, y, z):
            tag = self.next_node[0]
            self.next_node[0] += 1
            ops.node(tag, float(x), float(y), float(z))
            return tag

        def mesh(xs, zs, y0, y1):
            lo, up = {}, {}
            for z in zs:
                for x in xs:
                    lo[(x, z)] = add_node(x, y0, z)
                    up[(x, z)] = add_node(x, y1, z)
            for iz in range(len(zs) - 1):
                for ix in range(len(xs) - 1):
                    n1 = lo[(xs[ix], zs[iz])]
                    n2 = lo[(xs[ix + 1], zs[iz])]
                    n3 = lo[(xs[ix + 1], zs[iz + 1])]
                    n4 = lo[(xs[ix], zs[iz + 1])]
                    n5 = up[(xs[ix], zs[iz])]
                    n6 = up[(xs[ix + 1], zs[iz])]
                    n7 = up[(xs[ix + 1], zs[iz + 1])]
                    n8 = up[(xs[ix], zs[iz + 1])]
                    tag = self.next_elem[0]
                    self.next_elem[0] += 1
                    ops.element("stdBrick", tag, n1, n2, n3, n4,
                                n5, n6, n7, n8, 1)
            return lo, up

        lo_a, up_a = mesh(xs_a, zs_a, self.y_a0, self.y_a1)
        lo_b, up_b = mesh(xs_b, zs_b, self.y_b0, self.y_b1)

        # bolt springs: elastic materials, redefined between iterations
        rult = float(self.model["bolt_rult"])
        delta_max = float(self.model.get("delta_max", CK_DELTA_MAX))
        k0 = rult / max(delta_max, 1e-9)
        self.mat_axial = 20
        self.mat_shear_tags = [21 + 10 * i for i in range(len(bolts))]
        ops.uniaxialMaterial("Elastic", self.mat_axial, 5.0 * k0)
        for tag in self.mat_shear_tags:
            ops.uniaxialMaterial("Elastic", tag, k0)
        self.bolt_nodes: List[Tuple[int, int]] = []
        self.bolt_elems: List[int] = []
        for i, (bx, bz) in enumerate(bolts):
            na = up_a[(bx, bz)]
            nb = lo_b[(bx, bz)]
            tag = self.next_elem[0]
            self.next_elem[0] += 1
            # local directions 1,2,3 are global X,Y,Z: shear, axial, shear
            ops.element("twoNodeLink", tag, na, nb,
                        "-mat", self.mat_shear_tags[i], self.mat_axial,
                        self.mat_shear_tags[i], "-dir", 1, 2, 3)
            self.bolt_elems.append(tag)
            self.bolt_nodes.append((na, nb))

        for layer in (lo_a, up_a):
            for (x, z), nd in layer.items():
                if x in (xs_a[0], xs_a[-1]) or z in (zs_a[0], zs_a[-1]):
                    ops.fix(nd, 1, 1, 1)

        e, h = float(self.load.get("e", 0.0)), float(self.load.get("h", 0.0))
        self.load_node = up_b[min(up_b, key=lambda k: (k[0] - e) ** 2
                                  + (k[1] - h) ** 2)]
        ops.timeSeries("Linear", 1)
        ops.pattern("Plain", 1, 1)
        theta = math.radians(float(self.load.get("theta", 0.0)))
        ops.load(self.load_node, math.sin(theta), 0.0, -math.cos(theta))

        ops.system("BandGeneral")
        ops.numberer("RCM")
        ops.constraints("Plain")
        ops.test("NormDispIncr", 1e-12, 5, 0)
        ops.algorithm("Linear")
        ops.integrator("LoadControl", 1.0)
        ops.analysis("Static")

    # -- solve ----------------------------------------------------------
    def solve(self, lam: float, k_shear: List[float], k_axial: float
              ) -> Tuple[List[Tuple[float, float]], List[float]]:
        """Solve at load level `lam` with the given shear stiffnesses."""
        ops = self.ops
        # OpenSeesPy refuses to redefine an existing material tag, so every
        # iteration gets fresh tags and rebuilt link elements
        tags = []
        for k in k_shear:
            tag = self.next_mat[0]
            self.next_mat[0] += 1
            ops.uniaxialMaterial("Elastic", tag, float(k))
            tags.append(tag)
        axial_tag = self.next_mat[0]
        self.next_mat[0] += 1
        ops.uniaxialMaterial("Elastic", axial_tag, float(k_axial))
        for i, elem in enumerate(self.bolt_elems):
            na, nb = self.bolt_nodes[i]
            ops.remove("element", elem)
            ops.element("twoNodeLink", elem, na, nb,
                        "-mat", tags[i], axial_tag, tags[i], "-dir", 1, 2, 3)
        ops.integrator("LoadControl", 1.0)
        ops.analyze(1, lam)
        forces, deltas = [], []
        for i, tag in enumerate(self.bolt_elems):
            r = ops.eleResponse(tag, "force")
            forces.append((float(r[0]), float(r[2])))
            na, nb = self.bolt_nodes[i]
            da, db = ops.nodeDisp(na), ops.nodeDisp(nb)
            deltas.append(math.hypot(da[0] - db[0], da[2] - db[2]))
        return forces, deltas


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def solve(model: Dict, verbose: bool = False) -> Dict:
    """Secant-iterate the bolt stiffnesses and return forces + capacity."""
    fem = _FeModel(model)
    fem.build()

    rult = float(model["bolt_rult"])
    delta_max = float(model.get("delta_max", CK_DELTA_MAX))
    r_ult = rult * bolt_ratio(delta_max)          # resistance at delta_max
    n = len(fem.bolts)
    k0 = rult / max(delta_max, 1e-9)              # secant stiffness scale
    k_axial = 5.0 * k0
    lam = float(model.get("capacity_hint", 0.0)) or 100.0e3

    k = [k0] * n
    forces: List[Tuple[float, float]] = []
    history = []
    for it in range(25):
        forces, deltas = fem.solve(lam, k, k_axial)
        resistances = [rult * bolt_ratio(d) for d in deltas]
        fmax = max(resistances) if resistances else 0.0
        if verbose:
            print(f"  iter {it:2d}: P={lam/1e3:8.1f} kN  "
                  f"max R={fmax/1e3:7.2f} kN  "
                  f"dmax={max(deltas)*1e3:6.3f} mm", flush=True)
        history.append(lam)
        if fmax <= 0.0:
            return dict(ok=False, message="no bolt force obtained",
                        capacity=None, bolts=[])
        scale = r_ult / fmax
        new_lam = lam * scale
        # new secant stiffness at the (scaled) deformation of every bolt
        k = [max(rult * bolt_ratio(d * scale) / (d * scale), 1e-3 * k0)
             if d > 0 else k0 for d in deltas]
        if abs(scale - 1.0) < 2e-3:
            lam = new_lam
            break
        lam = new_lam

    forces, deltas = fem.solve(lam, k, k_axial)
    mags = [math.hypot(f[0], f[1]) for f in forces]
    return dict(
        ok=True,
        capacity=lam,
        bolt_ult=r_ult,
        bolts=[dict(name=f"B{i+1}", x=bx, z=bz,
                    fx=forces[i][0], fz=forces[i][1], magnitude=mags[i],
                    unity=mags[i] / r_ult if r_ult else 0.0)
               for i, (bx, bz) in enumerate(fem.bolts)],
        max_deformation=max(deltas) if deltas else 0.0,
        iterations=len(history),
        message="ok")


# ---------------------------------------------------------------------------
# CLI (called by eccbolt.fe_runner in a compatible interpreter)
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print("usage: python -m eccbolt.fe_fallback model.json result.json")
        return 2
    with open(argv[0], encoding="utf-8") as fh:
        model = json.load(fh)
    try:
        result = solve(model, verbose="--verbose" in argv)
    except Exception as exc:                       # keep the runner informed
        import traceback
        result = dict(ok=False, message=f"{type(exc).__name__}: {exc}",
                      traceback=traceback.format_exc(),
                      capacity=None, bolts=[])
    with open(argv[1], "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
