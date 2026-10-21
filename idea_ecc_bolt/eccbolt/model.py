"""
Data model for an eccentrically loaded bolted steel shear connection.

Conventions
-----------
* Connection (bolt-group) plane  : global X-Z plane  (X horizontal in plane, Z vertical).
* Out-of-plane direction         : global Y (bolt axes are parallel to Y).
* Bolt coordinates (x, y) entered by the user are in-plane coordinates:
  ``x`` horizontal, ``y`` vertical  (=> global (x, 0, y)).
* Load ``P`` acts in the connection plane at an angle ``theta`` measured from
  the vertical (positive towards +x).  Its line of action passes at a
  horizontal distance ``e`` from the bolt-group centroid (the classical
  Crawford & Kulak / AISC eccentricity for theta = 0).  The load application
  point used for the IDEA model is (e, h_load) relative to the centroid.
* Internal units: SI (m, N, Pa).  Helpers convert to/from imperial.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

INCH = 0.0254
KIP = 4448.2216152605
KSI = 6894757.2932
MM = 0.001

# Crawford & Kulak (1971) load-deformation parameters (inches, per bolt).
# CK_MU is kept in 1/in for reference; the model stores mu in 1/m (SI).
CK_MU = 10.0          # regression coefficient mu  [1/in]
CK_LAMBDA = 0.55      # regression coefficient lambda [-]
CK_DELTA_MAX = 0.34   # ultimate bolt deformation   [in]


@dataclass
class SteelMaterial:
    """Steel plate material."""
    designation: str = "A572 Gr.50"
    fy: float = 345.0e6            # Pa
    fu: float = 450.0e6            # Pa
    E: float = 200.0e9             # Pa
    poisson: float = 0.3

    def clone(self, **kw) -> "SteelMaterial":
        d = self.__dict__.copy()
        d.update(kw)
        return SteelMaterial(**d)


@dataclass
class BoltMaterial:
    """Bolt grade / properties."""
    designation: str = "A325"
    fub: float = 825.0e6           # Pa  ultimate tensile strength
    fyb: float = 580.0e6           # Pa  yield strength
    # Crawford & Kulak ultimate shear resistance of ONE bolt for the number of
    # shear planes considered (default: single shear, 3/4" A325 ~ 37 kip).
    # If None it is estimated as 0.62*fub*As*n_planes (C&K-type approximation).
    rult: Optional[float] = None   # N
    delta_max: float = CK_DELTA_MAX * INCH   # m, ultimate bolt deformation
    # Crawford & Kulak load-deformation law parameters.  ``mu`` is stored in
    # 1/m (the C&K value 10/in = 393.7/m); ``ck_lambda`` is dimensionless.
    # Both can be changed by the user or by the Calibrate command.
    mu: float = CK_MU / INCH                  # 1/m
    ck_lambda: float = CK_LAMBDA              # -

    @property
    def lambda_(self) -> float:
        """Alias for ``ck_lambda`` (``lambda`` is a Python keyword)."""
        return self.ck_lambda

    @lambda_.setter
    def lambda_(self, value: float) -> None:
        self.ck_lambda = float(value)

    @property
    def lam(self) -> float:
        return self.ck_lambda

    @lam.setter
    def lam(self, value: float) -> None:
        self.ck_lambda = float(value)


@dataclass
class Plate:
    """Rectangular steel plate lying in the connection (X-Z) plane."""
    name: str = "P1"
    width: float = 0.30            # m, in-plane horizontal extent  (X)
    height: float = 0.45           # m, in-plane vertical extent    (Z)
    thickness: float = 0.012       # m, out-of-plane                (Y)
    # centre of the plate rectangle relative to the bolt-group centroid (m)
    cx: float = 0.0
    cy: float = 0.0                # vertical offset in plane (Z)
    material: SteelMaterial = field(default_factory=SteelMaterial)

    # y position of plate mid-surface (out-of-plane), set by the modeller
    y_mid: float = 0.0

    def outline(self) -> List[Tuple[float, float]]:
        """(x, z) corners of the rectangle, relative to bolt-group centroid."""
        x0, x1 = self.cx - self.width / 2, self.cx + self.width / 2
        y0, y1 = self.cy - self.height / 2, self.cy + self.height / 2
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@dataclass
class LoadSpec:
    P: float = 100.0e3             # N, load magnitude
    theta: float = 0.0             # deg, from vertical (+ towards +x)
    e: float = 0.40                # m, horizontal eccentricity of the load
                                   #    line of action from bolt-group centroid
    h_load: Optional[float] = None  # m, height of load application point above
                                   #    centroid (default: top edge of loaded plate)

    @property
    def direction(self) -> Tuple[float, float]:
        """Unit load vector in plane (x, z); theta=0 => straight down."""
        t = math.radians(self.theta)
        return (math.sin(t), -math.cos(t))

    def moment_about_centroid(self) -> float:
        """Moment (about +Y, i.e. out-of-plane, N.m) of P about the bolt-group
        centroid:  M = P * (e*cos(theta) + h*sin(theta))  (CCW+)."""
        t = math.radians(self.theta)
        h = self.h_load if self.h_load is not None else 0.0
        return self.P * (self.e * math.cos(t) + h * math.sin(t))


@dataclass
class FemOptions:
    """Options for the OpenSees finite-element fallback.

    The defaults model the real bolt holes (diameter d + clearance), the
    nonlinear C&K bolt law and J2-plastic plates.  They can be overridden by
    the GUI/JSON without changing the analytical C&K model.
    """
    mesh: Optional[float] = None           # m, target element size
    triangulate: bool = True               # circular holes via Delaunay mesh
    plate_nonlinear: bool = True           # J2 plasticity (False = elastic)
    contact_gap: bool = True               # one-sided bearing with clearance
    elastic_bearing_radius: float = 1.5    # x hole radius kept elastic
    elastic_boundary_width: float = 0.05   # m, kept elastic near supports
    perpendicular_force_factor: float = 0.5
    plate_criterion: str = "nonlocal"
    plate_stress_percentile: float = 0.95
    plate_filter_radius: float = 2.0       # x hole radius
    num_steps: int = 80
    #: global bias factor applied to the FE capacity after calibration
    capacity_factor: float = 1.0


@dataclass
class ConnectionInput:
    """Complete definition of the eccentrically loaded bolted shear connection."""
    title: str = "Eccentrically loaded bolted shear connection"
    design_code: str = "AISC"          # "AISC" or "EC3"
    bolts_xy: List[Tuple[float, float]] = field(default_factory=list)  # m, in-plane
    bolt_diameter: float = 0.019       # m
    bolt_hole_clearance: float = 0.002  # m (hole = d + clearance)
    n_shear_planes: int = 1
    threads_in_shear_plane: bool = True
    plate_a: Plate = field(default_factory=lambda: Plate("Support plate"))
    plate_b: Plate = field(default_factory=lambda: Plate("Loaded plate"))
    bolt_mat: BoltMaterial = field(default_factory=BoltMaterial)
    load: LoadSpec = field(default_factory=LoadSpec)
    fem: FemOptions = field(default_factory=FemOptions)

    # ------------------------------------------------------------------ utils
    @property
    def n_bolts(self) -> int:
        return len(self.bolts_xy)

    def centroid(self) -> Tuple[float, float]:
        xs = [b[0] for b in self.bolts_xy]
        ys = [b[1] for b in self.bolts_xy]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def bolt_rult(self) -> float:
        """Ultimate shear resistance of one bolt (N) for the configured number
        of shear planes.  Crawford & Kulak measured Rult = 74 kip (double shear)
        for 3/4" A325 bolts, i.e. ~0.62*fub*Ab per shear plane; the user may
        override with ``bolt_mat.rult``."""
        if self.bolt_mat.rult:
            return self.bolt_mat.rult
        ab = math.pi / 4 * self.bolt_diameter ** 2
        return 0.62 * self.bolt_mat.fub * ab * self.n_shear_planes

    def validate(self) -> List[str]:
        errs = []
        if self.n_bolts < 1:
            errs.append("At least one bolt is required.")
        if self.n_bolts >= 2:
            xs = [b[0] for b in self.bolts_xy]
            ys = [b[1] for b in self.bolts_xy]
            for i in range(self.n_bolts):
                for j in range(i + 1, self.n_bolts):
                    d = math.hypot(xs[i] - xs[j], ys[i] - ys[j])
                    if d < 2.0 * self.bolt_diameter:
                        errs.append(f"Bolts {i+1} and {j+1} are closer than 2d.")
        for p in (self.plate_a, self.plate_b):
            if p.width <= 0 or p.height <= 0 or p.thickness <= 0:
                errs.append(f"Plate {p.name}: dimensions must be positive.")
        if self.bolt_diameter <= 0:
            errs.append("Bolt diameter must be positive.")
        if self.bolt_mat.mu <= 0:
            errs.append("C&K parameter mu must be positive (1/m).")
        if self.bolt_mat.ck_lambda <= 0:
            errs.append("C&K parameter lambda must be positive.")
        if self.bolt_mat.delta_max <= 0:
            errs.append("Bolt deformation delta_max must be positive.")
        if self.load.P <= 0:
            errs.append("Load magnitude must be positive.")
        if self.fem.mesh is not None and self.fem.mesh <= 0:
            errs.append("FE mesh size must be positive.")
        if not 0.0 < self.fem.plate_stress_percentile <= 1.0:
            errs.append("Plate stress percentile must be in (0, 1].")
        if self.fem.capacity_factor <= 0:
            errs.append("FE capacity factor must be positive.")
        return errs


# ---------------------------------------------------------------------------
# arbitrary / random bolt arrangements
# ---------------------------------------------------------------------------
def random_bolt_group(n: int, width: float, height: float,
                      min_dist: float, seed: Optional[int] = None,
                      max_tries: int = 20000) -> List[Tuple[float, float]]:
    """Generate *n* bolts at arbitrary (random) in-plane positions.

    The IDEA StatiCa ``BoltGrid`` accepts a free list of hole positions
    (``Positions: List<Point3D>``), i.e. completely irregular patterns are
    supported - this helper merely proposes one by rejection sampling inside
    a ``width x height`` rectangle centred on the origin, honouring a minimum
    bolt spacing ``min_dist``.
    """
    import random as _rnd
    rng = _rnd.Random(seed)
    pts: List[Tuple[float, float]] = []
    tries = 0
    while len(pts) < n and tries < max_tries:
        tries += 1
        cand = (rng.uniform(-width / 2, width / 2),
                rng.uniform(-height / 2, height / 2))
        if all(math.hypot(cand[0] - p[0], cand[1] - p[1]) >= min_dist
               for p in pts):
            pts.append(cand)
    if len(pts) < n:
        raise ValueError(f"could not place {n} bolts with min distance "
                         f"{min_dist} in {width}x{height}")
    return pts
