"""Verification suite: checks the implemented Crawford & Kulak (1971)
instantaneous-centre-of-rotation method against published values.

Checked values
--------------
* Brandt (1982) Examples 1 & 2 - worked examples of the C&K method,
  cross-checked in the original paper against the AISC 8th-edition tables
  (which were generated from Crawford & Kulak, 1971).
* Table 13.1 of Kulak, Fisher & Struik, "Guide to Design Criteria for Bolted
  and Riveted Joints" (2nd ed.) = AISC 8th ed. C-coefficients, generated with
  the C&K method (Rult = 74 kip double shear, delta_u = 0.34 in).
* Scale invariance of the C&K coefficient.
* Concentric limit C = n.
* End-to-end REST pipeline against the bundled mock of the IDEA StatiCa
  Connection API.
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import List, Tuple

import numpy as np

from .icr import solve_from_eccentricity, solve_icr
from .model import INCH

from .ck_table import L, N, TABLE


def _bolts_row(n, b_in):
    return [(0.0, (i - (n - 1) / 2) * b_in * INCH) for i in range(n)]


def run_checks(include_pipeline: bool = True, full_table: bool = False):
    checks: List[Tuple[str, str, str, bool]] = []

    def add(name, expected, got, ok):
        checks.append((name, expected, got, bool(ok)))

    # 1 - Brandt / C&K example 1 -------------------------------------------
    r = solve_from_eccentricity(_bolts_row(3, 3), (0, -1), 4 * INCH)
    add("Brandt Ex.1 ultimate coefficient Cu", "1.40", f"{r.C:.3f}",
        abs(r.C - 1.40) < 0.01)
    add("Brandt Ex.1 elastic coefficient Ce", "1.342", f"{r.elastic_C:.3f}",
        abs(r.elastic_C - 1.342) < 0.01)

    # 2 - Brandt / C&K example 2 (inclined load) ----------------------------
    bolts = [(x * INCH, y * INCH) for x in (-3, 3) for y in (-3, 0, 3)]
    r2 = solve_icr(bolts, (0.6, -0.8), (20 * INCH, 5 * INCH))
    add("Brandt Ex.2 ultimate coefficient Cu", "1.10", f"{r2.C:.3f}",
        abs(r2.C - 1.10) < 0.01)
    add("Brandt Ex.2 elastic coefficient Ce", "0.942", f"{r2.elastic_C:.3f}",
        abs(r2.elastic_C - 0.942) < 0.01)

    # 3 - C-coefficient table (C&K method, AISC 8th ed.) ---------------------
    entries = []
    for b, rows in TABLE.items():
        for l, vals in rows.items():
            for n, ctab in zip(N, vals):
                entries.append((b, l, n, ctab))
    if not full_table:
        entries = [e for e in entries if (e[1], e[2]) in
                   {(3, 2), (4, 3), (16, 4), (16, 6), (24, 5), (3, 12), (36, 12), (6, 8)}]
    worst = 0.0
    worst_case = None
    for b, l, n, ctab in entries:
        rr = solve_from_eccentricity(_bolts_row(n, b), (0, -1), l * INCH)
        err = abs(rr.C - ctab) / ctab
        if err > worst:
            worst, worst_case = err, (b, l, n, rr.C, ctab)
    b, l, n, cval, ctab = worst_case
    add(f"C-table ({len(entries)} entries, C&K/AISC-8th) max dev. "
        f"at b={b}\", l={l}\", n={n}", f"{ctab:.2f}", f"{cval:.3f}",
        worst < 0.025)

    # 4 - scale invariance ----------------------------------------------------
    ra = solve_from_eccentricity(_bolts_row(5, 3), (0, -1), 12 * INCH)
    rb = solve_from_eccentricity(_bolts_row(5, 6), (0, -1), 24 * INCH)
    add("Scale invariance C(2x geometry) = C", f"{ra.C:.4f}", f"{rb.C:.4f}",
        abs(ra.C - rb.C) < 1e-6)

    # 5 - concentric limit -----------------------------------------------------
    rc = solve_from_eccentricity(_bolts_row(6, 3), (0, -1), 0.0)
    add("Concentric load limit C = n", "6.000", f"{rc.C:.3f}", abs(rc.C - 6) < 1e-9)

    # 6 - IOM XML well formed ---------------------------------------------------
    from .iom import IomBuilder, parse_open_model
    from .presets import crawford_kulak_example
    ci = crawford_kulak_example()
    root = parse_open_model(IomBuilder(ci).build())
    nb = len(root.find("Connections/ConnectionData/BoltGrids/BoltGrid").find("Positions"))
    add("IOM XML well-formed, bolt positions exported", str(ci.n_bolts), str(nb),
        nb == ci.n_bolts)

    # 7 - REST pipeline against the mock IDEA service ---------------------------
    if include_pipeline:
        from .idea_service import IdeaWorkflow
        from .mock_service import MockIdeaService
        with tempfile.TemporaryDirectory() as td:
            with MockIdeaService() as svc:
                wf = IdeaWorkflow(base_url=svc.url, logger=lambda s: None)
                wf.connect()
                res = wf.analyse(ci, out_dir=td, save_project=True, save_report=True)
                wf.close()
        util = [b.unity_check for b in res.bolts]
        add("REST pipeline vs mock IDEA service (bolt results)",
            f"{ci.n_bolts} bolts", f"{len(res.bolts)} bolts, max util "
            f"{max(util):.3f}", res.ok and len(res.bolts) == ci.n_bolts and max(util) <= 1.0001)
    return checks


if __name__ == "__main__":
    for name, exp, got, ok in run_checks(full_table=True):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: expected {exp}, got {got}")
