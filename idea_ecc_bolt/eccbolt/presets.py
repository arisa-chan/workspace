"""
Ready-made input sets, including the verification examples of the
Crawford & Kulak (1971) ultimate-strength (instantaneous centre of rotation)
method.

References
----------
.. [1] S. F. Crawford, G. L. Kulak, "Eccentrically Loaded Bolted Connections",
       J. Struct. Div. ASCE 97(ST3), 1971, pp. 765-783.
.. [2] G. D. Brandt, "Rapid Determination of Ultimate Strength of Eccentrically
       Loaded Bolt Groups", Engineering Journal, AISC, Q2 1982.  (Examples 1 & 2
       reproduce the C&K method and are cross-checked against the AISC 8th ed.
       tables generated from [1].)
.. [3] G. L. Kulak, J. W. Fisher, J. H. A. Struik, "Guide to Design Criteria for
       Bolted and Riveted Joints", 2nd ed., 2001, Ch. 13 (Table 13.1 = AISC 8th
       ed. C coefficients, generated with Rult = 74 kip double shear and
       delta_u = 0.34 in from [1]).
"""
from __future__ import annotations

import math

from .model import (BoltMaterial, ConnectionInput, INCH, KIP, KSI, LoadSpec,
                    Plate, SteelMaterial)

KIP_IN = KIP * INCH


def _a572_50():
    return SteelMaterial("A572 Gr.50", 50 * KSI, 65 * KSI)


def _a325(d_in: float, rult_kip: float | None = None):
    return BoltMaterial("A325", fub=120 * KSI, fyb=92 * KSI,
                        rult=(rult_kip * KIP if rult_kip else None))


# ---------------------------------------------------------------------------
def brandt_example_1() -> ConnectionInput:
    """C&K method worked example (Brandt 1982, Ex. 1 / Fig. 5):
    one vertical row of 3 bolts, s = 3 in, vertical load, e = 4 in.
    Published: Ce = 1.342 (elastic), Cu = 1.40 (ultimate, AISC 8th ed. table)."""
    s = 3 * INCH
    ci = ConnectionInput(
        title="Crawford & Kulak method - Brandt Example 1 (3 bolts, e=4 in)",
        design_code="AISC",
        bolts_xy=[(0.0, s), (0.0, 0.0), (0.0, -s)],
        bolt_diameter=0.75 * INCH,
        plate_a=Plate("Support plate", width=6 * INCH, height=14 * INCH,
                      thickness=0.5 * INCH, material=_a572_50()),
        plate_b=Plate("Loaded plate", width=10 * INCH, height=12 * INCH,
                      thickness=0.5 * INCH, material=_a572_50(),
                      cx=2 * INCH, cy=3 * INCH),
        bolt_mat=_a325(0.75, rult_kip=37.0),
        load=LoadSpec(P=20 * KIP, theta=0.0, e=4 * INCH))
    return ci


def brandt_example_2() -> ConnectionInput:
    """C&K method worked example with inclined load (Brandt 1982, Ex. 2 / Fig. 6):
    6 bolts (2 columns x 3 rows, 6 in x 3 in), load direction (0.6, -0.8),
    load point (20 in, 5 in) from the centroid.  Published: Cu = 1.10."""
    ci = ConnectionInput(
        title="Crawford & Kulak method - Brandt Example 2 (inclined load)",
        design_code="AISC",
        bolts_xy=[(x * INCH, y * INCH)
                  for x in (-3.0, 3.0) for y in (-3.0, 0.0, 3.0)],
        bolt_diameter=0.75 * INCH,
        plate_a=Plate("Support plate", width=12 * INCH, height=12 * INCH,
                      thickness=0.5 * INCH, material=_a572_50()),
        plate_b=Plate("Loaded plate", width=26 * INCH, height=12 * INCH,
                      thickness=0.5 * INCH, material=_a572_50(), cx=8 * INCH),
        bolt_mat=_a325(0.75, rult_kip=37.0),
        load=LoadSpec(P=20 * KIP, theta=math.degrees(math.atan2(0.6, 0.8)),
                      e=20 * INCH, h_load=5 * INCH))
    return ci


def crawford_kulak_example() -> ConnectionInput:
    """Default demonstration model in the spirit of the Crawford & Kulak (1971)
    test specimens [1]: two plates joined by a bolt group of two vertical rows,
    loaded in the plate plane at an eccentricity; bolt data of the C&K test
    series (3/4 in A325, Rult = 74 kip in double shear [3])."""
    ci = ConnectionInput(
        title="Crawford & Kulak (1971) type specimen - 2x4 bolts, e = 12 in",
        design_code="AISC",
        bolts_xy=[(x * INCH, y * INCH)
                  for x in (-2.75, 2.75) for y in (-4.5, -1.5, 1.5, 4.5)],
        bolt_diameter=0.75 * INCH,
        n_shear_planes=1,
        plate_a=Plate("Support plate", width=12 * INCH, height=18 * INCH,
                      thickness=3 / 8 * INCH, material=_a572_50()),
        plate_b=Plate("Loaded plate", width=18 * INCH, height=16 * INCH,
                      thickness=3 / 8 * INCH, material=_a572_50(),
                      cx=4 * INCH, cy=2 * INCH),
        bolt_mat=_a325(0.75, rult_kip=37.0),
        load=LoadSpec(P=60 * KIP, theta=0.0, e=12 * INCH))
    return ci


def aisc_bracket_2x6() -> ConnectionInput:
    """Bolt group of AISC Design Example II.A-24 / IDEA StatiCa AISC
    verification 'Bracket Plate Connections': 2 vertical rows of 6 bolts,
    g = 5.5 in, s = 3 in, 7/8 in A325-N, e = 16 in, plates A572 Gr.50."""
    ys = [-7.5, -4.5, -1.5, 1.5, 4.5, 7.5]
    ci = ConnectionInput(
        title="Bracket plate bolt group (AISC II.A-24 layout), e = 16 in",
        design_code="AISC",
        bolts_xy=[(x * INCH, y * INCH) for x in (-2.75, 2.75) for y in ys],
        bolt_diameter=7 / 8 * INCH,
        plate_a=Plate("Column-side plate", width=12 * INCH, height=24 * INCH,
                      thickness=5 / 8 * INCH, material=_a572_50()),
        plate_b=Plate("Bracket plate", width=24 * INCH, height=22 * INCH,
                      thickness=5 / 8 * INCH, material=_a572_50(), cx=7 * INCH),
        bolt_mat=_a325(7 / 8, rult_kip=44.7),   # 0.62*fub*Ab for 7/8" A325
        load=LoadSpec(P=100 * KIP, theta=0.0, e=16 * INCH))
    return ci


PRESETS = {
    "Crawford & Kulak (1971) type specimen (2x4 bolts)": crawford_kulak_example,
    "C&K method - Brandt Ex. 1 (3 bolts, e=4 in, C=1.40)": brandt_example_1,
    "C&K method - Brandt Ex. 2 (inclined load, C=1.10)": brandt_example_2,
    "Bracket bolt group (AISC II.A-24 layout, e=16 in)": aisc_bracket_2x6,
}
