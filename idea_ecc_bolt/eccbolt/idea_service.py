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
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .iom import IomBuilder, member_load_components
from .model import ConnectionInput

log = logging.getLogger("eccbolt.idea")


@dataclass
class BoltResult:
    name: str
    unity_check: Optional[float]
    ok: Optional[bool]
    shear_v: Optional[float] = None     # N, resultant shear from raw results
    normal_n: Optional[float] = None    # N


@dataclass
class PlateResult:
    name: str
    max_stress: Optional[float]         # Pa
    max_strain: Optional[float]
    ok: Optional[bool]


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
                save_project: bool = True, save_report: bool = True) -> IdeaAnalysisResult:
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
            raw = json.loads(raw_txt[0])
            res.raw = raw
            self._extract_bolt_forces(raw, res)
        except Exception as exc:                     # pragma: no cover
            self.emit(f"(raw results unavailable: {exc})")

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
    def _extract_bolt_forces(raw: dict, res: IdeaAnalysisResult):
        """Pull per-bolt shear/normal forces out of the raw CheckResultsData."""
        def walk(node):
            if isinstance(node, dict):
                if "name" in node and ("shearForce" in node or "V" in node):
                    yield node
                for v in node.values():
                    yield from walk(v)
            elif isinstance(node, list):
                for v in node:
                    yield from walk(v)
        forces = {}
        for node in walk(raw):
            nm = node.get("name")
            sf = node.get("shearForce", node.get("V"))
            nf = node.get("normalForce", node.get("N"))
            if isinstance(sf, dict):
                import math
                sf = math.sqrt(sum(v * v for v in sf.values() if isinstance(v, (int, float))))
            forces[nm] = (sf, nf)
        for b in res.bolts:
            if b.name in forces:
                b.shear_v, b.normal_n = forces[b.name]
