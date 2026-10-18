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

**Crawford & Kulak (1971)** - eight full-size tests; the paper reports that the
analytical predictions ran 5-14 % above the observed failure loads, which is
used below as the reference band for the method's systematic bias.

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

    def connection_input(self, load_factor: float = 1.0) -> ConnectionInput:
        """A :class:`ConnectionInput` that reproduces the specimen."""
        height = max(self.plate_height,
                     max(b[1] for b in self.bolts_xy) * 2 + 0.1)
        width = max(self.plate_width,
                    max(b[0] for b in self.bolts_xy) * 2 + 0.1)
        mat = SteelMaterial(f"measured Fu={self.plate_fu/1e6:.0f} MPa",
                            fy=0.85 * self.plate_fu, fu=self.plate_fu)
        return ConnectionInput(
            title=f"{self.source} {self.name}",
            design_code="AISC",
            bolts_xy=list(self.bolts_xy),
            bolt_diameter=self.bolt_diameter,
            plate_a=Plate("Support plate", width=width, height=height,
                          thickness=self.plate_thickness, material=mat),
            plate_b=Plate("Loaded plate", width=width * 1.5, height=height,
                          thickness=self.plate_thickness, material=mat),
            bolt_mat=BoltMaterial("A325", fub=825e6, fyb=580e6,
                                  rult=self.bolt_rult),
            load=LoadSpec(P=self.failure_load * load_factor,
                          theta=0.0, e=self.eccentricity))

    def analytical_capacity(self) -> float:
        """Crawford & Kulak ultimate strength of the specimen [N]."""
        c = solve_from_eccentricity(self.bolts_xy, (0.0, -1.0),
                                    self.eccentricity).C
        return c * self.bolt_rult


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
            failure_load=p * 1e3, note=note))
    return cases


def experimental_cases() -> List[ExperimentalCase]:
    """All experimental specimens the program can model."""
    return badawi_cases()


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
@dataclass
class Comparison:
    case: ExperimentalCase
    analytical_capacity: float                 # N, C&K
    idea_capacity: Optional[float] = None      # N, from the IDEA StatiCa API
    idea_error: str = ""
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


def compare_case(case: ExperimentalCase, workflow=None, settings=None
                 ) -> Comparison:
    """C&K capacity of the specimen, plus the IDEA StatiCa capacity when a
    connected workflow is supplied."""
    comp = Comparison(case=case, analytical_capacity=case.analytical_capacity())
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
    lines = [f"{'specimen':8s} {'measured':>9s} {'C&K':>9s} {'m/C&K':>7s} "
             f"{'IDEA':>9s} {'m/IDEA':>7s}  note"]
    for c in comparisons:
        idea = f"{c.idea_capacity/1e3:9.1f}" if c.idea_capacity else "        -"
        ratio = f"{c.idea_ratio:7.3f}" if c.idea_ratio is not None else "      -"
        note = c.idea_error or c.case.note
        lines.append(f"{c.case.name:8s} {c.measured/1e3:9.1f} "
                     f"{c.analytical_capacity/1e3:9.1f} "
                     f"{c.analytical_ratio:7.3f} {idea} {ratio}  {note}")
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
    args = ap.parse_args(argv)

    cases = experimental_cases()
    workflow = None
    if args.idea:
        from .idea_service import IdeaWorkflow
        workflow = IdeaWorkflow(base_url=args.idea, logger=lambda s: None)
        workflow.connect()
    try:
        comparisons = []
        for case in cases:
            comp = (tune_and_compare(case, workflow)
                    if workflow and args.tune else compare_case(case, workflow))
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
