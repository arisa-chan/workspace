"""
Workflow that drives the IDEA StatiCa Connection REST API (official Python
SDK ``ideastatica_connection_api``) for an eccentrically loaded bolted shear
connection:

    IOM XML  ->  import-iom-file  ->  load effects  ->  CBFEM calculation
             ->  check results (bolts / plates)  ->  report / .ideaCon

The service (``IdeaStatiCa.ConnectionRestApi.exe``) ships with the IDEA StatiCa
desktop installation and listens on http://localhost:5000 by default.  It can
either be started manually (attach mode) or launched by the SDK service runner
from the installation directory (runner mode, Windows only).
"""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .iom import IomBuilder, member_load_components
from .model import ConnectionInput

log = logging.getLogger("eccbolt.idea")


@dataclass
class BoltResult:
    name: str
    unity_check: Optional[float]
    ok: Optional[bool]
    shear_v: Optional[float] = None     # N, resultant shear at the applied load
    normal_n: Optional[float] = None    # N, axial force at the applied load
    capacity: Optional[float] = None    # N, this bolt's resistance (Rult)
    shear_components: Optional[Tuple[float, float]] = None   # N, in-plane (x, z)


@dataclass
class PlateResult:
    name: str
    max_stress: Optional[float]         # Pa
    max_strain: Optional[float]
    ok: Optional[bool]


@dataclass
class IdeaSettings:
    """Calculation parameters of the IDEA StatiCa model that may be tuned.

    The REST service exposes them through ``GET/PUT …/settings``; the names
    below are the keys of that dictionary.  ``None`` leaves the value the
    service uses by default (which is the value of the user's IDEA profile).
    """
    mesh_max_element: Optional[float] = None        # m
    mesh_min_element: Optional[float] = None        # m
    elements_per_plate: Optional[int] = None
    material_model: Optional[str] = None             # biLinear / multiLinear
    detailing: Optional[bool] = None
    local_deformation_check: Optional[bool] = None
    distance_between_bolts: Optional[float] = None   # multiples of d

    def to_service_dict(self) -> Dict[str, object]:
        pairs = {
            "calculationCommon/Model/Mesh/Shared/MaximalSizeOfElement":
                self.mesh_max_element,
            "calculationCommon/Model/Mesh/Shared/MinimalSizeOfElement":
                self.mesh_min_element,
            "calculationCommon/Model/Mesh/Shared/NumberOfElementsOnIndividualPlates":
                self.elements_per_plate,
            "calculationCommon/Analysis/AnalysisGeneral/Shared/AnalysisMaterialModel":
                self.material_model,
            "calculationCommon/Analysis/AnalysisGeneral/Shared/Detailing":
                self.detailing,
            "calculationCommon/Analysis/AnalysisGeneral/Shared/LocalDeformationCheck":
                self.local_deformation_check,
            "calculation/Checks/US/DistanceBetweenBolts":
                self.distance_between_bolts,
        }
        return {k: v for k, v in pairs.items() if v is not None}


def apply_idea_settings(client, project_id: str, settings: Optional[IdeaSettings]
                        ) -> Dict[str, object]:
    """Merge ``settings`` into the model's calculation settings."""
    if settings is None:
        return {}
    wanted = settings.to_service_dict()
    if not wanted:
        return {}
    current = client.settings.get_settings(project_id)
    data = (current.to_dict() if hasattr(current, "to_dict") else dict(current))
    data.update(wanted)
    client.settings.update_settings(project_id, data)
    return wanted


@dataclass
class IdeaAnalysisResult:
    ok: bool
    summary: List[str] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    bolts: List[BoltResult] = field(default_factory=list)
    plates: List[PlateResult] = field(default_factory=list)
    passed: Optional[bool] = None
    ideacon_path: Optional[str] = None
    report_path: Optional[str] = None
    raw: Optional[dict] = None
    applied_load: Optional[float] = None      # N, the load the model was solved for
    capacity: Optional[float] = None          # N, load level at which unity check = 1
    capacity_source: str = ""
    capacity_service: Optional[float] = None  # N, P / max(service unity check)
    capacity_ck: Optional[float] = None       # N, C*Rult when the service reports C


