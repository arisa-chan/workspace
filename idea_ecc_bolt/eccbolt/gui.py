"""
PySide6 GUI for the analysis of eccentrically loaded bolted steel shear
connections with IDEA StatiCa (Connection REST API) and the Crawford & Kulak
(1971) instantaneous-centre-of-rotation method.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, is_dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QSettings
from PySide6.QtGui import QAction, QDoubleValidator
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .analysis import analyze
from .model import (KIP, KSI, MM, INCH, BoltMaterial, ConnectionInput,
                    LoadSpec, Plate, SteelMaterial)
from .view2d import Connection2DView
from .view3d import Connection3DView

APP_NAME = "EccentricBolt"


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------
class Units:
    def __init__(self, system="metric"):
        self.system = system

    @property
    def metric(self):
        return self.system == "metric"

    def len(self, v_si, to_si=False):
        f = 1000.0 if self.metric else 1 / INCH
        return v_si * f if not to_si else v_si / f

    def force(self, v_si, to_si=False):
        f = 1e-3 if self.metric else 1 / KIP
        return v_si * f if not to_si else v_si / f

    def stress(self, v_si, to_si=False):
        f = 1e-6 if self.metric else 1 / KSI
        return v_si * f if not to_si else v_si / f

    def mu(self, v_si, to_si=False):
        """C&K mu: stored in 1/m, displayed in 1/in."""
        return v_si * INCH if not to_si else v_si / INCH

    @property
    def u_len(self):
        return "mm" if self.metric else "in"

    @property
    def u_force(self):
        return "kN" if self.metric else "kip"

    @property
    def u_stress(self):
        return "MPa" if self.metric else "ksi"


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------
class IdeaWorker(QThread):
    log = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)
    service_missing = Signal(str)

    def __init__(self, ci, settings, parent=None):
        super().__init__(parent)
        self.ci = ci
        self.settings = settings
        self.result = None
        self.error = None

    def _log(self, msg):
        self.log.emit(msg)

    def run(self):
        try:
            from .idea_service import IdeaSettings, IdeaWorkflow
            from .mock_service import MockIdeaService
            out = self.settings.get("out_dir", os.path.join(os.getcwd(), "out"))
            os.makedirs(out, exist_ok=True)
            idea_settings = IdeaSettings(
                mesh_max_element=self.settings.get("mesh_max") or None,
                mesh_min_element=(self.settings.get("mesh_max") / 4.0
                                  if self.settings.get("mesh_max") else None),
                elements_per_plate=self.settings.get("elements_per_plate") or None,
                detailing=self.settings.get("detailing"),
                material_model=self.settings.get("material_model") or None)
            if self.settings.get("mock"):
                with MockIdeaService() as svc:
                    self._log("Using built-in MOCK of the IDEA StatiCa Connection "
                              "REST API (offline demonstration).")
                    wf = IdeaWorkflow(base_url=svc.url, logger=self._log)
                    wf.connect()
                    res = wf.analyse(self.ci, out_dir=out, settings=idea_settings)
                    wf.close()
            else:
                from eccbolt.idea_service import (IdeaImportError,
                                                  IdeaServiceNotRunning)
                wf = IdeaWorkflow(base_url=self.settings.get("base_url"),
                                  setup_dir=self.settings.get("setup_dir") or None,
                                  logger=self._log,
                                  auto_start=bool(self.settings.get("auto_start", True)))
                try:
                    wf.connect()
                except IdeaServiceNotRunning as exc:
                    self.error = str(exc)
                    self.service_missing.emit(str(exc))
                    return
                try:
                    res = wf.analyse(self.ci, out_dir=out,
                                     settings=idea_settings)
                except IdeaImportError as exc:
                    self.error = str(exc)
                    self.failed.emit(str(exc))
                    return
                wf.close()
            self.result = res
            self.finished_ok.emit(res)
        except Exception as exc:
            import traceback
            self.error = traceback.format_exc()
            self.failed.emit(self.error)


class ValidationWorker(QThread):
    finished_ok = Signal(object)

    def __init__(self, full_table, parent=None):
        super().__init__(parent)
        self.full = full_table

    def run(self):
        from .validation import run_checks
        self.finished_ok.emit(run_checks(include_pipeline=True,
                                         full_table=self.full))


class FeaWorker(QThread):
    log = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model

    def run(self):
        try:
            from .fe_fallback import solve
            self.log.emit("Running OpenSees FE fallback (bolt holes, "
                          "clearance, J2 plates, vector C&K bolts)...")
            self.finished_ok.emit(solve(self.model, verbose=False))
        except Exception as exc:
            import traceback
            self.failed.emit(traceback.format_exc())


class CalibrationWorker(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            from .calibration import calibrate_ck_parameters
            self.finished_ok.emit(calibrate_ck_parameters())
        except Exception as exc:
            import traceback
            self.failed.emit(traceback.format_exc())


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Eccentrically Loaded Bolted Shear Connections - "
                            "IDEA StatiCa + Crawford & Kulak (1971)")
        self.resize(1500, 900)
        self.units = Units("metric")
        self.ci: Optional[ConnectionInput] = None
        self.ana = None
        self.fields = []          # (spinbox, kind, getter, setter)
        self._loading = False
        self.settings = QSettings("EccentricBolt", "gui")
        self._build_ui()
        from .presets import crawford_kulak_example
        self.set_model(crawford_kulak_example())

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self._build_menus()
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_inputs())
        center = QTabWidget()
        self.view3d = Connection3DView()
        self.view2d = Connection2DView()
        self.log_view = QPlainTextEdit(readOnly=True)
        center.addTab(self.view3d, "3D connection view")
        center.addTab(self.view2d, "Bolt group (2D)")
        center.addTab(self.log_view, "Log")
        split.addWidget(center)
        split.addWidget(self._build_results())
        split.setSizes([380, 640, 420])
        self.setCentralWidget(split)
        self.statusBar().showMessage("Ready")

    def _build_menus(self):
        m_file = self.menuBar().addMenu("&File")
        m_presets = m_file.addMenu("Load &preset example")
        from .presets import PRESETS
        for name, fn in PRESETS.items():
            act = QAction(name, self)
            act.triggered.connect(lambda _=False, fn=fn: self.set_model(fn()))
            m_presets.addAction(act)
        act = QAction("&Open input JSON...", self)
        act.triggered.connect(self.open_json)
        m_file.addAction(act)
        act = QAction("&Save input JSON...", self)
        act.triggered.connect(self.save_json)
        m_file.addAction(act)
        act = QAction("Export &IOM XML...", self)
        act.triggered.connect(self.export_iom)
        m_file.addAction(act)
        m_file.addSeparator()
        m_file.addAction(QAction("E&xit", self, triggered=self.close))

        m_run = self.menuBar().addMenu("&Analysis")
        act = QAction("Run &Crawford && Kulak (ICR) analysis", self)
        act.triggered.connect(self.run_analytical)
        m_run.addAction(act)
        act = QAction("Run &IDEA StatiCa analysis...", self)
        act.triggered.connect(self.run_idea)
        m_run.addAction(act)
        act = QAction("Run &FE fallback (OpenSees)...", self)
        act.triggered.connect(self.run_fea)
        m_run.addAction(act)
        act = QAction("&Calibrate C&&K parameters...", self)
        act.triggered.connect(self.calibrate_ck)
        m_run.addAction(act)
        act = QAction("Run &verification suite...", self)
        act.triggered.connect(self.run_validation)
        m_run.addAction(act)

        m_set = self.menuBar().addMenu("&Settings")
        act = QAction("&Units (metric / imperial)", self)
        act.triggered.connect(self.toggle_units)
        m_set.addAction(act)
        act = QAction("&IDEA StatiCa connection...", self)
        act.triggered.connect(self.edit_settings)
        m_set.addAction(act)

        tb = self.addToolBar("Main")
        for txt, fn in (("Analyze C&K", self.run_analytical),
                        ("Run FE fallback", self.run_fea),
                        ("Run IDEA", self.run_idea),
                        ("Calibrate", self.calibrate_ck),
                        ("Verify", self.run_validation),
                        ("Refresh view", self.update_views)):
            a = QAction(txt, self)
            a.triggered.connect(fn)
            tb.addAction(a)

    # ------------------------------------------------------------- inputs
    def _spin(self, kind, decimals=2, lo=-1e7, hi=1e7):
        sp = QDoubleSpinBox()
        sp.setDecimals(decimals)
        sp.setRange(lo, hi)
        sp.setSuffix(" " + self._suffix(kind))
        sp.setProperty("kind", kind)
        return sp

    def _suffix(self, kind):
        return {"len": self.units.u_len, "force": self.units.u_force,
                "stress": self.units.u_stress, "angle": "deg",
                "mu": "1/in", "dim": ""}.get(kind, "")

    def _bind(self, spin, kind, getter, setter):
        self.fields.append((spin, kind, getter, setter))
        spin.valueChanged.connect(lambda _v, s=spin: self._field_edited(s))

    def _field_edited(self, spin):
        if self._loading:
            return
        kind = spin.property("kind")
        val = spin.value()
        if kind == "len":
            val = self.units.len(val, to_si=True)
        elif kind == "force":
            val = self.units.force(val, to_si=True)
        elif kind == "stress":
            val = self.units.stress(val, to_si=True)
        elif kind == "mu":
            val = self.units.mu(val, to_si=True)
        for s, k, g, st in self.fields:
            if s is spin:
                st(val)
                break
        self.update_views()

    def _build_inputs(self):
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(4, 4, 4, 4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        pane = QWidget()
        lay = QVBoxLayout(pane)

        # --- code & bolts -------------------------------------------------
        g = QGroupBox("Bolt group (coordinates in the connection plane)")
        vl = QVBoxLayout(g)
        self.bolt_table = QTableWidget(0, 2)
        self.bolt_table.setHorizontalHeaderLabels(["x", "y"])
        self.bolt_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.bolt_table.setMaximumHeight(220)
        self.bolt_table.itemChanged.connect(self._bolt_table_edited)
        vl.addWidget(self.bolt_table)
        hl = QHBoxLayout()
        for txt, fn in (("Add", self.bolt_add), ("Remove", self.bolt_remove),
                        ("Grid...", self.bolt_grid),
                        ("Random...", self.bolt_random),
                        ("Clear", self.bolt_clear)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            hl.addWidget(b)
        vl.addLayout(hl)
        lay.addWidget(g)

        g = QGroupBox("Bolts")
        fl = QFormLayout(g)
        self.sp_bolt_d = self._spin("len", 3)
        self._bind(self.sp_bolt_d, "len", lambda: self.ci.bolt_diameter,
                   lambda v: setattr(self.ci, "bolt_diameter", v))
        fl.addRow("Diameter d", self.sp_bolt_d)
        self.sp_hole = self._spin("len", 3)
        self._bind(self.sp_hole, "len", lambda: self.ci.bolt_hole_clearance,
                   lambda v: setattr(self.ci, "bolt_hole_clearance", v))
        fl.addRow("Hole clearance", self.sp_hole)
        self.sp_planes = QSpinBox()
        self.sp_planes.setRange(1, 3)
        self.sp_planes.valueChanged.connect(
            lambda v: (setattr(self.ci, "n_shear_planes", v), self.update_views()))
        fl.addRow("Shear planes", self.sp_planes)
        self.chk_threads = QCheckBox("threads in shear plane (N-type)")
        self.chk_threads.toggled.connect(self._threads_toggled)
        fl.addRow(self.chk_threads)
        self.cmb_grade = QComboBox()
        self.cmb_grade.addItems(["A325", "A490", "4.6", "8.8", "10.9", "custom"])
        self.cmb_grade.currentTextChanged.connect(self._grade_changed)
        fl.addRow("Grade", self.cmb_grade)
        self.sp_fub = self._spin("stress", 1)
        self._bind(self.sp_fub, "stress", lambda: self.ci.bolt_mat.fub,
                   lambda v: setattr(self.ci.bolt_mat, "fub", v))
        fl.addRow("Bolt fub", self.sp_fub)
        self.sp_fyb = self._spin("stress", 1)
        self._bind(self.sp_fyb, "stress", lambda: self.ci.bolt_mat.fyb,
                   lambda v: setattr(self.ci.bolt_mat, "fyb", v))
        fl.addRow("Bolt fyb", self.sp_fyb)
        self.sp_rult = self._spin("force", 1)
        self._bind(self.sp_rult, "force",
                   lambda: (self.ci.bolt_mat.rult or 0.0),
                   lambda v: setattr(self.ci.bolt_mat, "rult", v if v > 0 else None))
        fl.addRow("Rult per bolt (0 = auto)", self.sp_rult)
        self.sp_dmax = self._spin("len", 3)
        self._bind(self.sp_dmax, "len", lambda: self.ci.bolt_mat.delta_max,
                   lambda v: setattr(self.ci.bolt_mat, "delta_max", v))
        fl.addRow("Bolt deformation delta_max", self.sp_dmax)
        self.sp_mu = self._spin("mu", 3, 0.01, 1e6)
        self._bind(self.sp_mu, "mu", lambda: self.ci.bolt_mat.mu,
                   lambda v: setattr(self.ci.bolt_mat, "mu", v))
        fl.addRow("C&K mu (10 = original)", self.sp_mu)
        self.sp_lambda = self._spin("dim", 4, 0.01, 10.0)
        self._bind(self.sp_lambda, "dim", lambda: self.ci.bolt_mat.ck_lambda,
                   lambda v: setattr(self.ci.bolt_mat, "ck_lambda", v))
        fl.addRow("C&K lambda (0.55 = original)", self.sp_lambda)
        lay.addWidget(g)

        # --- plates ---------------------------------------------------------
        self.plate_widgets = {}
        for key, title in (("plate_a", "Support plate A (bearing member)"),
                           ("plate_b", "Loaded plate B (bolted component)")):
            g = QGroupBox(title)
            fl = QFormLayout(g)
            w = {}
            for lab, kind, attr in (("Width b", "len", "width"),
                                    ("Height h", "len", "height"),
                                    ("Thickness t", "len", "thickness"),
                                    ("Centre offset cx", "len", "cx"),
                                    ("Centre offset cy", "len", "cy")):
                sp = self._spin(kind, 3 if kind == "len" else 2)
                self._bind(sp, kind,
                           lambda k=key, a=attr: getattr(getattr(self.ci, k), a),
                           lambda v, k=key, a=attr: setattr(getattr(self.ci, k), a, v))
                fl.addRow(lab, sp)
                w[attr] = sp
            for lab, attr in (("Designation", None), ("fy", "fy"), ("fu", "fu"),
                              ("E", "E")):
                if attr is None:
                    from PySide6.QtWidgets import QLineEdit
                    le = QLineEdit()
                    le.editingFinished.connect(
                        lambda k=key, le=le: setattr(
                            getattr(self.ci, k).material, "designation", le.text()))
                    fl.addRow(lab, le)
                    w["desig"] = le
                else:
                    sp = self._spin("stress", 1)
                    self._bind(sp, "stress",
                               lambda k=key, a=attr: getattr(
                                   getattr(self.ci, k).material, a),
                               lambda v, k=key, a=attr: setattr(
                                   getattr(self.ci, k).material, a, v))
                    fl.addRow(lab, sp)
                    w[attr] = sp
            self.plate_widgets[key] = w
            lay.addWidget(g)

        # --- load -----------------------------------------------------------
        g = QGroupBox("Load")
        fl = QFormLayout(g)
        self.sp_P = self._spin("force", 2)
        self._bind(self.sp_P, "force", lambda: self.ci.load.P,
                   lambda v: setattr(self.ci.load, "P", v))
        fl.addRow("Magnitude P", self.sp_P)
        self.sp_theta = self._spin("angle", 2)
        self._bind(self.sp_theta, "angle", lambda: self.ci.load.theta,
                   lambda v: setattr(self.ci.load, "theta", v))
        fl.addRow("Angle from vertical theta", self.sp_theta)
        self.sp_e = self._spin("len", 2)
        self._bind(self.sp_e, "len", lambda: self.ci.load.e,
                   lambda v: setattr(self.ci.load, "e", v))
        fl.addRow("Eccentricity e (horiz.)", self.sp_e)
        self.sp_h = self._spin("len", 2)
        self._bind(self.sp_h, "len",
                   lambda: (self.ci.load.h_load if self.ci.load.h_load is not None else 0.0),
                   lambda v: setattr(self.ci.load, "h_load", v))
        fl.addRow("Load height h above c.g.", self.sp_h)
        self.chk_hauto = QCheckBox("automatic (top edge of plate B)")
        self.chk_hauto.toggled.connect(self._hauto_toggled)
        fl.addRow(self.chk_hauto)
        lay.addWidget(g)

        self.cmb_code = QComboBox()
        self.cmb_code.addItems(["AISC", "EC3"])
        self.cmb_code.currentTextChanged.connect(
            lambda t: (setattr(self.ci, "design_code", t), self.update_views()))
        lay.addWidget(self.cmb_code)
        lay.addStretch(1)
        scroll.setWidget(pane)
        outer.addWidget(scroll)
        return wrap

    # ------------------------------------------------------------- results
    def _build_results(self):
        w = QWidget()
        vl = QVBoxLayout(w)
        g = QGroupBox("Crawford & Kulak (1971) - instantaneous centre method")
        fl = QFormLayout(g)
        self.lbl = {}
        for key, lab in (("C", "Coefficient C = Pu/Rult"),
                         ("Ce", "Elastic coefficient Ce"),
                         ("Pu", "Ultimate load Pu = C*Rult"),
                         ("util", "Utilization P/Pu"),
                         ("ic", "Instantaneous centre (x, y)"),
                         ("M", "Moment at bolt group M"),
                         ("rult", "Bolt resistance Rult"),
                         ("Pcomp", "Load components Pv / Ph"),
                         ("fea", "FE fallback (OpenSees)"),
                         ("fea_capacity", "Connection capacity (FE)"),
                         ("idea", "IDEA StatiCa"),
                         ("capacity", "Connection capacity (IDEA)")):
            self.lbl[key] = QLabel("-")
            self.lbl[key].setTextInteractionFlags(Qt.TextSelectableByMouse)
            fl.addRow(lab, self.lbl[key])
        vl.addWidget(g)
        self.res_table = QTableWidget(0, 10)
        self.res_table.setHorizontalHeaderLabels(
            ["Bolt", "x", "y", "R/Rult @Pu", "F @P", "IDEA V", "IDEA N",
             "IDEA util.", "FE V", "FE util."])
        self.res_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        vl.addWidget(self.res_table, 3)
        self.chk_view = QPlainTextEdit(readOnly=True)
        self.chk_view.setMaximumHeight(180)
        vl.addWidget(self.chk_view, 1)
        return w

    # ------------------------------------------------------------- model io
    def set_model(self, ci: ConnectionInput):
        self.ci = ci
        self.ana = None
        self._loading = True
        for spin, kind, getter, setter in self.fields:
            v = getter()
            if kind == "len":
                v = self.units.len(v)
            elif kind == "force":
                v = self.units.force(v)
            elif kind == "stress":
                v = self.units.stress(v)
            elif kind == "mu":
                v = self.units.mu(v)
            spin.blockSignals(True)
            spin.setValue(v)
            spin.setSuffix(" " + self._suffix(kind))
            spin.blockSignals(False)
        for key in ("plate_a", "plate_b"):
            self.plate_widgets[key]["desig"].setText(
                getattr(self.ci, key).material.designation)
        self.cmb_code.setCurrentText(ci.design_code if ci.design_code in
                                     ("AISC", "EC3") else "AISC")
        self.cmb_grade.setCurrentText(ci.bolt_mat.designation
                                      if ci.bolt_mat.designation in
                                      ["A325", "A490", "4.6", "8.8", "10.9"]
                                      else "custom")
        self.sp_planes.setValue(ci.n_shear_planes)
        self.chk_threads.setChecked(ci.threads_in_shear_plane)
        self.chk_hauto.setChecked(ci.load.h_load is None)
        self.sp_h.setEnabled(ci.load.h_load is not None)
        self._fill_bolt_table()
        self._loading = False
        self.ana = None
        self.update_views()
        self.statusBar().showMessage(f"Loaded: {ci.title}")

    def _fill_bolt_table(self):
        self.bolt_table.blockSignals(True)
        self.bolt_table.setRowCount(self.ci.n_bolts)
        for i, (x, y) in enumerate(self.ci.bolts_xy):
            for j, v in enumerate((x, y)):
                it = QTableWidgetItem(f"{self.units.len(v):.4f}")
                self.bolt_table.setItem(i, j, it)
        self.bolt_table.blockSignals(False)

    def _bolt_table_edited(self, item):
        if self._loading:
            return
        try:
            v = float(item.text())
        except ValueError:
            return
        i, j = item.row(), item.column()
        while len(self.ci.bolts_xy) <= i:
            self.ci.bolts_xy.append((0.0, 0.0))
        x, y = self.ci.bolts_xy[i]
        self.ci.bolts_xy[i] = (self.units.len(v, to_si=True), y) if j == 0 \
            else (x, self.units.len(v, to_si=True))
        self.update_views()

    def bolt_add(self):
        self.ci.bolts_xy.append((0.0, 0.0))
        self._fill_bolt_table()
        self.update_views()

    def bolt_remove(self):
        r = self.bolt_table.currentRow()
        if 0 <= r < len(self.ci.bolts_xy):
            self.ci.bolts_xy.pop(r)
            self._fill_bolt_table()
            self.update_views()

    def bolt_clear(self):
        self.ci.bolts_xy = []
        self._fill_bolt_table()
        self.update_views()

    def bolt_grid(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Bolt grid generator")
        fl = QFormLayout(dlg)
        nx = QSpinBox(); nx.setRange(1, 8); nx.setValue(2)
        ny = QSpinBox(); ny.setRange(1, 16); ny.setValue(4)
        g = QDoubleSpinBox(); g.setRange(0, 10000); g.setValue(140)
        s = QDoubleSpinBox(); s.setRange(0, 10000); g.setDecimals(1)
        s.setValue(75)
        fl.addRow("Columns", nx)
        fl.addRow("Rows", ny)
        fl.addRow(f"Gage [{self.units.u_len}]", g)
        fl.addRow(f"Pitch [{self.units.u_len}]", s)
        bb = QHBoxLayout()
        ok = QPushButton("Generate")
        bb.addWidget(ok)
        fl.addRow(bb)
        ok.clicked.connect(dlg.accept)
        if dlg.exec() == QDialog.Accepted:
            gv = self.units.len(g.value(), to_si=True)
            sv = self.units.len(s.value(), to_si=True)
            self.ci.bolts_xy = [
                ((i - (nx.value() - 1) / 2) * gv, (j - (ny.value() - 1) / 2) * sv)
                for i in range(nx.value()) for j in range(ny.value())]
            self._fill_bolt_table()
            self.update_views()

    def bolt_random(self):
        """Arbitrary (random) bolt pattern - the IDEA BoltGrid accepts any
        free list of hole positions, not only regular grids."""
        from .model import random_bolt_group
        dlg = QDialog(self)
        dlg.setWindowTitle("Random bolt pattern (arbitrary positions)")
        fl = QFormLayout(dlg)
        n = QSpinBox(); n.setRange(1, 40); n.setValue(6)
        wdt = QDoubleSpinBox(); wdt.setRange(1, 10000); wdt.setValue(200)
        hgt = QDoubleSpinBox(); hgt.setRange(1, 10000); hgt.setValue(300)
        md = QDoubleSpinBox(); md.setRange(0, 10000); md.setValue(
            max(2.2 * self.ci.bolt_diameter / INCH if not self.units.metric
                else 2.2 * self.ci.bolt_diameter * 1000, 10))
        seed = QSpinBox(); seed.setRange(0, 99999); seed.setValue(1)
        fl.addRow("Number of bolts", n)
        fl.addRow(f"Field width [{self.units.u_len}]", wdt)
        fl.addRow(f"Field height [{self.units.u_len}]", hgt)
        fl.addRow(f"Min. spacing [{self.units.u_len}]", md)
        fl.addRow("Random seed", seed)
        bb = QHBoxLayout()
        okb = QPushButton("Generate")
        okb.clicked.connect(dlg.accept)
        bb.addWidget(okb)
        fl.addRow(bb)
        if dlg.exec() == QDialog.Accepted:
            try:
                pts = random_bolt_group(
                    n.value(),
                    self.units.len(wdt.value(), to_si=True),
                    self.units.len(hgt.value(), to_si=True),
                    self.units.len(md.value(), to_si=True),
                    seed=seed.value())
            except ValueError as exc:
                QMessageBox.warning(self, "Random pattern", str(exc))
                return
            self.ci.bolts_xy = pts
            self._fill_bolt_table()
            self.update_views()
            self._log(f"Random bolt pattern generated: {len(pts)} bolts "
                      f"(arbitrary positions are fully supported by the "
                      f"IDEA BoltGrid).")

    def _grade_changed(self, txt):
        grades = {"A325": (825e6, 580e6), "A490": (1035e6, 895e6),
                  "4.6": (400e6, 240e6), "8.8": (800e6, 640e6),
                  "10.9": (1000e6, 900e6)}
        if txt in grades:
            self.ci.bolt_mat.designation = txt
            self.ci.bolt_mat.fub, self.ci.bolt_mat.fyb = grades[txt]
            self._loading = True
            self.sp_fub.setValue(self.units.stress(self.ci.bolt_mat.fub))
            self.sp_fyb.setValue(self.units.stress(self.ci.bolt_mat.fyb))
            self._loading = False

    def _threads_toggled(self, v):
        self.ci.threads_in_shear_plane = v
        self.update_views()

    def _hauto_toggled(self, v):
        self.ci.load.h_load = None if v else 0.0
        self.sp_h.setEnabled(not v)
        if not v:
            self.ci.load.h_load = self.units.len(self.sp_h.value(), to_si=True)
        self.update_views()

    def toggle_units(self):
        self.units = Units("imperial" if self.units.metric else "metric")
        had_results = self.ana is not None
        self.set_model(self.ci)
        if had_results:
            self.run_analytical()

    # ------------------------------------------------------------- actions
    def update_views(self):
        if self.ci is None or self.ci.n_bolts == 0:
            return
        ic = self.ana.icr.ic if self.ana else None
        bf = self.ana.service_forces if self.ana else None
        if bf is not None and len(bf) != self.ci.n_bolts:
            bf, ic = None, None
        self.view3d.draw_connection(self.ci, ic=ic, bolt_forces=bf)
        self.view2d.draw_group(self.ci, ic=ic, bolt_forces=bf,
                               scale_unit=f"[{self.units.u_force}]")

    def run_analytical(self):
        errs = self.ci.validate()
        if errs:
            self.log_view.appendPlainText("INPUT ERRORS:\n" + "\n".join(errs))
            if not getattr(self, "suppress_dialogs", False):
                QMessageBox.warning(self, "Input errors", "\n".join(errs))
            return
        self.ana = analyze(self.ci)
        a = self.ana
        u = self.units
        self.lbl["C"].setText(f"{a.icr.C:.3f}")
        self.lbl["Ce"].setText(f"{a.icr.elastic_C:.3f}" if a.icr.elastic_C else "-")
        self.lbl["Pu"].setText(f"{u.force(a.Pu):.1f} {u.u_force}")
        self.lbl["util"].setText(f"{a.utilization:.3f}"
                                 + ("  (OVERLOAD)" if a.utilization > 1 else ""))
        self.lbl["ic"].setText(f"({u.len(a.icr.ic[0]):.2f}, "
                               f"{u.len(a.icr.ic[1]):.2f}) {u.u_len}")
        if u.metric:
            self.lbl["M"].setText(f"{a.moment/1e3:.1f} kN.m")
        else:
            self.lbl["M"].setText(f"{a.moment/KIP/INCH:.1f} kip.in")
        self.lbl["rult"].setText(f"{u.force(a.rult):.1f} {u.u_force}")
        t = math.radians(self.ci.load.theta)
        self.lbl["Pcomp"].setText(
            f"{u.force(a.ci.load.P*math.cos(t)):.1f} / "
            f"{u.force(a.ci.load.P*math.sin(t)):.1f} {u.u_force} "
            f"(vertical / horizontal)")
        self.lbl["idea"].setText(self.lbl["idea"].text())
        self.res_table.setRowCount(self.ci.n_bolts)
        for i, ((bx, by), rr, sf) in enumerate(zip(
                self.ci.bolts_xy, a.icr.bolt_magnitudes, a.service_magnitudes)):
            vals = [f"B{i+1}", f"{u.len(bx):.2f}", f"{u.len(by):.2f}",
                    f"{rr:.3f}", f"{u.force(sf):.1f}"]
            for j in range(5, 10):
                it = self.res_table.item(i, j)
                vals.append(it.text() if it else "")
            for j, v in enumerate(vals):
                self.res_table.setItem(i, j, QTableWidgetItem(str(v)))
        self.chk_view.setPlainText("\n".join(a.notes) or
                              "ICR solution converged "
                              f"({a.icr.iterations} iterations, residual "
                              f"{a.icr.unbalanced:.1e}).")
        self.update_views()
        self.statusBar().showMessage(
            f"C&K ICR: C = {a.icr.C:.3f}, Pu = {u.force(a.Pu):.1f} {u.u_force}, "
            f"utilization {a.utilization:.2f}")

    def run_idea(self):
        if self.ci is None or self.ci.validate():
            if not getattr(self, "suppress_dialogs", False):
                QMessageBox.warning(self, "Input errors",
                                    "\n".join(self.ci.validate() or ["no input"]))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("IDEA StatiCa Connection API")
        fl = QFormLayout(dlg)
        from PySide6.QtWidgets import QLineEdit
        url = QLineEdit(self.settings.value("base_url", "http://localhost:5000"))
        setup = QLineEdit(self.settings.value("setup_dir", ""))
        setup.setPlaceholderText(r"C:\Program Files\IDEA StatiCa\StatiCa 26.0")
        mock = QCheckBox("Use built-in mock service (no IDEA installation needed)")
        mock.setChecked(self.settings.value("mock", "0") == "1")
        auto = QCheckBox("Auto-start the REST service when it is not running "
                         "(searches C:\\Program Files\\IDEA StatiCa\\StatiCa *)")
        auto.setChecked(self.settings.value("auto_start", "1") == "1")
        testb = QPushButton("Test connection now")
        testb.clicked.connect(lambda: self._test_connection(url.text()))
        out = QLineEdit(self.settings.value("out_dir", os.path.join(os.getcwd(), "out")))
        mesh = QDoubleSpinBox()
        mesh.setRange(0.0, 100.0)
        mesh.setDecimals(1)
        mesh.setSuffix(" mm")
        mesh.setValue(float(self.settings.value("mesh_max_mm", 0.0) or 0.0))
        mesh.setToolTip("Maximum shell element size (0 = IDEA default)")
        plates_el = QSpinBox()
        plates_el.setRange(0, 200)
        plates_el.setValue(int(self.settings.value("elements_per_plate", 0) or 0))
        plates_el.setToolTip("Elements across an individual plate (0 = default)")
        chk_detailing = QCheckBox("run the detailing checks")
        chk_detailing.setChecked(self.settings.value("detailing", "1") == "1")
        cmb_model = QComboBox()
        cmb_model.addItems(["default", "biLinear", "multiLinear"])
        cmb_model.setCurrentText(self.settings.value("material_model", "default"))
        fl.addRow("Service URL", url)
        fl.addRow("IDEA setup dir (runner mode)", setup)
        fl.addRow("Output directory", out)
        fl.addRow("Mesh size", mesh)
        fl.addRow("Elements per plate", plates_el)
        fl.addRow("Material model", cmb_model)
        fl.addRow(chk_detailing)
        fl.addRow(mock)
        fl.addRow(auto)
        fl.addRow(testb)
        hb = QHBoxLayout()
        okb = QPushButton("Run analysis")
        okb.clicked.connect(dlg.accept)
        hb.addWidget(okb)
        fl.addRow(hb)
        if dlg.exec() != QDialog.Accepted:
            return
        self.settings.setValue("base_url", url.text())
        self.settings.setValue("setup_dir", setup.text())
        self.settings.setValue("mock", "1" if mock.isChecked() else "0")
        self.settings.setValue("auto_start", "1" if auto.isChecked() else "0")
        self.settings.setValue("out_dir", out.text())
        self.settings.setValue("mesh_max_mm", str(mesh.value()))
        self.settings.setValue("elements_per_plate", str(plates_el.value()))
        self.settings.setValue("detailing", "1" if chk_detailing.isChecked() else "0")
        self.settings.setValue("material_model", cmb_model.currentText())
        self._start_idea_worker(dict(
            base_url=url.text(), setup_dir=setup.text(), mock=mock.isChecked(),
            auto_start=auto.isChecked(), out_dir=out.text(),
            mesh_max=mesh.value() / 1000.0 if mesh.value() else 0.0,
            elements_per_plate=plates_el.value(),
            detailing=chk_detailing.isChecked(),
            material_model=(cmb_model.currentText()
                            if cmb_model.currentText() != "default" else "")))

    def _start_idea_worker(self, settings):
        self._idea_worker = IdeaWorker(self.ci, settings)
        self._idea_worker.log.connect(self._log)
        self._idea_worker.finished_ok.connect(self._idea_done)
        self._idea_worker.failed.connect(self._idea_failed)
        self._idea_worker.service_missing.connect(self._idea_service_missing)
        self._idea_worker.start()

    def _test_connection(self, base_url):
        from .idea_service import service_is_up, find_idea_setup_dir
        if service_is_up(base_url, timeout=2.0):
            QMessageBox.information(self, "Connection test",
                                    f"A service answers at {base_url} "
                                    f"(heartbeat OK).")
        else:
            setup = find_idea_setup_dir()
            extra = (f"\nIDEA installation found at: {setup}" if setup else
                     "\nNo IDEA StatiCa installation was found on this PC.")
            QMessageBox.warning(self, "Connection test",
                                f"No service is listening at {base_url}.\n"
                                f"Start IdeaStatiCa.ConnectionRestApi.exe or "
                                f"enable auto-start.{extra}")

    def _idea_service_missing(self, msg):
        self._log("IDEA service not running:\n" + msg)
        if getattr(self, "suppress_dialogs", False):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("IDEA StatiCa service not running")
        box.setText("The Connection REST API service is not reachable "
                    "(connection refused).")
        box.setInformativeText(msg)
        b_retry = box.addButton("Auto-start service && retry",
                                QMessageBox.AcceptRole)
        b_mock = box.addButton("Run with mock service instead",
                                QMessageBox.ActionRole)
        b_set = box.addButton("Open settings", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == b_retry:
            self.settings.setValue("auto_start", "1")
            self._start_idea_worker(dict(
                base_url=self.settings.value("base_url", "http://localhost:5000"),
                setup_dir=self.settings.value("setup_dir", ""),
                mock=False, auto_start=True,
                out_dir=self.settings.value("out_dir", os.path.join(os.getcwd(), "out"))))
        elif clicked == b_mock:
            self._start_idea_worker(dict(
                base_url=self.settings.value("base_url", "http://localhost:5000"),
                setup_dir="", mock=True, auto_start=False,
                out_dir=self.settings.value("out_dir", os.path.join(os.getcwd(), "out"))))
        elif clicked == b_set:
            self.run_idea()

    def _log(self, msg):
        self.log_view.appendPlainText(msg)

    def _idea_done(self, res):
        u = self.units
        self.lbl["idea"].setText(
            f"passed={res.passed}; bolts={len(res.bolts)}; "
            f"checks={len(res.checks)}")
        if res.capacity is not None:
            txt = f"{u.force(res.capacity):.1f} {u.u_force}"
            if res.applied_load:
                txt += (f"  (P = {u.force(res.applied_load):.1f} {u.u_force}"
                        f", utilization "
                        f"{res.applied_load/res.capacity:.2f})")
            self.lbl["capacity"].setText(txt)
        else:
            self.lbl["capacity"].setText(
                f"not available ({res.capacity_source})")
        lines = []
        for i, b in enumerate(res.bolts):
            if i < self.res_table.rowCount():
                for col, val in ((5, b.shear_v), (6, b.normal_n)):
                    self.res_table.setItem(i, col, QTableWidgetItem(
                        f"{u.force(val):.1f}" if val is not None else "-"))
                self.res_table.setItem(i, 7, QTableWidgetItem(
                    f"{b.unity_check:.3f}" if b.unity_check is not None else "-"))
        if res.capacity is not None:
            lines.append(f"Connection capacity: "
                         f"{u.force(res.capacity):.1f} {u.u_force} "
                         f"({res.capacity_source})")
        if res.capacity_ck is not None:
            lines.append(f"C*Rult from the service coefficient: "
                         f"{u.force(res.capacity_ck):.1f} {u.u_force}")
        if self.ana is not None:
            lines.append(f"Crawford & Kulak Pu = C*Rult "
                         f"(this program): {self.units.force(self.ana.Pu):.1f} "
                         f"{self.units.u_force}")
        for c in res.checks:
            lines.append(c)
        for p in res.plates:
            lines.append(f"plate {p.name}: max stress "
                         f"{u.stress(p.max_stress or 0):.1f} {u.u_stress} "
                         f"(ok={p.ok})")
        if res.ideacon_path:
            lines.append(f"IDEA project: {res.ideacon_path}")
        if res.report_path:
            lines.append(f"report: {res.report_path}")
        self.chk_view.setPlainText("\n".join(lines))
        self.statusBar().showMessage("IDEA StatiCa analysis finished.")

    def _idea_failed(self, tb):
        self._log("IDEA analysis FAILED:\n" + tb)
        QMessageBox.critical(self, "IDEA StatiCa analysis failed",
                             "See the Log tab for details.\n\n"
                             "Tip: enable the mock service in the dialog to "
                             "exercise the pipeline without an IDEA install.")

    # ------------------------------------------------------- FE fallback
    def run_fea(self):
        if self.ci is None:
            return
        errs = self.ci.validate()
        if errs:
            self.log_view.appendPlainText("INPUT ERRORS:\n" + "\n".join(errs))
            if not getattr(self, "suppress_dialogs", False):
                QMessageBox.warning(self, "Input errors", "\n".join(errs))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("OpenSees FE fallback")
        fl = QFormLayout(dlg)
        mesh = QDoubleSpinBox()
        mesh.setRange(0.0, 100.0)
        mesh.setDecimals(1)
        mesh.setSuffix(" mm")
        mesh.setValue(float(self.settings.value("fe_mesh_mm", 8.0)))
        chk_nl = QCheckBox("nonlinear J2 plates")
        chk_nl.setChecked(self.settings.value("fe_nonlinear", "1") == "1")
        chk_gap = QCheckBox("model hole clearance (bearing gap)")
        chk_gap.setChecked(self.settings.value("fe_gap", "1") == "1")
        info = QLabel("Models the actual bolt holes, one-sided bearing with "
                      "clearance and the vector C&K bolt law.")
        info.setWordWrap(True)
        fl.addRow(info)
        fl.addRow("Mesh size", mesh)
        fl.addRow(chk_nl)
        fl.addRow(chk_gap)
        hb = QHBoxLayout()
        okb = QPushButton("Run")
        okb.clicked.connect(dlg.accept)
        hb.addWidget(okb)
        fl.addRow(hb)
        if dlg.exec() != QDialog.Accepted:
            return
        self.settings.setValue("fe_mesh_mm", str(mesh.value()))
        self.settings.setValue("fe_nonlinear", "1" if chk_nl.isChecked() else "0")
        self.settings.setValue("fe_gap", "1" if chk_gap.isChecked() else "0")
        from .fe_fallback import model_from_connection
        model = model_from_connection(
            self.ci, mesh=mesh.value() / 1000.0 if mesh.value() else None,
            plate_nonlinear=chk_nl.isChecked(),
            contact_gap=chk_gap.isChecked())
        self._fea_worker = FeaWorker(model)
        self._fea_worker.log.connect(self._log)
        self._fea_worker.finished_ok.connect(self._fea_done)
        self._fea_worker.failed.connect(self._fea_failed)
        self.statusBar().showMessage("Running OpenSees FE fallback...")
        self._fea_worker.start()

    def _fea_done(self, res):
        u = self.units
        if not res.get("ok") or not res.get("capacity"):
            self._fea_failed(res.get("message", "no capacity"))
            return
        cap = res["capacity"]
        self.lbl["fea"].setText(
            f"{res.get('plate_model', '-')}; "
            f"failure: {res.get('failure_mode', '-')}")
        self.lbl["fea_capacity"].setText(
            f"{u.force(cap):.1f} {u.u_force}  "
            f"(P = {u.force(self.ci.load.P):.1f} {u.u_force}, "
            f"util. {self.ci.load.P/cap:.2f})")
        for i, b in enumerate(res.get("bolts", [])):
            if i < self.res_table.rowCount():
                self.res_table.setItem(i, 8, QTableWidgetItem(
                    f"{u.force(b['magnitude']):.1f}"))
                self.res_table.setItem(i, 9, QTableWidgetItem(
                    f"{b['unity']:.3f}"))
        lines = [f"FE capacity: {u.force(cap):.1f} {u.u_force}",
                 f"plate model: {res.get('plate_model', '-')}",
                 f"failure mode: {res.get('failure_mode', '-')}",
                 f"max bolt deformation: "
                 f"{res.get('max_bolt_deformation', 0)*1000:.3f} mm",
                 f"hole clearance: "
                 f"{res.get('hole_clearance', 0)*1000:.2f} mm"]
        if res.get("notes"):
            lines.append(str(res["notes"]))
        self.chk_view.setPlainText("\n".join(lines))
        self._log("\n".join(lines))
        self.statusBar().showMessage("FE fallback finished.")

    def _fea_failed(self, msg):
        self._log("FE fallback FAILED: " + str(msg))
        if not getattr(self, "suppress_dialogs", False):
            QMessageBox.warning(self, "FE fallback",
                                "The FE analysis did not produce a result.\n\n"
                                + str(msg)[-1500:])

    # -------------------------------------------------------- calibration
    def calibrate_ck(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Calibrate Crawford & Kulak parameters")
        vl = QVBoxLayout(dlg)
        info = QLabel(
            "Fits mu and lambda to the observed failure loads of the physical "
            "specimens (Badawi 1983, ten full-size tests).  The original "
            "values 10/in and 0.55 came from single-bolt tests, not from a "
            "bolt group.")
        info.setWordWrap(True)
        vl.addWidget(info)
        out = QPlainTextEdit(readOnly=True)
        out.setMinimumHeight(280)
        vl.addWidget(out)
        hb = QHBoxLayout()
        run = QPushButton("Run calibration")
        apply = QPushButton("Apply && close")
        apply.setEnabled(False)
        close = QPushButton("Close")
        hb.addWidget(run); hb.addWidget(apply); hb.addWidget(close)
        vl.addLayout(hb)
        state = {"res": None}

        def _run():
            run.setEnabled(False)
            out.setPlainText("Calibrating ...")
            self._cal_worker = CalibrationWorker()
            self._cal_worker.finished_ok.connect(lambda r: _done(r))
            self._cal_worker.failed.connect(
                lambda t: out.setPlainText("Calibration failed:\n" + t))
            self._cal_worker.start()

        def _done(res):
            state["res"] = res
            out.setPlainText(res.format())
            apply.setEnabled(True)
            run.setEnabled(True)

        def _apply():
            if state["res"] is not None:
                state["res"].apply(self.ci)
                self._loading = True
                self.sp_mu.setValue(self.units.mu(self.ci.bolt_mat.mu))
                self.sp_lambda.setValue(self.ci.bolt_mat.ck_lambda)
                self._loading = False
                self._log(state["res"].format())
                self.update_views()
                dlg.accept()

        run.clicked.connect(_run)
        apply.clicked.connect(_apply)
        close.clicked.connect(dlg.reject)
        dlg.resize(760, 620)
        dlg.exec()

    def run_validation(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Verification suite - Crawford & Kulak (1971)")
        vl = QVBoxLayout(dlg)
        info = QLabel(
            "Checks the implemented C&K instantaneous-centre method against:\n"
            "  * Brandt (1982) worked examples of the C&K method (Cu = 1.40 / 1.10)\n"
            "  * the C-coefficient table generated from Crawford & Kulak (1971)\n"
            "    (AISC 8th ed. Tables X-XVIII / Kulak et al. Guide Table 13.1)\n"
            "  * scale invariance and the concentric limit C = n\n"
            "  * the IOM export and the Connection REST API pipeline (mock service)")
        info.setWordWrap(True)
        vl.addWidget(info)
        full = QCheckBox("check the complete C-table (308 entries)")
        vl.addWidget(full)
        self.val_table = QTableWidget(0, 4)
        self.val_table.setHorizontalHeaderLabels(["check", "expected", "got", "result"])
        self.val_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        vl.addWidget(self.val_table)
        bb = QHBoxLayout()
        run = QPushButton("Run")
        bb.addWidget(run)
        vl.addLayout(bb)
        dlg.resize(760, 520)

        def _run():
            self._val_worker = ValidationWorker(full.isChecked())
            self._val_worker.finished_ok.connect(
                lambda checks: _fill(checks))
            self._val_worker.start()

        def _fill(checks):
            self.val_table.setRowCount(len(checks))
            for r, (name, exp, got, ok) in enumerate(checks):
                for c, v in enumerate((name, exp, got, "PASS" if ok else "FAIL")):
                    it = QTableWidgetItem(str(v))
                    if c == 3:
                        it.setForeground(Qt.darkGreen if ok else Qt.red)
                    self.val_table.setItem(r, c, it)
            self._log("verification: " + ", ".join(
                f"{n}={'PASS' if ok else 'FAIL'}" for n, _e, _g, ok in checks))
        run.clicked.connect(_run)
        dlg.exec()

    # ------------------------------------------------------------- files
    def _to_json(self):
        def conv(o):
            if is_dataclass(o):
                return asdict(o)
            raise TypeError(str(type(o)))
        return json.dumps(self.ci, default=conv, indent=1)

    def save_json(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save input", "connection.json",
                                           "JSON (*.json)")
        if p:
            open(p, "w").write(self._to_json())

    def open_json(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open input", "", "JSON (*.json)")
        if not p:
            return
        d = json.load(open(p))

        def _plate(data):
            data = dict(data)
            mat = data.get("material")
            if isinstance(mat, dict):
                data["material"] = SteelMaterial(**mat)
            return Plate(**data)

        from .model import FemOptions
        ci = ConnectionInput(
            title=d.get("title", ""), design_code=d.get("design_code", "AISC"),
            bolts_xy=[tuple(b) for b in d.get("bolts_xy", [])],
            bolt_diameter=d.get("bolt_diameter", 0.02),
            bolt_hole_clearance=d.get("bolt_hole_clearance", 0.002),
            n_shear_planes=d.get("n_shear_planes", 1),
            threads_in_shear_plane=d.get("threads_in_shear_plane", True),
            plate_a=_plate(d["plate_a"]), plate_b=_plate(d["plate_b"]),
            bolt_mat=BoltMaterial(**d["bolt_mat"]), load=LoadSpec(**d["load"]),
            fem=FemOptions(**d.get("fem", {})))
        self.set_model(ci)

    def export_iom(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export IOM", "connection_iom.xml",
                                           "IOM xml (*.xml)")
        if p:
            from .iom import IomBuilder
            IomBuilder(self.ci).write(p)
            self._log(f"IOM exported to {p}")

    def edit_settings(self):
        self.run_idea()
