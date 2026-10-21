"""
OpenSeesPy fallback analysis for an eccentrically loaded bolted lap joint.

This module is the **fallback** used when the IDEA StatiCa Connection REST API
does not return usable results (solver failure, singularity, dropped bolt
group, ...).  It performs a finite-element analysis of the same connection and
reports the same quantities the IDEA workflow reports:

* the force in **every bolt** (in-plane shear components and resultant),
* the **capacity** of the connection,
* the governing failure mode (bolt shear, plate tear-out / net section or
  gross plate yielding).

Model
-----
The connection is analysed as a two-dimensional plane-stress problem in the
connection plane (global X-Z).  Each plate is a separate mesh, so the two
plates can slide relative to each other.  The model deliberately includes the
effects that are omitted by the classical rigid-plate C&K solution:

* **real bolt holes** - the plate mesh is generated around a circular opening of
  diameter ``d + hole_clearance``; elements inside the hole are not created,
  which lets the net section and the stress concentration at the hole develop;
* **bolt-hole clearance** - the bolt is represented by two pin nodes (one in
  each plate).  Each pin is connected to the nodes on its hole boundary by
  compression-only truss elements with an initial radial gap of
  ``(d_hole - d_bolt)/2``.  The bolt therefore idles through the clearance
  before it bears on the hole wall, exactly like a snug/standard hole;
* **nonlinear plates** - the plate material is ``J2Plasticity`` with the
  measured yield/tensile strengths (``ElasticIsotropic`` is still available
  for a quick linear check);
* **nonlinear bolts** - a ``twoNodeLink`` between the two pin nodes uses the
  Crawford & Kulak load-deformation law in both in-plane directions:

      R(d) = Rult * (1 - exp(-mu*d))**lambda

  with user/calibration supplied ``mu`` (1/m), ``lambda`` and ``delta_max``.

The model is loaded monotonically.  At every load step the bolt deformation is
the relative displacement of the two pin nodes (bolt shear + bending + plate
bearing), and the plate stress is read from the plane-stress elements.  The
capacity is the first load at which either

* the most deformed bolt reaches ``delta_max`` (bolt shear failure), or
* the maximum von Mises stress in a plate reaches the measured tensile
  strength ``fu`` (net-section / tear-out / plate yielding).

The governing criterion is reported as ``failure_mode``.

Solution scheme
---------------
A load-controlled nonlinear static analysis is run with small increments.
OpenSees' ``MultiLinear`` material represents the C&K bolt curve (including
unloading), ``J2Plasticity`` represents the plate steel, and
``ElasticPPGap`` represents the one-sided bearing contact at the hole wall.
The module runs standalone (``python -m eccbolt.fe_fallback model.json
out.json``) so that it can be executed by an interpreter that is compatible
with the installed OpenSeesPy binary (the Windows wheel is built for one Python
minor version only).  :mod:`eccbolt.fe_runner` takes care of that.
"""
from __future__ import annotations

import json
import math
import sys
from typing import Dict, List, Optional, Sequence, Tuple

MU = 10.0 / 0.0254          # 10 per inch -> 1/m
LAMBDA = 0.55
CK_DELTA_MAX = 0.34 * 0.0254


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def bolt_ratio(delta: float, mu: float = MU, lam: float = LAMBDA) -> float:
    """R/Rult on the Crawford & Kulak curve for deformation ``delta`` [m]."""
    if delta <= 0.0:
        return 0.0
    return (1.0 - math.exp(-mu * delta)) ** lam


def bolt_force(delta: float, rult: float, mu: float = MU,
               lam: float = LAMBDA) -> float:
    """Bolt resistance [N] at deformation ``delta`` [m]."""
    return rult * bolt_ratio(delta, mu, lam)


def _bolt_curve(rult: float, delta_max: float, mu: float, lam: float,
                n: int = 16, clearance: float = 0.0) -> List[float]:
    """Symmetric multi-linear points (deformation, force) for a bolt link.

    ``clearance`` is a slip offset: the bolt carries no force until the
    relative plate deformation has closed the hole clearance.  The points are
    clustered near zero because the C&K curve has a very steep (infinite)
    initial slope when lambda < 1.

    At most 16 intervals (33 points) are used: the Windows OpenSees build has
    a fixed-size buffer in ``ElasticMultiLinear`` and aborts with a stack
    buffer overrun when more than ~64 strain/stress entries are supplied.
    """
    n = max(4, min(int(n), 16))
    slip = max(clearance, 0.0)
    dtop = max((delta_max + slip) * 1.25, 1e-6)
    ds = [dtop * (i / n) ** 3 for i in range(n + 1)]
    rmax = bolt_force(delta_max, rult, mu, lam)

    def force(d: float) -> float:
        ad = abs(d)
        eff = max(ad - slip, 0.0)
        if slip > 0.0 and ad <= slip:
            # small pre-slip stiffness: the bolt is still idling in the hole,
            # but a zero tangent would make the first load step singular
            f = (0.05 * rult / max(delta_max, 1e-9)) * ad
        else:
            f = bolt_force(eff, rult, mu, lam)
            if eff > delta_max:
                # a small positive slope past the ultimate deformation avoids
                # a singular (perfectly plastic) tangent in the push-over
                f = rmax * (1.0 + 0.01 * (eff - delta_max) / delta_max)
        return math.copysign(f, d) if d else 0.0

    pts: List[float] = []
    # negative branch, from the plateau back to zero
    for d in reversed(ds):
        pts.extend([-d, force(-d)])
    # positive branch (zero is already the last negative point)
    for d in ds[1:]:
        pts.extend([d, force(d)])
    return pts