class IdeaServiceError(RuntimeError):
    pass


class IdeaServiceNotRunning(IdeaServiceError):
    """Raised when nothing is listening on the Connection REST API port."""


class IdeaImportError(IdeaServiceError):
    """Raised when the service refuses every IOM variant we offer."""


def _exc_brief(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    status = getattr(exc, "status", None)
    if body:
        try:
            import json as _json
            d = _json.loads(body if isinstance(body, str) else body.decode())
            return f"HTTP {status}: {d.get('title')} - {d.get('detail')}"
        except Exception:
            return f"HTTP {status}: {body}"
    return f"{type(exc).__name__}: {exc}"


COMMON_PORTS = (5000, 5193)
_STARTED_PROCESSES: List[subprocess.Popen] = []


def _stop_started_services():
    for proc in _STARTED_PROCESSES:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _STARTED_PROCESSES.clear()


atexit.register(_stop_started_services)


def find_idea_setup_dir() -> Optional[str]:
    """Locate the IDEA StatiCa installation folder on Windows (highest
    version first); returns None elsewhere / when not installed."""
    if os.name != "nt":
        return None
    import glob
    roots = [r"C:\Program Files", r"D:\Program Files",
             r"C:\Program Files (x86)"]
    for root in roots:
        for cand in sorted(glob.glob(os.path.join(root, "IDEA StatiCa", "StatiCa *")),
                           reverse=True):
            if os.path.isfile(os.path.join(cand, "IdeaStatiCa.ConnectionRestApi.exe")):
                return cand
    return None


def parse_base_url(base_url: str):
    from urllib.parse import urlparse
    u = urlparse(base_url)
    port = u.port or 5000
    host = u.hostname or "localhost"
    return host, port


def service_is_up(base_url: str, timeout: float = 1.5) -> bool:
    """Fast probe: TCP connect + GET /heartbeat (the endpoint the official SDK
    service runner waits for)."""
    host, port = parse_base_url(base_url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return False
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/heartbeat")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:      # any HTTP answer = a service listens
        return exc.code < 500
    except Exception:
        return False


def wait_for_service(base_url: str, timeout_s: float = 120.0,
                     poll: float = 1.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if service_is_up(base_url, timeout=1.0):
            return True
        time.sleep(poll)
    return False


class ServiceManager:
    """Finds / starts / stops ``IdeaStatiCa.ConnectionRestApi.exe``."""

    def __init__(self, base_url: str, setup_dir: Optional[str] = None,
                 logger: Optional[Callable[[str], None]] = None,
                 start_cmd: Optional[List[str]] = None):
        self.base_url = base_url
        self.setup_dir = setup_dir
        self.emit = logger or (lambda s: None)
        self.start_cmd = start_cmd          # test hook (e.g. mock service)
        self.proc: Optional[subprocess.Popen] = None

    def ensure_running(self, wait_s: float = 120.0) -> bool:
        if service_is_up(self.base_url):
            return True
        if self.start_cmd is None:
            setup = self.setup_dir or find_idea_setup_dir()
            if not setup:
                return False
            _, port = parse_base_url(self.base_url)
            exe = os.path.join(setup, "IdeaStatiCa.ConnectionRestApi.exe")
            if not os.path.isfile(exe):
                return False
            self.start_cmd = [exe, f"-port={port}"]
            self.setup_dir = setup
        self.emit(f"Starting Connection REST API service: "
                  f"{' '.join(self.start_cmd)}")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.proc = subprocess.Popen(
            self.start_cmd, cwd=self.setup_dir or None, creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _STARTED_PROCESSES.append(self.proc)
        ok = wait_for_service(self.base_url, timeout_s=wait_s)
        if ok:
            self.emit("Service is up (heartbeat answered).")
        else:
            self.emit("Service did not answer the heartbeat in time.")
        return ok

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            if self.proc in _STARTED_PROCESSES:
                _STARTED_PROCESSES.remove(self.proc)
            self.proc = None


class IdeaWorkflow:
    """High level driver of the Connection REST API."""

    def __init__(self, base_url: str = "http://localhost:5000",
                 setup_dir: Optional[str] = None,
                 logger: Optional[Callable[[str], None]] = None,
                 auto_start: bool = True):
        self.base_url = base_url
        self.setup_dir = setup_dir
        self.auto_start = auto_start
        self.emit = logger or (lambda s: log.info(s))
        self._client = None
        self._runner = None
        self.service_manager: Optional[ServiceManager] = None

    # ------------------------------------------------------------------ conn
    @staticmethod
    def _not_running_message(base_url: str) -> str:
        host, port = parse_base_url(base_url)
        return (
            f"No IDEA StatiCa Connection REST API service is listening at "
            f"{base_url} (connection refused on {host}:{port}).\n\n"
            "To analyse with IDEA StatiCa:\n"
            "  1. IDEA StatiCa >= 24.1 must be installed with a license that "
            "enables the API (the Basic license does not; Trial/Educational "
            "do).\n"
            "  2. Start the service once per session, e.g.\n"
            '       cd "C:\\Program Files\\IDEA StatiCa\\StatiCa 26.0"\n'
            "       IdeaStatiCa.ConnectionRestApi.exe          (default port "
            f"{port})\n"
            "     or tick Settings > 'Auto-start service when missing' and "
            "press Retry - the app then launches and waits for the service "
            "itself.\n"
            "  3. If your service runs on another port, put that port in the "
            "Service URL (e.g. http://localhost:5193).\n\n"
            "Without an IDEA installation you can tick 'Use built-in mock "
            "service' to exercise the complete pipeline offline.")

    def connect(self):
        import ideastatica_connection_api as isa
        if not self.setup_dir and not service_is_up(self.base_url):
            mgr = ServiceManager(self.base_url, self.setup_dir, self.emit)
            started = self.auto_start and mgr.ensure_running()
            if not started and not service_is_up(self.base_url):
                raise IdeaServiceNotRunning(
                    self._not_running_message(self.base_url))
            self.service_manager = mgr if started else None
        if self.setup_dir:
            from ideastatica_connection_api.connection_api_service_runner import (
                ConnectionApiServiceRunner)
            import asyncio
            self._loop = asyncio.new_event_loop()
            self._runner = self._loop.run_until_complete(
                ConnectionApiServiceRunner(self.setup_dir, "EccentricBolt", "1.0").__aenter__())
            self._client = self._runner.create_api_client()
            self._client.__enter__()
        else:
            from ideastatica_connection_api.connection_api_service_attacher import (
                ConnectionApiServiceAttacher)
            self._client = ConnectionApiServiceAttacher(
                self.base_url, "EccentricBolt", "1.0").create_api_client()
            self._client.__enter__()
        self.emit(f"Connected to IDEA StatiCa Connection API at {self.base_url} "
                  f"(client {self._client.client_id})")
        return self._client

    def close(self):
        try:
            if self._client:
                proj = getattr(self._client, "project", None)
                if proj is not None and not proj.active_project_id:
                    # nothing was opened: the SDK __exit__ would try to close
                    # a None project id -> skip that call
                    self._client.project = None
                self._client.__exit__(None, None, None)
        finally:
            self._client = None
            if self._runner is not None:
                import asyncio
                self._loop.run_until_complete(self._runner.__aexit__(None, None, None))
                self._runner = None

    # ------------------------------------------------------------------ main
    def analyse(self, ci: ConnectionInput, out_dir: str = ".",
                save_project: bool = True, save_report: bool = True,
                settings: Optional[IdeaSettings] = None) -> IdeaAnalysisResult:
        res = IdeaAnalysisResult(ok=False)
        client = self._client or self.connect()
        api = client

        # 1. IOM -----------------------------------------------------------
        builder = IomBuilder(ci)
        variants = builder.variants()
        with open(os.path.join(out_dir, "connection_iom.xml"), "wb") as fh:
            fh.write(variants[0][1].encode("utf-8"))
        self.emit(f"IOM written: {os.path.join(out_dir, 'connection_iom.xml')}")

        # 2. import - try the compatibility variants until one is accepted --
        proj = None
        last_brief = ""
        for label, xml_text in variants:
            enc = "utf-16" if label.startswith("utf16") else "utf-8"
            iom_bytes = xml_text.encode(enc)
            vpath = os.path.join(out_dir, f"connection_iom_{label}.xml")
            with open(vpath, "wb") as fh:
                fh.write(iom_bytes)
            self.emit(f"Importing IOM variant '{label}' ...")
            try:
                proj = api.project.import_iom(iom_bytes)
                self.emit(f"IOM variant '{label}' accepted by the service.")
                break
            except Exception as exc:
                last_brief = _exc_brief(exc)
                self.emit(f"Variant '{label}' rejected: {last_brief}")
        if proj is None:
            raise IdeaImportError(
                "The IDEA StatiCa service rejected every IOM compatibility "
                f"variant ({', '.join(v[0] for v in variants)}).\n"
                f"Last service response: {last_brief}\n"
                f"The attempted documents were saved to {out_dir} "
                "(connection_iom_*.xml); open one in the IDEA StatiCa desktop "
                "application or send it to IDEA support to see the exact "
                "parser message.")
        pid = proj.project_id
        api.project.active_project_id = pid
        self.emit(f"Project imported, id {pid}")

        # 2b. calculation parameters (mesh, material model, detailing) ------
        try:
            changed = apply_idea_settings(api, pid, settings)
        except Exception as exc:                     # pragma: no cover
            changed = {}
            self.emit(f"(IDEA settings were not applied: {_exc_brief(exc)})")
        if changed:
            for key, value in changed.items():
                self.emit(f"   setting {key.split('/')[-1]} = {value}")

        cons = api.connection.get_connections(pid)
        if not cons:
            raise IdeaServiceError("Import produced no connection.")
        con = cons[0]
        self.emit(f"Connection '{con.name}' (id {con.id}) with "
                  f"{len(cons)} connection(s) in project")

        # 3. members & load effect ------------------------------------------
        members = api.member.get_members(pid, con.id)
        self.emit("Members: " + ", ".join(f"{m.id}:{m.name}" for m in members))
        a_load, c_load = member_load_components(ci)

        def _ml(member, comp):
            from ideastatica_connection_api import (ConLoadEffectMemberLoad,
                                                     ConLoadEffectSectionLoad,
                                                     ConLoadEffectPositionEnum)
            return ConLoadEffectMemberLoad(
                member_id=member.id,
                position=ConLoadEffectPositionEnum.END,
                section_load=ConLoadEffectSectionLoad(**comp))

        from ideastatica_connection_api import ConLoadEffect, ConLoadSettings
        # member order follows IOM: 1 = support plate (bearing), 2 = load stub
        by_id = {m.id: m for m in members}
        sup = next((m for m in members if m.name and "Support" in m.name), members[0])
        stub = next((m for m in members if m.id != sup.id), members[-1])
        le = ConLoadEffect(name="LC1 - eccentric load", active=True,
                           member_loadings=[_ml(sup, a_load), _ml(stub, c_load)])
        le = api.load_effect.add_load_effect(pid, con.id, le)
        self.emit(
            f"Load effect '{le.name}' added (id {le.id}). Angled load resolved "
            f"into member force components (the REST API takes N/Vy/Vz/Mx/My/Mz, "
            f"there is no 'angle' parameter):")
        self.emit(f"   load stub : N={c_load['n']/1e3:.1f} kN, "
                  f"Vz={c_load['vz']/1e3:.1f} kN, My={c_load['my']/1e3:.1f} kNm")
        self.emit(f"   support   : N={a_load['n']/1e3:.1f} kN, "
                  f"Vz={a_load['vz']/1e3:.1f} kN, My={a_load['my']/1e3:.1f} kNm")
        api.load_effect.set_load_settings(
            pid, con.id, ConLoadSettings(loads_in_equilibrium=True,
                                         loads_in_percentage=False))

        # 4. calculation -----------------------------------------------------
        summary = api.calculation.calculate(pid, [con.id])
        for s in summary:
            res.passed = s.passed
            res.summary.append(f"connection {s.id}: passed={s.passed}")
            for cs in (s.result_summary or []):
                # the service reports *why* a check failed in the message -
                # keep it, it is the only diagnostic the user gets
                msg = (cs.unity_check_message or "").strip()
                res.checks.append(f"{cs.name}: util={cs.check_value} "
                                  f"ok={cs.check_status}"
                                  + (f" - {msg}" if msg else ""))
                if not cs.check_status and msg:
                    self.emit(f"   check '{cs.name}': {msg}")
        self.emit("Calculation finished: " + "; ".join(res.summary))

        # 5. detailed results -------------------------------------------------
        checks = api.calculation.get_results(pid, [con.id])
        for chk in checks:
            for b in (chk.check_res_bolt or []):
                res.bolts.append(BoltResult(name=b.name, unity_check=b.unity_check,
                                            ok=b.check_status))
            for p in (chk.check_res_plate or []):
                res.plates.append(PlateResult(name=p.name, max_stress=p.max_stress,
                                              max_strain=p.max_strain, ok=p.check_status))
            for m in ((chk.messages.messages if chk.messages else None) or []):
                text = getattr(m, "description", None) or str(m)
                res.checks.append(f"service message: {text}")
                self.emit(f"   service message: {text}")
        try:
            raw_txt = api.calculation.get_raw_json_results(pid, [con.id])
            raw = _parse_raw_results(raw_txt)
            res.raw = raw
            if raw is None:
                msg = _raw_failure_message(raw_txt)
                if msg:
                    res.checks.append(f"service: {msg}")
                    self.emit(f"(the service reported: {msg})")
                else:
                    self.emit("(the service returned no raw results - the "
                              "CBFEM solver did not run)")
            else:
                self._extract_bolt_forces(raw, res, ci)
        except Exception as exc:                     # pragma: no cover
            self.emit(f"(raw results unavailable: {_exc_brief(exc)})")

        # 5b. per-bolt forces + connection capacity -------------------------
        for b in res.bolts:
            self.emit(f"   bolt {b.name}: V = {b.shear_v/1e3:.1f} kN, "
                      f"N = {(b.normal_n or 0.0)/1e3:.1f} kN, "
                      f"unity check {_fmt(b.unity_check)}")
        if res.capacity is not None:
            self.emit(f"Connection capacity: {res.capacity/1e3:.1f} kN "
                      f"at P = {ci.load.P/1e3:.1f} kN "
                      f"({res.capacity_source})")
            res.checks.append(
                f"capacity {res.capacity/1e3:.1f} kN at P = {ci.load.P/1e3:.1f} kN "
                f"({res.capacity_source})")
            if res.capacity_service is not None and \
                    abs(res.capacity_service - res.capacity) > 0.02 * res.capacity:
                self.emit(f"   (the service's own unity checks imply "
                          f"{res.capacity_service/1e3:.1f} kN - it derives the "
                          f"bolt resistance from the grade in the model)")
            if res.capacity_ck is not None and \
                    abs(res.capacity_ck - res.capacity) > 0.02 * res.capacity:
                self.emit(f"   (C*Rult from the service coefficient = "
                          f"{res.capacity_ck/1e3:.1f} kN)")
        else:
            self.emit("Connection capacity: not available from the service results")

        # 6. artefacts ---------------------------------------------------------
        if save_project:
            path = os.path.join(out_dir, "eccentric_connection.ideaCon")
            try:
                api.project.download_project(pid, path)
                res.ideacon_path = path
                self.emit(f"Project saved: {path}")
            except Exception as exc:                 # pragma: no cover
                self.emit(f"(project download failed: {exc})")
        if save_report:
            path = os.path.join(out_dir, "eccentric_connection_report.pdf")
            try:
                api.report.save_report_pdf(pid, con.id, path)
                res.report_path = path
                self.emit(f"Report saved: {path}")
            except Exception as exc:                 # pragma: no cover
                self.emit(f"(report export failed: {exc})")

        res.ok = True
        return res

    # ------------------------------------------------------------------ raw
    @staticmethod
    def _extract_bolt_forces(raw: dict, res: IdeaAnalysisResult,
                             ci: ConnectionInput):
        """Pull per-bolt forces and the connection capacity out of the raw
        ``CheckResultsData`` returned by ``get_raw_json_results``.

        The service documents (their API reference) that per-bolt results live
        in a dictionary keyed by *opaque* bolt identifiers - "B15", not
        necessarily 1..n and possibly with gaps - and that the force payload of
        each bolt is itself a dictionary keyed by the internal load-case id
        (``forcesAllLoadCases``), whose key is *not* always "1".  Bolt names are
        matched against the names the check results reported, so the two result
        views are aligned; the load case with the largest resultant is used.
        """
        nodes = _bolt_nodes_for(res.bolts, _bolt_result_nodes(raw))
        rult = ci.bolt_rult()
        forces = []
        for bolt in res.bolts:
            node = nodes.get(bolt.name)
            if node is None:
                continue
            shear, normal = _bolt_forces(node)
            bolt.normal_n = normal
            bolt.capacity = rult
            if shear is not None:
                bolt.shear_components = shear
                bolt.shear_v = math.hypot(*shear)
                forces.append(bolt.shear_v)
            elif normal is not None:
                forces.append(abs(normal))
        res.applied_load = ci.load.P

        utils = [b.unity_check for b in res.bolts if b.unity_check]
        if utils and max(utils) > 0:
            res.capacity_service = ci.load.P / max(utils)

        if forces:
            # scale the applied load up to the point where the most heavily
            # loaded bolt reaches the bolt resistance used for Pu = C * Rult
            f_max = max(forces)
            if f_max > 0:
                res.capacity = ci.load.P * rult / f_max
                res.capacity_source = (
                    f"bolt forces from the service: load scaled to Rult "
                    f"= {rult/1e3:.1f} kN for the most loaded bolt")
        elif res.capacity_service is not None:
            res.capacity = res.capacity_service
            res.capacity_source = "service unity check"

        # the service sometimes reports the bolt-group coefficient directly:
        # Pu = C * Rult is then exact instead of a scaling estimate
        c = _find_number(raw, ("coefficientc", "coefficient"))
        if c:
            res.capacity_ck = c * rult
        if res.capacity is None and res.capacity_ck is not None:
            res.capacity = res.capacity_ck
            res.capacity_source = "C * Rult reported by the service"
        if res.capacity is None:
            res.capacity_source = "not available"


# ---------------------------------------------------------------------------
# raw-results helpers
# ---------------------------------------------------------------------------
_BOLT_ID_RE = re.compile(r"^[A-Za-z]{0,4}\d+$")
# exact key names, then prefixes that only longer aliases may match, so that a
# single letter alias ("v", "n") never swallows unrelated keys such as "value"
_SHEAR_KEYS = ("shearforce", "resultantshear", "shear", "v", "vy", "vz",
               "vs", "qy", "qz", "shearforces", "resultantforce")
_SHEAR_PREFIXES = ("shearforce", "resultantshear", "shear")
_NORMAL_KEYS = ("normalforce", "tensionforce", "tensileforce", "axialforce",
                "tension", "n", "ft", "fn")
_NORMAL_PREFIXES = ("normalforce", "tension", "tensile", "axial")
_LOADSET_KEYS = ("forcesallloadcases", "forcesallloadcase",
                 "forcessallloadcases", "forcesbyallloadcases")


def _parse_raw_results(payload) -> Optional[dict]:
    """Turn the ``rawresults-text`` response into a dict, or ``None``.

    The endpoint returns one *string* per connection.  When the CBFEM solver
    did not run, that string is ``["Results for connection 1 failed: …"]`` -
    a JSON array of plain strings rather than a results document - so a plain
    ``json.loads(text)["..."]`` blows up with "Expecting value: line 1 column
    1".  Empty bodies occur as well.
    """
    if isinstance(payload, (list, tuple)):
        payload = payload[0] if payload else ""
    text = (payload or "").strip()
    if not text:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(doc, dict):
        return doc
    return None


def _raw_failure_message(payload) -> str:
    """The service's own failure text when the raw document is a string list."""
    if isinstance(payload, (list, tuple)):
        payload = payload[0] if payload else ""
    text = (payload or "").strip()
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(doc, list) and doc and isinstance(doc[0], str):
        return doc[0]
    return ""


def _norm_key(key) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.3f}"


def _numeric_leaves(node: dict) -> Dict[str, float]:
    return {_norm_key(k): float(v) for k, v in node.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _magnitude(payload) -> Optional[float]:
    """Resultant of a force payload: a scalar, or the norm of its components."""
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        return float(payload)
    if isinstance(payload, dict):
        comps = _numeric_leaves(payload)
        if comps:
            return math.hypot(*comps.values())
    return None


def _shear_vector(payload) -> Optional[Tuple[float, float]]:
    """In-plane (x, z) components of a shear payload.

    The bolt axis of this model is the global Y axis, so when the service
    reports three components the y (axial) one is dropped; a bare scalar is
    taken as a single positive component.
    """
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        return (float(payload), 0.0)
    if isinstance(payload, dict):
        comps = _numeric_leaves(payload)
        if not comps:
            return None
        if "x" in comps or "z" in comps:
            return (comps.get("x", 0.0), comps.get("z", 0.0))
        vals = list(comps.values())
        if len(vals) == 1:
            return (vals[0], 0.0)
        return (vals[0], vals[1])
    return None


def _divide(payload, keys: Tuple[str, ...], prefixes: Tuple[str, ...]):
    """Split a force payload into (matching entry, remaining entries)."""
    if not isinstance(payload, dict):
        return None, payload
    hit = [k for k in payload if _norm_key(k) in keys]
    if not hit:
        hit = [k for k in payload
               if any(_norm_key(k).startswith(p) for p in prefixes)]
    if not hit:
        return None, payload
    key = hit[0]
    rest = {k: v for k, v in payload.items() if k != key}
    return payload[key], (rest or None)


def _bolt_forces(node: dict):
    """((shear_x, shear_z) or None, axial force or None) of one per-bolt node.

    Handles the documented ``forcesAllLoadCases`` map (one force payload per
    load case - the load case with the largest resultant wins) as well as a
    flat node that simply carries force components.
    """
    payload = node
    for key, value in node.items():
        if _norm_key(key) in _LOADSET_KEYS and isinstance(value, dict):
            candidates = [v for v in value.values() if isinstance(v, dict)]
            if candidates:
                payload = max(candidates, key=lambda p: _magnitude(p) or 0.0)
            break
    if not isinstance(payload, dict):
        return _shear_vector(payload), None

    shear_part, rest = _divide(payload, _SHEAR_KEYS, _SHEAR_PREFIXES)
    normal_part, _ = _divide(rest or {}, _NORMAL_KEYS, _NORMAL_PREFIXES)
    shear = _shear_vector(shear_part) if shear_part is not None else None
    normal = _magnitude(normal_part) if normal_part is not None else None
    if shear is None and normal is None:
        shear = _shear_vector(payload)
    return shear, normal


def _has_numbers(node, depth: int = 4) -> bool:
    if isinstance(node, bool):
        return False
    if isinstance(node, (int, float)):
        return True
    if depth <= 0:
        return False
    if isinstance(node, dict):
        return any(_has_numbers(v, depth - 1) for v in node.values())
    if isinstance(node, list):
        return any(_has_numbers(v, depth - 1) for v in node)
    return False


_FORCE_KEY_PREFIXES = ("force", "shear", "tension", "tensile", "normal",
                       "axial", "unity", "check", "moment")


def _has_force_keys(node, depth: int = 4) -> bool:
    if depth <= 0 or not isinstance(node, dict):
        return False
    for k, v in node.items():
        nk = _norm_key(k)
        if nk in _LOADSET_KEYS or any(nk.startswith(p) for p in _FORCE_KEY_PREFIXES):
            return True
        if isinstance(v, (dict, list)) and _has_force_keys(v, depth - 1):
            return True
    return False


def _bolt_result_nodes(raw) -> Dict[str, dict]:
    """Map bolt name -> per-bolt results dict found anywhere in the document.

    A node counts as a bolt results node when it is keyed by a bolt-like
    identifier (or carries a bolt-like ``name``/``id`` field) and holds numeric
    force data.  Load-case buckets and other dictionaries that merely happen to
    be keyed by a number are rejected.
    """
    found: Dict[str, dict] = {}

    def walk(node, typed_name=None, in_loadset=False):
        if isinstance(node, dict):
            name = None if in_loadset else typed_name
            if name is None and not in_loadset:
                for k in ("name", "boltName", "boltId", "id"):
                    v = node.get(k)
                    if isinstance(v, str) and _BOLT_ID_RE.match(v):
                        name = v
                        break
            if name and name not in found and _has_numbers(node) \
                    and (typed_name is None or _has_force_keys(node)):
                found[name] = node
            for k, v in node.items():
                if isinstance(v, dict):
                    nk = _norm_key(k)
                    child = (k if isinstance(k, str) and _BOLT_ID_RE.match(k)
                             and not in_loadset and nk not in _LOADSET_KEYS
                             else None)
                    walk(v, child, in_loadset or nk in _LOADSET_KEYS)
                elif isinstance(v, list):
                    walk(v, None, in_loadset)
        elif isinstance(node, list):
            for v in node:
                walk(v, typed_name, in_loadset)

    walk(raw)
    return found


def _name_suffix(name: str) -> Tuple[str, int]:
    """Sort key for opaque bolt identifiers ("B15" -> ("B", 15))."""
    m = re.search(r"([A-Za-z]*)(\d+)\s*$", str(name or ""))
    return (m.group(1), int(m.group(2))) if m else (str(name or ""), 0)


def _bolt_nodes_for(bolts: List[BoltResult],
                    nodes: Dict[str, dict]) -> Dict[str, dict]:
    """Align the raw per-bolt nodes with the reported bolt results.

    Names are matched directly when the two result views agree.  When they do
    not, the API's documented rule is used instead: bolt identifiers are opaque,
    so bolts map to sequential positions by sorting the opaque keys
    numerically.
    """
    if not nodes or any(b.name in nodes for b in bolts):
        return nodes
    if len(nodes) != len(bolts):
        return {}
    raw = [node for _, node in sorted(nodes.items(), key=lambda kv: _name_suffix(kv[0]))]
    ordered = sorted(bolts, key=lambda b: _name_suffix(b.name))
    return {b.name: node for b, node in zip(ordered, raw)}


def _find_number(raw, wanted: Tuple[str, ...], limit: int = 4000) -> Optional[float]:
    """First finite number stored under one of the (normalised) keys."""
    stack = [raw]
    seen = 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            for k, v in node.items():
                if _norm_key(k) in wanted and isinstance(v, (int, float)) \
                        and not isinstance(v, bool):
                    return float(v)
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return None
