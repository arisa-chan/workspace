#!/usr/bin/env python3
"""Entry point of the EccentricBolt GUI.

Usage:
    python main.py               launch the GUI
    python main.py --selftest    headless self test (analysis + verification
                                 suite + mock REST pipeline), exit code 0/1
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