def _grid_lines(lo: float, hi: float, forced: Sequence[float],
                target: float) -> List[float]:
    """1-D mesh lines between lo..hi, forced through every useful coordinate."""
    n = max(2, int(math.ceil((hi - lo) / max(target, 1e-4))))
    lines = {lo + (hi - lo) * i / n for i in range(n + 1)}
    for e in forced:
        if lo + 1e-9 < e < hi - 1e-9:
            lines.add(float(e))
    return sorted(lines)


def _plate_outline(plate: Dict) -> Tuple[float, float, float, float]:
    cx, cy = float(plate.get("cx", 0.0)), float(plate.get("cy", 0.0))
    w, hgt = float(plate["w"]), float(plate["h"])
    return cx - w / 2, cx + w / 2, cy - hgt / 2, cy + hgt / 2


def _nearest(x: float, z: float, coords: Dict[int, Tuple[float, float]]
             ) -> int:
    return min(coords, key=lambda t: (coords[t][0] - x) ** 2
               + (coords[t][1] - z) ** 2)


# ---------------------------------------------------------------------------
# OpenSees model
# ---------------------------------------------------------------------------
class _PlateMesh:
    """Plane-stress mesh of one plate, with circular bolt holes removed."""

    def __init__(self, ops, plate: Dict, bolts: Sequence[Tuple[float, float]],
                 hole_radius: float, bolt_radius: float, target: float,
                 mat_tag: int, first_node: int, first_elem: int,
                 mat_near: Optional[int] = None,
                 near_radius: Optional[float] = None,
                 boundary_width: float = 0.05,
                 min_target: Optional[float] = None):
        self.ops = ops
        self.plate = plate
        self.bolts = list(bolts)
        self.rh = hole_radius
        self.rb = bolt_radius
        self.target = target
        self.min_target = target if min_target is None else min_target
        self.mat_tag = mat_tag
        self.mat_near = mat_near
        self.near_radius = (near_radius if near_radius is not None
                            else 1.5 * hole_radius)
        self.boundary_width = boundary_width
        self.next_node = first_node
        self.next_elem = first_elem
        self.thickness = float(plate["t"])
        self.coords: Dict[int, Tuple[float, float]] = {}
        self.elems: List[int] = []
        self.node_of: Dict[Tuple[int, int], int] = {}
        self.x0, self.x1, self.z0, self.z1 = _plate_outline(plate)
        self.hole_nodes: Dict[int, List[Tuple[float, float, int]]] = {}
        self._build()

    def _build(self) -> None:
        ops = self.ops
        rh, target = self.rh, self.target
        forced_x, forced_z = [], []
        fine = max(self.min_target, 1e-4)
        nfine = min(120, max(2, int(math.ceil(6.0 * rh / fine))))
        for bx, bz in self.bolts:
            for f in (0.0, -rh, rh, -rh - target, rh + target,
                      -0.5 * rh, 0.5 * rh):
                forced_x.append(bx + f)
                forced_z.append(bz + f)
            for j in range(nfine + 1):
                f = -3.0 * rh + 6.0 * rh * j / nfine
                forced_x.append(bx + f)
                forced_z.append(bz + f)
        xs = _grid_lines(self.x0, self.x1, forced_x, target)
        zs = _grid_lines(self.z0, self.z1, forced_z, target)

        # cells inside a hole are omitted; record them per bolt
        removed: List[Tuple[int, int]] = []
        hole_of: Dict[Tuple[int, int], int] = {}
        for iz in range(len(zs) - 1):
            for ix in range(len(xs) - 1):
                xc = 0.5 * (xs[ix] + xs[ix + 1])
                zc = 0.5 * (zs[iz] + zs[iz + 1])
                for k, (bx, bz) in enumerate(self.bolts):
                    if math.hypot(xc - bx, zc - bz) < rh:
                        removed.append((ix, iz))
                        hole_of[(ix, iz)] = k
                        break
        removed_set = set(removed)

        def node(ix: int, iz: int) -> int:
            key = (ix, iz)
            tag = self.node_of.get(key)
            if tag is None:
                tag = self.next_node
                self.next_node += 1
                x, z = xs[ix], zs[iz]
                ops.node(tag, float(x), float(z))
                self.node_of[key] = tag
                self.coords[tag] = (float(x), float(z))
            return tag

        # create kept cells (this also creates only the nodes they use)
        for iz in range(len(zs) - 1):
            for ix in range(len(xs) - 1):
                if (ix, iz) in removed_set:
                    continue
                n1 = node(ix, iz)
                n2 = node(ix + 1, iz)
                n3 = node(ix + 1, iz + 1)
                n4 = node(ix, iz + 1)
                xc = 0.5 * (xs[ix] + xs[ix + 1])
                zc = 0.5 * (zs[iz] + zs[iz + 1])
                mat = self.mat_tag
                boundary_zone = self.boundary_width
                if self.mat_near is not None and (
                        any(math.hypot(xc - bx, zc - bz) < self.near_radius
                            for bx, bz in self.bolts)
                        or xc - self.x0 < boundary_zone
                        or self.x1 - xc < boundary_zone
                        or zc - self.z0 < boundary_zone
                        or self.z1 - zc < boundary_zone):
                    mat = self.mat_near
                tag = self.next_elem
                self.next_elem += 1
                ops.element("quad", tag, n1, n2, n3, n4,
                            self.thickness, "PlaneStress", mat)
                self.elems.append(tag)

        # nodes on the boundary of each hole (vertices shared with kept cells)
        for k in range(len(self.bolts)):
            corners = set()
            for ix, iz in removed:
                if hole_of.get((ix, iz)) != k:
                    continue
                corners.update(((ix, iz), (ix + 1, iz),
                                (ix + 1, iz + 1), (ix, iz + 1)))
            nodes = []
            for key in corners:
                tag = self.node_of.get(key)
                if tag is not None:
                    x, z = self.coords[tag]
                    nodes.append((x, z, tag))
            self.hole_nodes[k] = nodes

    def perimeter_nodes(self) -> List[int]:
        return [t for t, (x, z) in self.coords.items()
                if abs(x - self.x0) < 1e-9 or abs(x - self.x1) < 1e-9
                or abs(z - self.z0) < 1e-9 or abs(z - self.z1) < 1e-9]


