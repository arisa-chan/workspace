"""Headless smoke test of the PySide6 GUI (offscreen platform)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from eccbolt.gui import MainWindow  # noqa: E402
from eccbolt.presets import (aisc_bracket_2x6, brandt_example_1,
                             brandt_example_2, crawford_kulak_example)  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_gui_smoke(app):
    w = MainWindow()
    w.suppress_dialogs = True
    w.show()
    cases = [(crawford_kulak_example(), "2.39"),
             (brandt_example_1(), "1.40"),
             (brandt_example_2(), "1.09"),
             (aisc_bracket_2x6(), "3.55")]
    for ci, c_exp in cases:
        w.set_model(ci)
        w.run_analytical()
        assert w.lbl["C"].text().startswith(c_exp), (ci.title, w.lbl["C"].text())
        assert w.res_table.rowCount() == ci.n_bolts
    w.toggle_units()
    assert w.sp_e.value() == pytest.approx(16.0, abs=1e-6)   # imperial inches
    w.toggle_units()
    w.close()


def test_gui_idea_mock(app, tmp_path):
    from eccbolt.gui import IdeaWorker
    w = MainWindow()
    w.suppress_dialogs = True
    w.set_model(crawford_kulak_example())
    w.run_analytical()
    wk = IdeaWorker(w.ci, dict(mock=True, out_dir=str(tmp_path)))
    wk.run()                      # synchronous (no event loop needed)
    assert wk.error is None, wk.error
    assert wk.result.ok and len(wk.result.bolts) == w.ci.n_bolts
