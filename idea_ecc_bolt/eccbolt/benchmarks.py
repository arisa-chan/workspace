"""Comparison of the program with **experimental** data only.

The comparison is deliberately restricted to tests that were carried out on
physical specimens and that report their observed failure load.  Analytical
reference solutions (the C&K/AISC tabulated coefficients, Brandt's worked
examples) are *not* used here - they only serve as unit tests of the solver in
``tests/test_icr.py``, because comparing one analytical method with another
says nothing about the accuracy of the program.

Sources
-------
**Badawi (1983)** - "Behaviour of Eccentric Bolted Shear Connections", M.Eng.
thesis, McGill University (publicly available).  Twelve full-size double-angle
specimens S13-S24 were tested to failure; the thesis reports

* the bolt patterns and eccentricities (Table 4 / Figs. 21-23),
* the ultimate bolt value of each specimen from single-bolt tension-shear
  tests of the actual beam web (Table 41: ``L t Fu`` with ``L = 3d``),
* the measured tensile strength of every beam web (Table 5),
* the observed failure load of every specimen (Table 48, column P1).

The ten specimens whose bolt pattern is unambiguous are modelled here.  S15
and S16 are omitted because their pattern is only partly reproduced in the
published table.

**Crawford & Kulak (1968/1971)** - the Nova Scotia Technical College report is
the full test programme behind the 1971 paper: eight full-size double-angle
specimens B1-B8 with the bolt patterns, eccentricities and observed ultimate
loads in Tables 1 and 2.  The model uses Pult/2 (one of the two identical
connections) and the measured 74 kip double-shear bolt strength.

**Denavit, Franceschetti & Shahan (2021)** - the AISC tearout report.  Table 11
contains 22 single-bolt tension tests with standard, no-clearance, oversize,
extra-oversize and slotted holes, measured edge distances and ultimate loads.
They are an FE-only set (the C&K rigid-plate method does not model tearout);
the lap-joint model uses Rexp,u/2 for one shear plane.

**Astaneh et al. (1989) and Sarkar (1992)** - single-plate shear connection
tests, as tabulated in Ashakul's (2004) thesis (Tables 2.2-2.5).  They are
included as FE-only sets with the reported connection shear and, for Astaneh,
the bolt-line moment converted to an equivalent eccentricity.  Yorgun et al.
(2004), also supplied as a PDF, is a double-channel beam-to-column moment
connection and is not a bolt-group shear specimen; it is therefore not part of
the calibration database.

**Abu-Yosef, Sayed-Ahmed & Soliman (2019)** - twelve eccentric shear+torsion
tests (ISEC-10, STR-40); the paper reports an experimental/inelastic-analysis
ratio of 0.90 +/- 0.04, i.e. the same over-prediction.  It is quoted as a
documented bias band, not as a per-specimen check (the specimen geometry is not
published in a form that allows a model to be rebuilt).

**Bahia & Martin (1980)** - 117 single-shear joints; the paper's conclusion
(linear load-deformation theory "suitable for the analysis and slightly
conservative") is quoted for completeness.  The test tables are not openly
available, so no per-specimen comparison is possible.

Tuning the IDEA StatiCa model
-----------------------------
``TUNING_STEPS`` are applied in order when the service does not produce a
result.  They follow the guidance in the request: mesh size first, then the
material model, then the detailing checks (which some of the test specimens
fail because they were deliberately built with small edge distances).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .icr import solve_from_eccentricity
from .model import (BoltMaterial, ConnectionInput, INCH, LoadSpec, Plate,
                    SteelMaterial)

MM = 0.001

#: observed/analysed ratios inside this band are considered a match
ACCEPTANCE = (0.85, 1.15)

#: documented experimental scatter of the ultimate-strength method
CK_BAND = (1.05, 1.16)     # measured/predicted, Crawford & Kulak (1971)
ISEC_BAND = (0.86, 0.94)   # measured/predicted, Abu-Yosef et al. (2019)


# ---------------------------------------------------------------------------
# experimental data
# ---------------------------------------------------------------------------
@dataclass
class ExperimentalCase:
    """One physical test specimen with everything needed to model it."""
    source: str
    name: str
    bolts_xy: List[Tuple[float, float]]        # m, in-plane coordinates
    eccentricity: float                        # m
    bolt_diameter: float                       # m
    bolt_rult: float                           # N, from the single-bolt tests
    plate_thickness: float                     # m, thinnest connected material
    plate_fu: float                            # Pa, measured tensile strength
    plate_width: float                         # m
    plate_height: float                        # m
    failure_load: float                        # N, observed
    note: str = ""
    #: key/name of the experimental set this specimen belongs to
    set_key: str = ""
    set_name: str = ""
    #: measured plate yield strength; None -> 0.85*Fu (Badawi approximation)
    plate_fy: Optional[float] = None
    #: bolt-hole clearance (hole diameter - bolt diameter)
    hole_clearance: float = 0.002
    #: whether the specimen is meaningful for the C&K group method / FE model
    supports_analytical: bool = True
    supports_fea: bool = True

    @property
    def case_id(self) -> str:
        return f"{self.set_key or self.source}:{self.name}"

    def connection_input(self, load_factor: float = 1.0) -> ConnectionInput:
        """A :class:`ConnectionInput` that reproduces the specimen."""
        height = max(self.plate_height,
                     max(b[1] for b in self.bolts_xy) * 2 + 0.1)
        width = max(self.plate_width,
                    max(b[0] for b in self.bolts_xy) * 2 + 0.1)
        fy = 0.85 * self.plate_fu if self.plate_fy is None else self.plate_fy
        mat = SteelMaterial(f"measured Fu={self.plate_fu/1e6:.0f} MPa",
                            fy=fy, fu=self.plate_fu)
        return ConnectionInput(
            title=f"{self.source} {self.name}",
            design_code="AISC",
            bolts_xy=list(self.bolts_xy),
            bolt_diameter=self.bolt_diameter,
            bolt_hole_clearance=self.hole_clearance,
            plate_a=Plate("Support plate", width=width, height=height,
                          thickness=self.plate_thickness, material=mat),
            plate_b=Plate("Loaded plate", width=width * 1.5, height=height,
                          thickness=self.plate_thickness, material=mat),
            bolt_mat=BoltMaterial("A325", fub=825e6, fyb=580e6,
                                  rult=self.bolt_rult),
            load=LoadSpec(P=self.failure_load * load_factor,
                          theta=0.0, e=self.eccentricity))

    def analytical_capacity(self, mu=None, ck_lambda=None) -> float:
        """Crawford & Kulak ultimate strength of the specimen [N].

        The default uses the C&K parameters of the model class; calibrated
        values can be supplied by the Calibrate command.
        """
        from .model import BoltMaterial
        bm = BoltMaterial()
        c = solve_from_eccentricity(
            self.bolts_xy, (0.0, -1.0), self.eccentricity,
            mu=bm.mu if mu is None else mu,
            lam=bm.ck_lambda if ck_lambda is None else ck_lambda).C
        return c * self.bolt_rult

    def fea_model(self, mesh: float = 0.008, plate_nonlinear: bool = True,
                  **options) -> Dict:
        """OpenSees model dictionary for the FE fallback."""
        ci = self.connection_input()
        pa, pb = ci.plate_a, ci.plate_b
        model = {
            "plate_a": dict(w=pa.width, h=pa.height, t=pa.thickness,
                            cx=pa.cx, cy=pa.cy,
                            E=pa.material.E, nu=pa.material.poisson,
                            fy=pa.material.fy, fu=pa.material.fu),
            "plate_b": dict(w=pb.width, h=pb.height, t=pb.thickness,
                            cx=pb.cx, cy=pb.cy,
                            E=pb.material.E, nu=pb.material.poisson,
                            fy=pb.material.fy, fu=pb.material.fu),
            "bolts": list(ci.bolts_xy),
            "bolt_rult": ci.bolt_rult(),
            "delta_max": ci.bolt_mat.delta_max,
            "mu": ci.bolt_mat.mu,
            "ck_lambda": ci.bolt_mat.ck_lambda,
            "bolt_diameter": ci.bolt_diameter,
            "hole_clearance": ci.bolt_hole_clearance,
            "load": dict(P=ci.load.P, e=ci.load.e, h=0.0,
                         theta=ci.load.theta),
            "mesh": mesh,
            "mesh_max_element": mesh,
            "mesh_min_element": ci.fem.mesh_min_element,
            "capacity_hint": max(self.analytical_capacity() * 1.6,
                                 0.95 * ci.n_bolts * ci.bolt_rult()),
            "num_steps": 80,
            "plate_nonlinear": plate_nonlinear,
            "plate_criterion": "nonlocal",
            "perpendicular_force_factor": 0.5,
            "capacity_factor": ci.fem.capacity_factor,
        }
        model.update(options)
        return model

    def fea_analysis(self, mesh: float = 0.008,
                     plate_nonlinear: bool = True, **options) -> Dict:
        """Full OpenSees fallback result for the specimen."""
        from .fe_fallback import solve
        return solve(self.fea_model(mesh=mesh,
                                    plate_nonlinear=plate_nonlinear,
                                    **options))

    def fea_capacity(self, mesh: float = 0.008,
                     plate_nonlinear: bool = True, **options):
        """FE capacity [N], or ``None`` if no limit state was reached."""
        res = self.fea_analysis(mesh=mesh, plate_nonlinear=plate_nonlinear,
                                **options)
        return res.get("capacity") if res.get("ok") else None


def row(n, pitch):
    return [(0.0, (i - (n - 1) / 2) * pitch) for i in range(n)]


def row_h(n, spacing):
    return [((i - (n - 1) / 2) * spacing, 0.0) for i in range(n)]


def line45(n, pitch):
    s = pitch / 2 ** 0.5
    return [((i - (n - 1) / 2) * s, (i - (n - 1) / 2) * s) for i in range(n)]


def grid(ncols, nrows, gage, pitch):
    return [((i - (ncols - 1) / 2) * gage, (j - (nrows - 1) / 2) * pitch)
            for i in range(ncols) for j in range(nrows)]


BADAWI = "Badawi (1983)"

#  name, bolts, e [mm], Rult [kN], t_web [mm], Fu [MPa], P_test [kN], note
_BADAWI_DATA = (
    ("S13", row_h(2, 65 * MM), 90, 215.00, 7.75, 485.42, 148.0,
     "two-bolt group, B7 web"),
    ("S14", row_h(2, 65 * MM), 140, 215.00, 7.75, 485.42, 132.9,
     "two-bolt group, B7 web"),
    ("S17", row(4, 65 * MM), 165, 198.71, 8.03, 433.00, 327.6,
     "four bolts vertical, B9 web"),
    ("S18", row(4, 65 * MM), 300, 198.71, 8.03, 433.00, 170.1,
     "four bolts vertical, B9 web"),
    ("S19", line45(4, 65 * MM), 165, 209.92, 7.82, 469.70, 253.2,
     "four bolts at 45 deg, B10 web"),
    ("S20", line45(4, 65 * MM), 300, 209.92, 7.82, 469.70, 166.9,
     "four bolts at 45 deg, B10 web"),
    ("S21", row_h(4, 65 * MM), 165, 206.89, 7.83, 462.33, 318.1,
     "four bolts horizontal, B11 web"),
    ("S22", row_h(4, 65 * MM), 300, 206.89, 7.83, 462.33, 160.6,
     "four bolts horizontal, B11 web"),
    ("S23", grid(3, 3, 75 * MM, 75 * MM), 150, 306.34, 11.55, 464.10, 853.0,
     "3 x 3 grid at 75 mm, B12 web"),
    ("S24", grid(3, 3, 75 * MM, 75 * MM), 300, 306.34, 11.55, 464.10, 689.0,
     "3 x 3 grid at 75 mm, B12 web"),
)


def badawi_cases() -> List[ExperimentalCase]:
    cases = []
    for name, bolts, ecc, rult, t, fu, p, note in _BADAWI_DATA:
        span_y = max(abs(b[1]) for b in bolts) * 2 + 0.12
        span_x = max(abs(b[0]) for b in bolts) * 2 + 0.12
        cases.append(ExperimentalCase(
            source=BADAWI, name=name, bolts_xy=bolts, eccentricity=ecc * MM,
            bolt_diameter=0.75 * INCH, bolt_rult=rult * 1e3,
            plate_thickness=t * MM, plate_fu=fu * 1e6,
            plate_width=span_x, plate_height=span_y,
            failure_load=p * 1e3, note=note,
            set_key="badawi_1983", set_name="Badawi (1983)"))
    return cases


CRAWFORD_KULAK = "Crawford & Kulak (1968)"
DENAVIT = "Denavit et al. (2021)"
ASTANEH = "Astaneh et al. (1989)"
SARKAR = "Sarkar (1992)"

#: C&K full-size tests B1-B8 (Tables 1 and 2 of the 1968 report).
#: The table's Pult is for the complete specimen (two identical connections);
#: the model represents one connection, hence failure_load = Pult/2.
#: Rult = 74 kip is the measured 3/4 in A325 double-shear bolt strength.
#: name, bolts, e [in], test Pult [kip], note
_CK_DATA = (
    ("B1", row(5, 2.5), 8, 225, "one column, 5 bolts"),
    ("B2", row(5, 3.0), 10, 230, "one column, 5 bolts"),
    ("B3", row(5, 3.0), 12, 190, "one column, 5 bolts"),
    ("B4", row(6, 3.0), 13, 251, "one column, 6 bolts"),
    ("B5", row(6, 3.0), 15, 221, "one column, 6 bolts"),
    ("B6", grid(2, 4, 2.5, 3.0), 12, 264, "two columns, 8 bolts"),
    ("B7", grid(2, 4, 2.5, 3.0), 15, 212, "two columns, 8 bolts"),
    ("B8", grid(2, 5, 2.5, 2.5), 15, 266, "two columns, 10 bolts"),
)


def crawford_kulak_cases() -> List[ExperimentalCase]:
    cases = []
    for name, bolts, ecc_in, p_kip, note in _CK_DATA:
        span = max(abs(b[1]) for b in bolts) * 2
        height = span + 3 * INCH
        cases.append(ExperimentalCase(
            source=CRAWFORD_KULAK, name=name,
            set_key="crawford_kulak_1968", set_name="Crawford & Kulak (1968)",
            bolts_xy=[(x * INCH, y * INCH) for x, y in bolts],
            eccentricity=ecc_in * INCH,
            bolt_diameter=0.75 * INCH, bolt_rult=74.0 * 4448.2216152605,
            plate_thickness=0.5 * INCH, plate_fu=58.0 * 6894757.2932,
            plate_fy=36.0 * 6894757.2932,
            plate_width=4.5 * INCH, plate_height=height,
            hole_clearance=0.0,
            failure_load=0.5 * p_kip * 4448.2216152605,
            note=note + " (test Pult/2, one connection)",
            supports_analytical=True, supports_fea=False))
    return cases


#: Denavit et al. (2021), Table 11 - single-bolt tension tests.  The test is a
#: butt splice with two shear planes; the lap-joint model represents one plane,
#: so the target is Rexp,u/2 and Rult is the single-shear A325 value.
#: name, Le [in], dh [in], Rexp,u [kip], note
_DENAVIT_DATA = (
    ("STD1", 1.02, 0.812, 19.9, "standard hole"),
    ("STD1g", 0.99, 0.823, 17.8, "standard hole, untightened/greased"),
    ("STD2", 1.16, 0.817, 22.9, "standard hole"),
    ("STD3", 1.56, 0.815, 31.4, "standard hole"),
    ("STD4", 2.01, 0.818, 43.8, "standard hole, bearing/tearout"),
    ("NC1", 1.03, 0.753, 20.7, "no-clearance hole"),
    ("NC2a", 1.32, 0.752, 29.0, "no-clearance hole"),
    ("NC2b", 1.27, 0.744, 27.4, "no-clearance hole, repeat"),
    ("NC3", 1.56, 0.752, 34.0, "no-clearance hole"),
    ("NC4", 2.03, 0.747, 42.4, "no-clearance hole"),
    ("OVS1", 1.05, 0.938, 20.3, "oversize hole"),
    ("OVS2", 1.28, 0.928, 24.4, "oversize hole"),
    ("OVS3", 1.54, 0.929, 31.0, "oversize hole"),
    ("OVS4", 2.05, 0.923, 42.0, "oversize hole"),
    ("XOVS1", 0.96, 1.062, 14.1, "extra-oversize hole"),
    ("XOVS2", 1.29, 1.056, 24.2, "extra-oversize hole"),
    ("XOVS3", 1.51, 1.054, 27.1, "extra-oversize hole"),
    ("XOVS4", 2.04, 1.058, 36.8, "extra-oversize hole"),
    ("SSLT1", 1.02, 0.994, 18.1, "short-slotted hole (width)"),
    ("SSLT2", 1.31, 0.997, 21.8, "short-slotted hole (width)"),
    ("SSLT3", 1.56, 0.990, 29.2, "short-slotted hole (width)"),
    ("SSLT4", 2.10, 0.992, 40.7, "short-slotted hole (width)"),
)


def denavit_cases() -> List[ExperimentalCase]:
    cases = []
    for name, le_in, dh_in, p_kip, note in _DENAVIT_DATA:
        h = (le_in + 3.0) * INCH
        y = h / 2 - le_in * INCH           # one free edge at distance Le
        cases.append(ExperimentalCase(
            source=DENAVIT, name=name,
            set_key="denavit_2021", set_name="Denavit et al. (2021)",
            bolts_xy=[(0.0, y)],
            eccentricity=0.0,
            bolt_diameter=0.75 * INCH, bolt_rult=37.0 * 4448.2216152605,
            plate_thickness=0.25 * INCH,
            plate_fu=73.7 * 6894757.2932,
            plate_fy=54.5 * 6894757.2932,
            plate_width=3.0 * INCH, plate_height=h,
            hole_clearance=max(0.0, dh_in - 0.75) * INCH,
            failure_load=0.5 * p_kip * 4448.2216152605,
            note=note + " (Rexp,u/2, one shear plane)",
            supports_analytical=False, supports_fea=True))
    return cases


#: Astaneh et al. (1989) single-plate tests, as tabulated by Ashakul (2004),
#: Tables 2.2 and 2.3.  The failure load is the measured connection shear.
#: name, n, plate depth [in], V [kip], M_bolt [kip-in], Rult [kip], note
_ASTANEH_DATA = (
    ("A1", 3, 9.0, 94, 20, 37, "A325-N, 3 bolts"),
    ("A2", 5, 15.0, 137, 314, 37, "A325-N, 5 bolts"),
    ("A3", 7, 21.0, 160, 306, 37, "A325-N, 7 bolts"),
    ("A4", 3, 8.25, 79, -47, 49, "A490-N, 3 bolts"),
    ("A5", 5, 14.25, 130, 273, 49, "A490-N, 5 bolts"),
    ("A6", 9, 27.0, 260, 591, 49, "A490-N, 9 bolts"),
)


def astaneh_cases() -> List[ExperimentalCase]:
    cases = []
    for name, n, depth_in, v_kip, m_kipin, rult_kip, note in _ASTANEH_DATA:
        h = depth_in * INCH
        e = (m_kipin / v_kip) * INCH
        cases.append(ExperimentalCase(
            source=ASTANEH, name=name,
            set_key="astaneh_1989", set_name="Astaneh et al. (1989)",
            bolts_xy=row(n, 3.0 * INCH), eccentricity=e,
            bolt_diameter=0.75 * INCH,
            bolt_rult=rult_kip * 4448.2216152605,
            plate_thickness=0.375 * INCH,
            plate_fu=58.0 * 6894757.2932,
            plate_fy=36.0 * 6894757.2932,
            plate_width=4.25 * INCH, plate_height=h,
            failure_load=v_kip * 4448.2216152605,
            note=note + " (single-plate connection)",
            supports_analytical=False, supports_fea=True))
    return cases


#: Sarkar (1992) tests as tabulated by Ashakul (2004), Tables 2.4/2.5.
#: Tests with weld failures are omitted (S2); the selected load is the
#: maximum connection shear reported for the specimen.
#: name, n, plate depth [in], V [kip], note
_SARKAR_DATA = (
    ("S1", 2, 6.0, 64.3, "A325-X, 2 bolts, plate distortion"),
    ("S3", 4, 12.0, 93.0, "A325-N, 4 bolts"),
    ("S4", 4, 12.0, 129.0, "A325-N, 4 bolts, short slots"),
    ("S5", 6, 18.0, 119.0, "A325-N, 6 bolts"),
    ("S6", 6, 18.0, 194.0, "A325-N, 6 bolts, short slots"),
)


def sarkar_cases() -> List[ExperimentalCase]:
    cases = []
    for name, n, depth_in, v_kip, note in _SARKAR_DATA:
        cases.append(ExperimentalCase(
            source=SARKAR, name=name,
            set_key="sarkar_1992", set_name="Sarkar (1992)",
            bolts_xy=row(n, 3.0 * INCH), eccentricity=0.0,
            bolt_diameter=0.75 * INCH,
            bolt_rult=37.0 * 4448.2216152605,
            plate_thickness=0.375 * INCH,
            plate_fu=58.0 * 6894757.2932,
            plate_fy=36.0 * 6894757.2932,
            plate_width=5.0 * INCH,
            plate_height=depth_in * INCH,
            failure_load=v_kip * 4448.2216152605,
            note=note + " (single-plate connection)",
            supports_analytical=False, supports_fea=True))
    return cases


def experimental_sets() -> Dict[str, List[ExperimentalCase]]:
    """Named experimental sets available for comparison/calibration."""
    return {
        "badawi_1983": badawi_cases(),
        "crawford_kulak_1968": crawford_kulak_cases(),
        "denavit_2021": denavit_cases(),
        "astaneh_1989": astaneh_cases(),
        "sarkar_1992": sarkar_cases(),
    }


def all_experimental_cases() -> List[ExperimentalCase]:
    return [c for cases in experimental_sets().values() for c in cases]


def select_experimental_cases(selection=None) -> List[ExperimentalCase]:
    """Filter the experimental database.

    ``selection`` is ``None`` (the historical Badawi-only default), a set key
    or list of set keys, or a mapping ``{set_key: None|"all"| [sample, ...]}``.
    Case names may be given with or without the ``set_key:`` prefix.
    """
    sets = experimental_sets()
    if selection is None:
        return experimental_cases()
    if isinstance(selection, str):
        selection = {selection: None}
    elif isinstance(selection, (list, tuple, set)):
        selection = {str(k): None for k in selection}
    out = []
    for key, samples in selection.items():
        if key not in sets:
            raise KeyError(f"unknown experimental set: {key}")
        cases = sets[key]
        if samples in (None, "all", "*"):
            out.extend(cases)
            continue
        wanted = {str(s).split(":")[-1] for s in samples}
        out.extend(c for c in cases if c.name in wanted)
    return out


def experimental_cases() -> List[ExperimentalCase]:
    """Historical default set (Badawi 1983, ten full-size specimens)."""
    return badawi_cases()


def calibrate_ck_parameters(**kwargs):
    """Lazy re-export of :func:`eccbolt.calibration.calibrate_ck_parameters`."""
    from .calibration import calibrate_ck_parameters as _cal
    return _cal(**kwargs)


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
@dataclass
class Comparison:
    case: ExperimentalCase
    analytical_capacity: float                 # N, C&K
    idea_capacity: Optional[float] = None      # N, from the IDEA StatiCa API
    idea_error: str = ""
    fea_capacity: Optional[float] = None       # N, OpenSees fallback
    fea_error: str = ""
    fea_result: Dict[str, object] = field(default_factory=dict)
    settings: Dict[str, object] = field(default_factory=dict)

    @property
    def measured(self) -> float:
        return self.case.failure_load

    @property
    def analytical_ratio(self) -> float:
        return self.measured / self.analytical_capacity

    @property
    def idea_ratio(self) -> Optional[float]:
        if not self.idea_capacity:
            return None
        return self.measured / self.idea_capacity

    @property
    def idea_ok(self) -> Optional[bool]:
        r = self.idea_ratio
        return None if r is None else ACCEPTANCE[0] <= r <= ACCEPTANCE[1]

    @property
    def fea_ratio(self) -> Optional[float]:
        if not self.fea_capacity:
            return None
        return self.measured / self.fea_capacity

    @property
    def fea_ok(self) -> Optional[bool]:
        r = self.fea_ratio
        return None if r is None else ACCEPTANCE[0] <= r <= ACCEPTANCE[1]


def compare_case(case: ExperimentalCase, workflow=None, settings=None,
                 fea: bool = False, fea_options=None) -> Comparison:
    """C&K capacity of the specimen, plus the FE fallback and/or the IDEA
    StatiCa capacity when requested."""
    comp = Comparison(case=case, analytical_capacity=case.analytical_capacity())
    if fea:
        try:
            res = case.fea_analysis(**(fea_options or {}))
            if res.get("ok") and res.get("capacity"):
                comp.fea_capacity = res["capacity"]
                comp.fea_result = res
                comp.settings["fea_plate_model"] = res.get("plate_model")
                comp.settings["fea_failure_mode"] = res.get("failure_mode")
            else:
                comp.fea_error = res.get("message", "no FE result")
        except Exception as exc:
            comp.fea_error = f"{type(exc).__name__}: {exc}"
    if workflow is None:
        return comp
    import tempfile
    ci = case.connection_input()
    try:
        with tempfile.TemporaryDirectory() as td:
            res = workflow.analyse(ci, out_dir=td, save_project=False,
                                   save_report=False, settings=settings)
        if res.capacity:
            comp.idea_capacity = res.capacity
            comp.settings = {k.split("/")[-1]: v
                             for k, v in (settings.to_service_dict()
                                          if settings else {}).items()}
        else:
            comp.idea_error = "; ".join(res.checks) or "no result"
    except Exception as exc:
        comp.idea_error = f"{type(exc).__name__}: {exc}"
    return comp


#: parameter sets tried, in order, while the service produces no usable result
TUNING_STEPS: Sequence[Tuple[str, Dict[str, object]]] = (
    ("defaults", {}),
    ("coarse mesh", {"mesh_max_element": 0.02, "mesh_min_element": 0.005,
                     "elements_per_plate": 10}),
    ("fine mesh", {"mesh_max_element": 0.005, "mesh_min_element": 0.001,
                   "elements_per_plate": 60}),
    ("multi-linear material", {"material_model": "multiLinear"}),
    ("detailing off", {"detailing": False, "distance_between_bolts": 2.0}),
)


def tune_and_compare(case: ExperimentalCase, workflow,
                     steps: Sequence[Tuple[str, Dict[str, object]]] = TUNING_STEPS
                     ) -> Comparison:
    """Try the tuning steps until IDEA returns a capacity for the specimen."""
    from .idea_service import IdeaSettings
    last = None
    for label, params in steps:
        comp = compare_case(case, workflow, IdeaSettings(**params))
        comp.settings = dict(params, step=label)
        if comp.idea_capacity:
            return comp
        last = comp
    return last


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def format_comparison(comparisons: Sequence[Comparison]) -> str:
    show_fea = any(c.fea_capacity or c.fea_error for c in comparisons)
    show_idea = any(c.idea_capacity or c.idea_error for c in comparisons)
    head = (f"{'specimen':8s} {'measured':>9s} {'C&K':>9s} {'m/C&K':>7s} ")
    if show_fea:
        head += f"{'FEA':>9s} {'m/FEA':>7s} "
    if show_idea:
        head += f"{'IDEA':>9s} {'m/IDEA':>7s} "
    lines = [head + " note"]
    for c in comparisons:
        line = (f"{c.case.name:8s} {c.measured/1e3:9.1f} "
                f"{c.analytical_capacity/1e3:9.1f} "
                f"{c.analytical_ratio:7.3f} ")
        if show_fea:
            fea = (f"{c.fea_capacity/1e3:9.1f}" if c.fea_capacity
                   else "        -")
            fr = (f"{c.fea_ratio:7.3f}" if c.fea_ratio is not None
                  else "      -")
            line += f"{fea} {fr} "
        if show_idea:
            idea = (f"{c.idea_capacity/1e3:9.1f}" if c.idea_capacity
                    else "        -")
            ir = (f"{c.idea_ratio:7.3f}" if c.idea_ratio is not None
                  else "      -")
            line += f"{idea} {ir} "
        note = c.fea_error or c.idea_error or c.case.note
        lines.append(line + f" {note}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Compare the program with experimental data only.")
    ap.add_argument("--idea", metavar="URL",
                    help="IDEA StatiCa Connection service URL, e.g. "
                         "http://localhost:5000 - runs the IDEA side as well")
    ap.add_argument("--tune", action="store_true",
                    help="try the documented parameter sets when IDEA fails")
    ap.add_argument("--fea", action="store_true",
                    help="also run the nonlinear OpenSees fallback")
    ap.add_argument("--fea-mesh", type=float, default=0.008,
                    help="FE target element size [m] (default 0.008)")
    ap.add_argument("--fea-linear", action="store_true",
                    help="use elastic plates in the FE fallback")
    ap.add_argument("--calibrate", action="store_true",
                    help="calibrate mu/lambda against the specimens before "
                         "the comparison")
    args = ap.parse_args(argv)

    if args.calibrate:
        from .calibration import calibrate_ck_parameters
        result = calibrate_ck_parameters()
        print(result.format())
        print()

    cases = experimental_cases()
    workflow = None
    if args.idea:
        from .idea_service import IdeaWorkflow
        workflow = IdeaWorkflow(base_url=args.idea, logger=lambda s: None)
        workflow.connect()
    try:
        comparisons = []
        for case in cases:
            if workflow and args.tune:
                comp = tune_and_compare(case, workflow)
                if args.fea:
                    comp2 = compare_case(case, fea=True,
                                         fea_options=dict(
                                             mesh=args.fea_mesh,
                                             plate_nonlinear=not args.fea_linear))
                    comp.fea_capacity = comp2.fea_capacity
                    comp.fea_error = comp2.fea_error
                    comp.fea_result = comp2.fea_result
            else:
                comp = compare_case(
                    case, workflow, fea=args.fea,
                    fea_options=dict(mesh=args.fea_mesh,
                                     plate_nonlinear=not args.fea_linear))
            comparisons.append(comp)
            print(f"  {case.name}: done")
        print()
        print("Comparison with experimental failure loads "
              "(ratio = measured / predicted, 1.0 = exact)")
        print(format_comparison(comparisons))

        ratios = [c.analytical_ratio for c in comparisons]
        print(f"\nC&K vs experiment: mean {sum(ratios)/len(ratios):.3f} "
              f"(Crawford & Kulak report 5-14 % over-prediction, i.e. "
              f"{CK_BAND[0]:.2f}-{CK_BAND[1]:.2f}; Abu-Yosef et al. 0.90 +/- 0.04)")
        fea = [c for c in comparisons if c.fea_ratio is not None]
        if fea:
            print(f"FEA vs experiment: {len(fea)}/{len(comparisons)} cases, "
                  f"mean {sum(c.fea_ratio for c in fea)/len(fea):.3f}")
        elif args.fea:
            print("FEA vs experiment: no case produced a capacity - see the "
                  "per-specimen note above")
        idea = [c for c in comparisons if c.idea_ratio is not None]
        if idea:
            print(f"IDEA vs experiment: {len(idea)}/{len(comparisons)} cases, "
                  f"mean {sum(c.idea_ratio for c in idea)/len(idea):.3f}")
        elif args.idea:
            print("IDEA vs experiment: no case produced a capacity - see the "
                  "per-specimen note above")
    finally:
        if workflow is not None:
            workflow.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