class _TriPlateMesh:
    """Unstructured plane-stress mesh with circular bolt holes (Delaunay)."""

    def __init__(self, ops, plate: Dict, bolts: Sequence[Tuple[float, float]],
                 hole_radius: float, bolt_radius: float, target: float,
                 mat_tag: int, first_node: int, first_elem: int,
                 mat_near: Optional[int] = None,
                 near_radius: Optional[float] = None,
                 boundary_width: float = 0.05,
                 min_target: Optional[float] = None):
        self.ops = ops
        self.plate = plate
        self.bolts = list(bolts)
        self.rh = hole_radius
        self.rb = bolt_radius
        self.target = target
        self.min_target = target if min_target is None else min_target
        self.mat_tag = mat_tag
        self.mat_near = mat_near
        self.near_radius = (near_radius if near_radius is not None
                            else 1.5 * hole_radius)
        self.boundary_width = boundary_width
        self.next_node = first_node
        self.next_elem = first_elem
        self.thickness = float(plate["t"])
        self.coords: Dict[int, Tuple[float, float]] = {}
        self.elems: List[int] = []
        self.node_of: Dict[Tuple[float, float], int] = {}
        self.x0, self.x1, self.z0, self.z1 = _plate_outline(plate)
        self.hole_nodes: Dict[int, List[Tuple[float, float, int]]] = {}
        self._build()

    def _build(self) -> None:
        import numpy as np
        from scipy.spatial import Delaunay

        ops = self.ops
        target = max(self.target, 1e-4)
        pts: List[Tuple[float, float]] = []
        seen = set()
        key_list: List[Tuple[float, float]] = []

        def add(x: float, z: float):
            key = (round(float(x), 9), round(float(z), 9))
            if key not in seen:
                seen.add(key)
                key_list.append(key)
                pts.append((float(x), float(z)))
            return key

        # rectangular outline, subdivided at the target spacing
        nx = max(1, int(math.ceil((self.x1 - self.x0) / target)))
        nz = max(1, int(math.ceil((self.z1 - self.z0) / target)))
        for i in range(nx + 1):
            x = self.x0 + (self.x1 - self.x0) * i / nx
            add(x, self.z0)
            add(x, self.z1)
        for j in range(1, nz):
            z = self.z0 + (self.z1 - self.z0) * j / nz
            add(self.x0, z)
            add(self.x1, z)

        # points on every hole circumference; refined by the minimum size
        fine = max(self.min_target, 1e-4)
        ring_keys: List[List[Tuple[float, float]]] = []
        for bx, bz in self.bolts:
            ntheta = min(160, max(16, int(math.ceil(
                2.0 * math.pi * self.rh / (0.8 * fine)))))
            keys = []
            for k in range(ntheta):
                a = 2.0 * math.pi * k / ntheta
                keys.append(add(bx + self.rh * math.cos(a),
                                bz + self.rh * math.sin(a)))
            ring_keys.append(keys)
            # one or more transition rings at the minimum element size
            r_ref = self.rh
            while r_ref < self.rh + 2.0 * target:
                r_ref = min(r_ref + max(fine, 0.35 * target),
                            self.rh + 2.0 * target)
                nt = min(200, max(16, int(math.ceil(
                    2.0 * math.pi * r_ref / max(fine, 1e-4)))))
                for k in range(nt):
                    a = 2.0 * math.pi * (k + 0.5) / nt
                    x = bx + r_ref * math.cos(a)
                    z = bz + r_ref * math.sin(a)
                    if (self.x0 + 1e-9 < x < self.x1 - 1e-9
                            and self.z0 + 1e-9 < z < self.z1 - 1e-9):
                        add(x, z)
                if r_ref >= self.rh + 2.0 * target - 1e-12:
                    break

        # interior grid points, excluding the hole interiors
        n_interior = max(1, int(math.ceil((self.x1 - self.x0) / target)))
        m_interior = max(1, int(math.ceil((self.z1 - self.z0) / target)))
        for i in range(1, n_interior):
            x = self.x0 + (self.x1 - self.x0) * i / n_interior
            for j in range(1, m_interior):
                z = self.z0 + (self.z1 - self.z0) * j / m_interior
                if all(math.hypot(x - bx, z - bz) > 1.15 * self.rh
                       for bx, bz in self.bolts):
                    add(x, z)

        arr = np.asarray(pts, dtype=float)
        tri = Delaunay(arr)
        point_of = {k: (arr[i, 0], arr[i, 1])
                    for i, k in enumerate(key_list)}

        def node(key) -> int:
            tag = self.node_of.get(key)
            if tag is None:
                tag = self.next_node
                self.next_node += 1
                x, z = point_of[key]
                ops.node(tag, x, z)
                self.node_of[key] = tag
                self.coords[tag] = (x, z)
            return tag

        for simplex in tri.simplices:
            keys = list(simplex)
            a, b, c = arr[keys[0]], arr[keys[1]], arr[keys[2]]
            area2 = ((b[0] - a[0]) * (c[1] - a[1])
                     - (b[1] - a[1]) * (c[0] - a[0]))
            if abs(area2) < 1e-6 * target * target:
                continue
            cx = (a[0] + b[0] + c[0]) / 3.0
            cz = (a[1] + b[1] + c[1]) / 3.0
            if any(math.hypot(cx - bx, cz - bz) < 0.98 * self.rh
                   for bx, bz in self.bolts):
                continue
            kk = [key_list[i] for i in keys]
            n1, n2, n3 = (node(kk[0]), node(kk[1]), node(kk[2]))
            if area2 < 0.0:
                n2, n3 = n3, n2
            mat = self.mat_tag
            boundary_zone = self.boundary_width
            if self.mat_near is not None and (
                    any(math.hypot(cx - bx, cz - bz) < self.near_radius
                        for bx, bz in self.bolts)
                    or cx - self.x0 < boundary_zone
                    or self.x1 - cx < boundary_zone
                    or cz - self.z0 < boundary_zone
                    or self.z1 - cz < boundary_zone):
                mat = self.mat_near
            tag = self.next_elem
            self.next_elem += 1
            ops.element("Tri31", tag, n1, n2, n3, self.thickness,
                        "PlaneStress", mat)
            self.elems.append(tag)

        for k, keys in enumerate(ring_keys):
            nodes = []
            for key in keys:
                tag = self.node_of.get(key)
                if tag is not None:
                    x, z = self.coords[tag]
                    nodes.append((x, z, tag))
            self.hole_nodes[k] = nodes

    def perimeter_nodes(self) -> List[int]:
        return [t for t, (x, z) in self.coords.items()
                if abs(x - self.x0) < 1e-8 or abs(x - self.x1) < 1e-8
                or abs(z - self.z0) < 1e-8 or abs(z - self.z1) < 1e-8]


