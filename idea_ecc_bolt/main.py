#!/usr/bin/env python3
"""Entry point of the EccentricBolt GUI.

Usage:
    python main.py               launch the GUI
    python main.py --selftest    headless self test (analysis + verification
                                 suite + mock REST pipeline), exit code 0/1
    python main.py --fea-selftest  headless check of the OpenSees fallback
    python main.py --smoke       headless GUI smoke test
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def selftest() -> int:
    from eccbolt.analysis import analyze
    from eccbolt.presets import (PRESETS, brandt_example_1, brandt_example_2)
    from eccbolt.validation import run_checks
    ok = True
    for name, fn in PRESETS.items():
        a = analyze(fn())
        print(f"[preset] {name}: C={a.icr.C:.3f}  Pu={a.Pu/1e3:.1f} kN  "
              f"util={a.utilization:.3f}  converged={a.icr.converged}")
        ok &= a.icr.converged
    r1 = analyze(brandt_example_1())
    r2 = analyze(brandt_example_2())
    print(f"[check ] Brandt Ex.1 C={r1.icr.C:.3f} (1.40)  Ex.2 C={r2.icr.C:.3f} (1.10)")
    ok &= abs(r1.icr.C - 1.40) < 0.01 and abs(r2.icr.C - 1.10) < 0.01
    for name, exp, got, passed in run_checks(include_pipeline=True,
                                             full_table=False):
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: expected {exp}, got {got}")
        ok &= passed
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def fea_selftest() -> int:
    """Headless check that the bundled OpenSees fallback runs."""
    from eccbolt.fe_fallback import model_from_connection, solve
    from eccbolt.presets import crawford_kulak_example
    ci = crawford_kulak_example()
    model = model_from_connection(ci, mesh=0.02)
    res = solve(model)
    ok = bool(res.get("ok") and res.get("capacity"))
    print(f"[fea] plate model: {res.get('plate_model')}")
    print(f"[fea] failure mode: {res.get('failure_mode')}")
    print(f"[fea] capacity: {res.get('capacity')}")
    if not ok:
        print(f"[fea] error: {res.get('message')}")
    print("FEA SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def smoke() -> int:
    """Headless GUI smoke test: build the window, analyse, grab a PNG."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from eccbolt.gui import MainWindow
    from eccbolt.presets import crawford_kulak_example
    app = QApplication([])
    w = MainWindow()
    w.suppress_dialogs = True
    w.set_model(crawford_kulak_example())
    w.run_analytical()
    w.grab().save("smoke_gui.png")
    print("SMOKE OK - window built, analysis run, smoke_gui.png written")
    return 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--fea-selftest" in sys.argv:
        sys.exit(fea_selftest())
    if "--smoke" in sys.argv:
        sys.exit(smoke())
    from PySide6.QtWidgets import QApplication
    from eccbolt.gui import MainWindow
    app = QApplication(sys.argv[1:])
    app.setApplicationName("EccentricBolt")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