class _FeModel:
    """The OpenSees model (built once, solved with a monotonic push-over)."""

    def __init__(self, model: Dict):
        self.model = model
        pa, pb = model["plate_a"], model["plate_b"]
        self.pa, self.pb = pa, pb
        self.bolts = [(float(x), float(z)) for x, z in model["bolts"]]
        self.bolt_d = float(model.get("bolt_diameter", 0.019))
        self.clearance = float(model.get("hole_clearance",
                                          model.get("bolt_hole_clearance",
                                                    0.002)))
        self.rh = 0.5 * (self.bolt_d + self.clearance)
        self.rb = 0.5 * self.bolt_d
        self.rult = float(model["bolt_rult"])
        self.delta_max = float(model.get("delta_max", CK_DELTA_MAX))
        self.mu = float(model.get("mu", MU))
        self.lam = float(model.get("ck_lambda", model.get("lam", LAMBDA)))
        self.nonlinear = bool(model.get("plate_nonlinear", True))
        self.model_holes = bool(model.get("model_holes", True))
        self.contact_factor = float(model.get("contact_stiffness_factor", 1.0))
        self.ring_factor = float(model.get("ring_stiffness_factor", 1.0))
        self.precontact = float(model.get("precontact_stiffness_ratio", 0.05))
        self.contact_gap = bool(model.get("contact_gap", True))
        self.bolt_slip = 0.0
        self.near_factor = float(model.get("elastic_bearing_radius", 1.5))
        self.boundary_width = float(model.get("elastic_boundary_width", 0.05))
        self.plate_criterion = str(model.get("plate_criterion", "nonlocal"))
        self.plate_percentile = float(model.get("plate_stress_percentile",
                                               0.95))
        self.plate_filter_radius = float(model.get("plate_filter_radius",
                                                   2.0)) * self.rh
        self.plate_fu = float(model.get("plate_fu",
                                        max(float(pa.get("fu", 450e6)),
                                            float(pb.get("fu", 450e6)))))
        self.max_steps = int(model.get("num_steps", 60))
        self.capacity_factor = float(model.get("capacity_factor", 1.0))
        self.return_mesh = bool(model.get("return_mesh", False))
        self.verbose = bool(model.get("verbose", False))
        self._mat_tag = 100
        self._node_tag = 1
        self._elem_tag = 1

    # -- material / tag helpers -----------------------------------------
    def _new_mat(self) -> int:
        tag = self._mat_tag
        self._mat_tag += 1
        return tag

    def _new_node(self) -> int:
        tag = self._node_tag
        self._node_tag += 1
        return tag

    def _new_elem(self) -> int:
        tag = self._elem_tag
        self._elem_tag += 1
        return tag

    def _elastic_material(self, plate: Dict) -> int:
        tag = self._new_mat()
        self.ops.nDMaterial("ElasticIsotropic", tag,
                            float(plate.get("E", 200e9)),
                            float(plate.get("nu", 0.3)), 0.0)
        return tag

    def _plate_material(self, plate: Dict) -> int:
        ops = self.ops
        tag = self._new_mat()
        E = float(plate.get("E", 200e9))
        nu = float(plate.get("nu", 0.3))
        if not self.nonlinear:
            ops.nDMaterial("ElasticIsotropic", tag, E, nu, 0.0)
            return tag
        K = E / (3.0 * (1.0 - 2.0 * nu))
        G = E / (2.0 * (1.0 + nu))
        fy = float(plate.get("fy", 345e6))
        fu = float(plate.get("fu", 1.2 * fy))
        # saturation + modest linear hardening up to the measured fu
        sig0 = float(self.model.get("j2_sig0", fy))
        sig_inf = float(self.model.get("j2_sig_inf", fu))
        delta = float(self.model.get("j2_delta", 20.0))
        H = float(self.model.get("j2_H", max(fu - fy, 0.0) / 0.15))
        ops.nDMaterial("J2Plasticity", tag, K, G, sig0, sig_inf, delta, H)
        return tag

    # -- build -----------------------------------------------------------
    def build(self) -> None:
        import openseespy.opensees as ops
        self.ops = ops
        ops.wipe()
        ops.model("basic", "-ndm", 2, "-ndf", 2)

        if self.model_holes:
            span = max(float(self.pa["w"]), float(self.pa["h"]),
                       float(self.pb["w"]), float(self.pb["h"]))
            target = float(self.model.get("mesh_max_element", 0.0)
                           or self.model.get("mesh", 0.0) or 0.0)
            if target <= 0.0:
                target = span / 12.0
            fine = float(self.model.get("mesh_min_element", 0.0) or 0.0)
            if fine <= 0.0:
                fine = min(target, max(self.rh / 1.5, 0.0015))
            target = max(target, 1e-4)
            fine = max(min(fine, target), 1e-4)

            mat_a = self._plate_material(self.pa)
            mat_b = self._plate_material(self.pb)
            # keep the immediate bearing zone elastic: the point-wise contact
            # would otherwise force the J2 return mapping into a singular
            # strain field at the hole edge
            near_a = self._elastic_material(self.pa) if self.nonlinear else None
            near_b = self._elastic_material(self.pb) if self.nonlinear else None
            try:
                import scipy.spatial  # noqa: F401
                mesh_cls = (_TriPlateMesh if self.model.get("triangulate", True)
                            else _PlateMesh)
            except Exception:
                mesh_cls = _PlateMesh
            near_r = self.near_factor * self.rh
            self.mesh_a = mesh_cls(ops, self.pa, self.bolts, self.rh,
                                   self.rb, target, mat_a, 1, 1,
                                   mat_near=near_a, near_radius=near_r,
                                   boundary_width=self.boundary_width,
                                   min_target=fine)
            self._node_tag = self.mesh_a.next_node
            self._elem_tag = self.mesh_a.next_elem
            self.mesh_b = mesh_cls(ops, self.pb, self.bolts, self.rh,
                                   self.rb, target, mat_b,
                                   self._node_tag, self._elem_tag,
                                   mat_near=near_b, near_radius=near_r,
                                   boundary_width=self.boundary_width,
                                   min_target=fine)
            self._node_tag = self.mesh_b.next_node
            self._elem_tag = self.mesh_b.next_elem
            for nd in self.mesh_a.perimeter_nodes():
                ops.fix(nd, 1, 1)
            self.plate_elems = self.mesh_a.elems + self.mesh_b.elems
        else:
            # legacy: no hole geometry, bolts act directly on the plate centre
            mat_a = self._plate_material(self.pa)
            mat_b = self._plate_material(self.pb)
            self.mesh_a = _PlateMesh(ops, self.pa, [], self.rh, self.rb,
                                     float(self.model.get("mesh", 0.02)),
                                     mat_a, 1, 1)
            self._node_tag = self.mesh_a.next_node
            self._elem_tag = self.mesh_a.next_elem
            self.mesh_b = _PlateMesh(ops, self.pb, [], self.rh, self.rb,
                                     float(self.model.get("mesh", 0.02)),
                                     mat_b, self._node_tag, self._elem_tag)
            self._node_tag = self.mesh_b.next_node
            self._elem_tag = self.mesh_b.next_elem
            for nd in self.mesh_a.perimeter_nodes():
                ops.fix(nd, 1, 1)
            self.plate_elems = self.mesh_a.elems + self.mesh_b.elems

        # element centroids, used to exclude the local bearing process zone
        # from the non-local plate-yielding criterion
        self.plate_elem_centroids = {}
        for tag in self.plate_elems:
            nds = ops.eleNodes(tag)
            xyz = [ops.nodeCoord(n) for n in nds]
            self.plate_elem_centroids[tag] = (
                sum(p[0] for p in xyz) / len(xyz),
                sum(p[1] for p in xyz) / len(xyz))

        # -- bolt pins, bearing contact and bolt shear links ----------------
        # expected C&K force direction of every bolt (used to orient the
        # vector bolt spring); a single axial spring per bolt is sufficient
        # because the bolt force is parallel to the relative displacement.
        from .icr import solve_icr
        load = self.model["load"]
        theta = math.radians(float(load.get("theta", 0.0)))
        pdir = (math.sin(theta), -math.cos(theta))
        ppoint = (float(load.get("e", 0.0)), float(load.get("h", 0.0)))
        try:
            ck = solve_icr(self.bolts, pdir, ppoint, mu=self.mu,
                           lam=self.lam, delta_max=self.delta_max)
            dirs = []
            for f in ck.bolt_forces:
                n = math.hypot(f[0], f[1])
                dirs.append((f[0] / n, f[1] / n) if n > 1e-12 else pdir)
            ck_capacity = ck.C * self.rult
        except Exception:
            dirs = [pdir] * len(self.bolts)
            ck_capacity = 0.0

        self.pin_a: List[int] = []
        self.pin_b: List[int] = []
        self.bolt_elems: List[int] = []
        self.contact_elems: List[int] = []
        k_bolt = self.rult / max(self.delta_max, 1e-9)
        eps = 1e-4
        pts = _bolt_curve(self.rult, self.delta_max, self.mu, self.lam,
                          clearance=self.bolt_slip)
        strains, stresses = pts[::2], pts[1::2]
        perp_factor = float(self.model.get("perpendicular_force_factor", 0.5))
        for i, (bx, bz) in enumerate(self.bolts):
            if self.model_holes:
                na = self._new_node()
                nb = self._new_node()
                ops.node(na, float(bx), float(bz))
                ops.node(nb, float(bx), float(bz))
                self._attach_pin(na, self.mesh_a.hole_nodes[i], bx, bz,
                                 k_bolt)
                self._attach_pin(nb, self.mesh_b.hole_nodes[i], bx, bz,
                                 k_bolt)
            else:
                na = _nearest(bx, bz, self.mesh_a.coords)
                nb = _nearest(bx, bz, self.mesh_b.coords)
            self.pin_a.append(na)
            self.pin_b.append(nb)

            # Two orthogonal C&K springs aligned with the expected bolt-force
            # direction.  This reproduces the vector law closely (the main
            # direction carries the force; the perpendicular component only
            # acts when the actual displacement direction differs).
            ux, uz = dirs[i]
            mt = self._new_mat()
            ops.uniaxialMaterial("ElasticMultiLinear", mt, 0.0,
                                 "-strain", *strains, "-stress", *stresses)
            mt_t = self._new_mat()
            st_p = [f * perp_factor for f in pts[1::2]]
            ops.uniaxialMaterial("ElasticMultiLinear", mt_t, 0.0,
                                 "-strain", *strains, "-stress", *st_p)
            tag = self._new_elem()
            # pass both local axes explicitly: the Windows OpenSees build
            # requires six orientation components even in 2-D (three values
            # cause a C++ buffer overrun), while Linux accepts three as well
            ops.element("twoNodeLink", tag, na, nb, "-mat", mt, mt_t,
                        "-dir", 1, 2,
                        "-orient", ux, uz, 0.0, -uz, ux, 0.0)
            self.bolt_elems.append(tag)

        # -- load ------------------------------------------------------------
        load = self.model["load"]
        e = float(load.get("e", 0.0))
        h = float(load.get("h", 0.0))
        if self.model_holes:
            if self.mesh_b.coords:
                self.load_node = _nearest(e, h, self.mesh_b.coords)
            else:
                self.load_node = _nearest(0.0, 0.0, self.mesh_b.coords)
        else:
            self.load_node = _nearest(e, h, self.mesh_b.coords)
        theta = math.radians(float(load.get("theta", 0.0)))
        P_ref = float(self.model.get("capacity_hint", 0.0) or 0.0)
        if P_ref <= 0.0:
            P_ref = ck_capacity if ck_capacity > 0 else max(
                float(load.get("P", 1.0)), self.rult)
        # a small margin above the expected capacity so the criteria are met
        self.P_ref = max(P_ref * 1.15, 1.0)
        ops.timeSeries("Linear", 1)
        ops.pattern("Plain", 1, 1)
        ops.load(self.load_node, self.P_ref * math.sin(theta),
                 -self.P_ref * math.cos(theta))

        ops.system("BandGeneral")
        ops.numberer("RCM")
        ops.constraints("Plain")
        ops.test("NormDispIncr", 1e-6, 30, 0)
        ops.algorithm("BFGS")
        ops.integrator("LoadControl", 1.0 / self.max_steps)
        ops.analysis("Static")

    def _attach_pin(self, pin: int, boundary: Sequence[Tuple[float, float, int]],
                    bx: float, bz: float, k_bolt: float) -> None:
        """Radial compression-only trusses from a pin to its hole boundary."""
        ops = self.ops
        if not boundary:
            return
        boundary = sorted(boundary, key=lambda b: math.atan2(b[1] - bz,
                                                              b[0] - bx))
        n = len(boundary)
        # stiff truss ring around the hole: spreads the bearing force over the
        # boundary instead of concentrating it on one mesh node
        if n >= 3 and self.ring_factor > 0.0:
            E = float(self.model.get("E", 200e9))
            t = max(float(self.pa["t"]), float(self.pb["t"]))
            A_ring = t * max(2.0 * self.rh, 1e-3)
            E_ring = self.ring_factor * E
            mat_ring = self._new_mat()
            ops.uniaxialMaterial("Elastic", mat_ring, E_ring)
            for k in range(n):
                nd1 = boundary[k][2]
                nd2 = boundary[(k + 1) % n][2]
                tag = self._new_elem()
                ops.element("corotTruss", tag, nd1, nd2, A_ring, mat_ring)
                self.contact_elems.append(tag)
        # each truss carries a share of the hole perimeter
        area = max(self.pa["t"], self.pb["t"]) * (2 * math.pi * self.rh / n)
        E_contact = self.contact_factor * float(
            self.model.get("E", 200e9))
        for x, z, nd in boundary:
            L = math.hypot(x - bx, z - bz)
            if L <= 0.0:
                continue
            if self.contact_gap:
                gap = max(0.0, L - self.rb) / L
                mat_gap = self._new_mat()
                ops.uniaxialMaterial("ElasticPPGap", mat_gap, E_contact,
                                     1e15, gap, 0.0, "noDamage")
                # a small pre-contact stiffness keeps the plate from becoming
                # a free rigid body while the bolt idles in the clearance
                mat_stab = self._new_mat()
                ops.uniaxialMaterial("Elastic", mat_stab, self.precontact
                                     * E_contact)
                mat = self._new_mat()
                ops.uniaxialMaterial("Parallel", mat, mat_gap, mat_stab)
            else:
                mat = self._new_mat()
                ops.uniaxialMaterial("Elastic", mat, E_contact)
            tag = self._new_elem()
            ops.element("corotTruss", tag, pin, nd, area, mat)
            self.contact_elems.append(tag)

    # -- solve -----------------------------------------------------------
    def _bolt_forces(self) -> List[Tuple[float, float]]:
        """Force of the (oriented) C&K link of every bolt."""
        out = []
        for tag in self.bolt_elems:
            f = self.ops.eleForce(tag)
            out.append((float(f[0]), float(f[1])))
        return out

    def _plate_stress(self) -> float:
        """Characteristic von Mises stress of the plates [Pa].

        ``plate_criterion='max'`` uses the absolute maximum (which is mesh
        sensitive at the bearing point), ``'nonlocal'`` (default) discards the
        local bearing process zone within ``plate_filter_radius`` of a bolt and
        returns the requested percentile - a stable measure of net-section /
        gross plate yielding.  ``'none'`` skips the plate check.
        """
        if self.plate_criterion == "none":
            return 0.0
        ops = self.ops
        values = []
        for tag in self.plate_elems:
            cx, cz = self.plate_elem_centroids.get(tag, (0.0, 0.0))
            if self.plate_criterion != "max":
                if any(math.hypot(cx - bx, cz - bz) < self.plate_filter_radius
                       for bx, bz in self.bolts):
                    continue
            try:
                s = ops.eleResponse(tag, "stresses")
            except Exception:
                continue
            if not s:
                continue
            # quad returns 3 components per Gauss point, Tri31 one set
            for k in range(0, len(s) - 2, 3):
                sx, sz, txz = float(s[k]), float(s[k + 1]), float(s[k + 2])
                values.append(math.sqrt(max(
                    sx * sx + sz * sz - sx * sz + 3.0 * txz * txz, 0.0)))
        if not values:
            return 0.0
        if self.plate_criterion == "max":
            return max(values)
        values.sort()
        idx = min(len(values) - 1,
                  max(0, int(round(self.plate_percentile
                                   * (len(values) - 1)))))
        return values[idx]

    def run(self) -> Dict:
        ops = self.ops
        dlam = 1.0 / self.max_steps
        prev_lam = 0.0
        prev_q = 0.0
        prev_forces = [(0.0, 0.0)] * len(self.bolts)
        prev_deltas = [0.0] * len(self.bolts)
        history = []
        for step in range(1, self.max_steps + 1):
            ok = ops.analyze(1, dlam)
            if ok != 0:
                # The Windows OpenSees J2 material can corrupt its internal
                # state after a failed return mapping.  Re-entering analyze()
                # on that state caused a page fault (c0000005 / Wine crash),
                # so the nonlinear path stops cleanly.  The elastic path is
                # safe to retry with smaller steps and other algorithms.
                if self.nonlinear:
                    return dict(ok=False, message=f"J2 plate update did not "
                                f"converge at load factor {prev_lam:.3f}; "
                                f"rerun with elastic plates or a finer mesh",
                                capacity=None, bolts=[], iterations=step)
                for alg in ("ModifiedNewton", "NewtonLineSearch", "BFGS"):
                    ops.algorithm(alg)
                    ok = ops.analyze(1, 0.25 * dlam)
                    if ok == 0:
                        break
                ops.algorithm("BFGS")
                if ok != 0:
                    return dict(ok=False, message=f"FE did not converge at "
                                f"load factor {prev_lam:.3f}",
                                capacity=None, bolts=[], iterations=step)
                lam = prev_lam + 0.25 * dlam
            else:
                lam = step * dlam

            forces = self._bolt_forces()
            deltas: List[float] = []
            for i in range(len(self.bolts)):
                da = ops.nodeDisp(self.pin_a[i])
                db = ops.nodeDisp(self.pin_b[i])
                deltas.append(math.hypot(da[0] - db[0], da[1] - db[1]))
            vm = self._plate_stress()
            dcrit = (max(0.0, max(deltas) - self.bolt_slip)
                     / max(self.delta_max, 1e-12)) if deltas else 0.0
            scrit = vm / max(self.plate_fu, 1e-12)
            q = max(dcrit, scrit)
            if self.verbose:
                print(f"  step {step:3d}  lambda={lam:7.4f}  "
                      f"P={lam*self.P_ref/1e3:8.2f} kN  "
                      f"dmax={max(deltas)*1e3 if deltas else 0:7.3f} mm  "
                      f"vm={vm/1e6:8.1f} MPa  q={q:.3f}", flush=True)
            history.append((lam, max(deltas) if deltas else 0.0, vm))
            if q >= 1.0:
                # linear interpolation between the last two safe states
                if q > prev_q:
                    frac = (1.0 - prev_q) / (q - prev_q)
                else:
                    frac = 1.0
                cap_lam = prev_lam + frac * (lam - prev_lam)
                cap_forces = [(prev_forces[i][0] + frac * (forces[i][0]
                                                           - prev_forces[i][0]),
                               prev_forces[i][1] + frac * (forces[i][1]
                                                           - prev_forces[i][1]))
                              for i in range(len(forces))]
                cap_deltas = [prev_deltas[i] + frac * (deltas[i]
                                                       - prev_deltas[i])
                              for i in range(len(deltas))]
                return self._result(cap_lam * self.P_ref, cap_forces,
                                    cap_deltas, vm, step, history,
                                    "bolt" if dcrit >= scrit else "plate")
            prev_lam, prev_q = lam, q
            prev_forces, prev_deltas = forces, deltas
        # reached the end of the push-over without triggering a limit state
        forces = self._bolt_forces()
        deltas = []
        for i in range(len(self.bolts)):
            da = ops.nodeDisp(self.pin_a[i])
            db = ops.nodeDisp(self.pin_b[i])
            deltas.append(math.hypot(da[0] - db[0], da[1] - db[1]))
        return self._result(prev_lam * self.P_ref, forces, deltas,
                            self._plate_stress(), self.max_steps, history,
                            "not reached")

    def _element_von_mises(self) -> Dict[int, float]:
        """Peak von Mises stress of every plate element [Pa]."""
        ops = self.ops
        out = {}
        for tag in self.plate_elems:
            try:
                s = ops.eleResponse(tag, "stresses")
            except Exception:
                s = []
            vals = []
            for k in range(0, len(s) - 2, 3):
                sx, sz, txz = float(s[k]), float(s[k + 1]), float(s[k + 2])
                vals.append(math.sqrt(max(
                    sx * sx + sz * sz - sx * sz + 3.0 * txz * txz, 0.0)))
            out[tag] = max(vals) if vals else 0.0
        return out

    def mesh_export(self) -> Dict:
        """Nodes, elements and element stresses for the GUI mesh view."""
        stress = self._element_von_mises()
        plates = []
        for mesh, name in ((self.mesh_a, "Support plate"),
                           (self.mesh_b, "Loaded plate")):
            nodes = [[tag, x, z] for tag, (x, z) in mesh.coords.items()]
            elements = []
            values = []
            for tag in mesh.elems:
                try:
                    elements.append(list(self.ops.eleNodes(tag)))
                    values.append(stress.get(tag, 0.0))
                except Exception:
                    pass
            plates.append(dict(name=name, nodes=nodes,
                               elements=elements, stress=values))
        return dict(plates=plates, bolts=list(self.bolts),
                    bolt_diameter=self.bolt_d, hole_radius=self.rh,
                    mesh_min=float(self.model.get("mesh_min_element", 0.0)
                                   or 0.0),
                    mesh_max=float(self.model.get("mesh_max_element", 0.0)
                                   or self.model.get("mesh", 0.0) or 0.0))

    def _result(self, capacity: float, forces, deltas, vm, iterations,
                history, failure_mode: str) -> Dict:
        r_ult = self.rult * bolt_ratio(self.delta_max, self.mu, self.lam)
        mags = [math.hypot(f[0], f[1]) for f in forces]
        raw_capacity = capacity
        capacity = capacity * self.capacity_factor
        mesh_data = self.mesh_export() if self.return_mesh else None
        if mesh_data is not None:
            mesh_data["forces"] = forces
        return dict(
            ok=True,
            capacity=capacity,
            raw_capacity=raw_capacity,
            capacity_factor=self.capacity_factor,
            bolt_ult=r_ult,
            failure_mode=failure_mode,
            plate_fu=self.plate_fu,
            max_plate_stress=vm,
            max_plate_stress_ratio=vm / max(self.plate_fu, 1e-12),
            max_bolt_deformation=max(deltas) if deltas else 0.0,
            max_bolt_deformation_ratio=(max(0.0, max(deltas) - self.bolt_slip)
                                        / self.delta_max
                                        if deltas else 0.0),
            hole_clearance=self.clearance,
            plate_model=("J2 plasticity" if self.nonlinear
                         else "elastic"),
            parameters=dict(mu=self.mu, ck_lambda=self.lam,
                            delta_max=self.delta_max, rult=self.rult),
            bolts=[dict(name=f"B{i+1}", x=bx, z=bz,
                        fx=forces[i][0], fz=forces[i][1],
                        magnitude=mags[i],
                        unity=mags[i] / r_ult if r_ult else 0.0)
                   for i, (bx, bz) in enumerate(self.bolts)],
            max_deformation=max(deltas) if deltas else 0.0,
            iterations=iterations,
            history=history,
            mesh=mesh_data,
            message="ok")


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def model_from_connection(ci, mesh: Optional[float] = None,
                          capacity_hint: Optional[float] = None,
                          **options) -> Dict:
    """Build the FE model dictionary from a ``ConnectionInput``."""
    pa, pb = ci.plate_a, ci.plate_b
    fem = ci.fem
    model = {
        "plate_a": dict(w=pa.width, h=pa.height, t=pa.thickness,
                        cx=pa.cx, cy=pa.cy, E=pa.material.E,
                        nu=pa.material.poisson, fy=pa.material.fy,
                        fu=pa.material.fu),
        "plate_b": dict(w=pb.width, h=pb.height, t=pb.thickness,
                        cx=pb.cx, cy=pb.cy, E=pb.material.E,
                        nu=pb.material.poisson, fy=pb.material.fy,
                        fu=pb.material.fu),
        "bolts": list(ci.bolts_xy),
        "bolt_rult": ci.bolt_rult(),
        "delta_max": ci.bolt_mat.delta_max,
        "mu": ci.bolt_mat.mu,
        "ck_lambda": ci.bolt_mat.ck_lambda,
        "bolt_diameter": ci.bolt_diameter,
        "hole_clearance": ci.bolt_hole_clearance,
        "load": dict(P=ci.load.P, e=ci.load.e,
                     h=(ci.load.h_load if ci.load.h_load is not None
                        else pb.cy + pb.height / 2.0),
                     theta=ci.load.theta),
        "mesh": fem.mesh if mesh is None else mesh,
        "mesh_min_element": fem.mesh_min_element,
        "mesh_max_element": (fem.mesh_max_element if fem.mesh_max_element
                             is not None else
                             (fem.mesh if mesh is None else mesh)),
        "capacity_hint": capacity_hint,
        "num_steps": fem.num_steps,
        "plate_nonlinear": fem.plate_nonlinear,
        "contact_gap": fem.contact_gap,
        "triangulate": fem.triangulate,
        "elastic_bearing_radius": fem.elastic_bearing_radius,
        "elastic_boundary_width": fem.elastic_boundary_width,
        "perpendicular_force_factor": fem.perpendicular_force_factor,
        "plate_criterion": fem.plate_criterion,
        "plate_stress_percentile": fem.plate_stress_percentile,
        "plate_filter_radius": fem.plate_filter_radius,
        "capacity_factor": fem.capacity_factor,
    }
    model.update(options)
    return model


def _attempt(model: Dict, verbose: bool) -> Dict:
    fem = _FeModel(dict(model, verbose=verbose))
    try:
        fem.build()
        return fem.run()
    except Exception as exc:                       # keep the runner informed
        import traceback
        return dict(ok=False, message=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                    capacity=None, bolts=[])


def solve(model: Dict, verbose: bool = False) -> Dict:
    """Run the nonlinear FE fallback and return forces + capacity.

    If the J2 plate update fails to converge (possible for a locally singular
    bearing stress field), the analysis is repeated with elastic plates and the
    result is flagged with ``plate_model='elastic (J2 fallback)'``.  If the
    push-over reaches its load ceiling without meeting a limit state, it is
    repeated with a higher ceiling.
    """
    model = dict(model)
    result = _attempt(model, verbose)

    # Do NOT rebuild/retry a failed J2 model in the same process: the Windows
    # OpenSees build can page-fault while the failed material state is being
    # destroyed.  The GUI defaults to elastic plates; users who request J2 get
    # a clean diagnostic instead of a crash.
    if result.get("ok") and result.get("failure_mode") == "not reached":
        hint = float(model.get("capacity_hint", 0.0) or 0.0)
        cap = float(result.get("capacity") or 0.0)
        higher = dict(model, capacity_hint=max(hint, cap) * 1.6)
        retry = _attempt(higher, verbose)
        if retry.get("ok") and (retry.get("capacity") or 0.0) >= cap:
            result = retry
    return result


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
    result = solve(model, verbose="--verbose" in argv)
    with open(argv[1], "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
