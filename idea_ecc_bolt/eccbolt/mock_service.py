"""
A *mock* of the IDEA StatiCa Connection REST API service.

IDEA StatiCa is a commercial Windows application; this module provides a
light-weight stand-in that implements exactly the endpoints used by
:mod:`eccbolt.idea_service` so that the whole client pipeline (SDK client ->
HTTP -> IOM import -> load effects -> calculation -> results -> report) can be
exercised and regression tested on any platform, without a license.

The mock is not a fake *answer*: it parses the uploaded IOM document, rebuilds
the bolt group and the load, and evaluates the bolt forces with the
Crawford & Kulak instantaneous-centre-of-rotation method implemented in
:mod:`eccbolt.icr`.  The returned per-bolt utilizations are therefore
physically meaningful (they equal the C&K solution of the imported model).
"""
from __future__ import annotations

import json
import math
import re
import threading
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np

from .icr import solve_from_eccentricity
from .iom import parse_open_model
from .model import CK_DELTA_MAX


class _State:
    def __init__(self):
        self.projects = {}


STATE = _State()


def _camel_to_json(obj):
    return json.dumps(obj)


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockIdeaStatiCaConnectionApi/4.0"

    # ------------------------------------------------------------ helpers
    def _send(self, code, payload=None, raw: bytes = None, ctype="application/json"):
        body = raw if raw is not None else (
            json.dumps(payload).encode() if payload is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ------------------------------------------------------------ routing
    def do_GET(self):
        try:
            self.route_get()
        except Exception as exc:  # keep the connection alive on bugs
            try:
                self._send(500, {"title": f"mock service error: {exc}"})
            except Exception:
                pass

    def do_POST(self):
        try:
            self.route_post()
        except Exception as exc:
            try:
                self._send(500, {"title": f"mock service error: {exc}"})
            except Exception:
                pass

    def route_get(self):
        path = urlparse(self.path).path
        if path.endswith("/clients/connect-client"):
            return self._send(200, raw=json.dumps(str(uuid.uuid4())).encode())
        if path.endswith("/clients/idea-service-version"):
            return self._send(200, raw=json.dumps("26.0.0.0-mock").encode())
        m = re.match(r".*/projects/([^/]+)/connections$", path)
        if m:
            pid = m.group(1)
            proj = STATE.projects.get(pid, {})
            return self._send(200, proj.get("connections", []))
        m = re.match(r".*/projects/([^/]+)/connections/(\d+)/members$", path)
        if m:
            pid = m.group(1)
            return self._send(200, STATE.projects.get(pid, {}).get("members", []))
        m = re.match(r".*/projects/([^/]+)/connections/(\d+)/load-effects$", path)
        if m:
            pid = m.group(1)
            return self._send(200, STATE.projects.get(pid, {}).get("load_effects", []))
        m = re.match(r".*/projects/([^/]+)/download$", path)
        if m:
            pid = m.group(1)
            return self._send(200, raw=STATE.projects.get(pid, {}).get("iom", b"mock"),
                              ctype="application/octet-stream")
        m = re.match(r".*/projects/([^/]+)/connections/(\d+)/reports/pdf$", path)
        if m:
            return self._send(200, raw=b"%PDF-1.4\n% mock report\n%%EOF\n",
                              ctype="application/pdf")
        m = re.match(r".*/projects/([^/]+)/close$", path)
        if m:
            STATE.projects.pop(m.group(1), None)
            return self._send(200, raw=b"true")
        return self._send(404, {"title": "not found", "path": path})

    def route_post(self):
        path = urlparse(self.path).path
        body = self._body()
        if path.endswith("/projects/import-iom-file"):
            return self._import_iom(body)
        m = re.match(r".*/projects/([^/]+)/connections/(\d+)/load-effects$", path)
        if m:
            pid = m.group(1)
            le = json.loads(body or b"{}")
            le.setdefault("id", 1)
            STATE.projects[pid].setdefault("load_effects", []).append(le)
            return self._send(200, le)
        m = re.match(r".*/projects/([^/]+)/connections/(\d+)/load-effects/set-load-settings$", path)
        if m:
            return self._send(200, json.loads(body or b"{}"))
        m = re.match(r".*/projects/([^/]+)/connections/calculate$", path)
        if m:
            pid = m.group(1)
            ids = json.loads(body or b"[]")
            return self._send(200, STATE.projects[pid].get("summary", []))
        m = re.match(r".*/projects/([^/]+)/connections/results$", path)
        if m:
            pid = m.group(1)
            return self._send(200, STATE.projects[pid].get("checks", []))
        m = re.match(r".*/projects/([^/]+)/connections/rawresults-text$", path)
        if m:
            pid = m.group(1)
            return self._send(200, STATE.projects[pid].get("raw", []))
        return self._send(404, {"title": "not found", "path": path})

    def do_PUT(self):
        return self._send(200, json.loads(self._body() or b"{}"))

    # ------------------------------------------------------------ import
    def _import_iom(self, body: bytes):
        # multipart body: locate the IOM document (OpenModelContainer or a bare
        # OpenModel, UTF-8 or UTF-16)
        starts = [p for p in (body.find(b"\xff\xfe"), body.find(b"<?xml"),
                              body.find(b"<OpenModelContainer"),
                              body.find(b"<OpenModel")) if p >= 0]
        i = min(starts) if starts else 0
        xml_bytes = body[i:] if starts else body
        if getattr(self.server, "reject_first_n", 0) > 0:
            self.server.reject_first_n -= 1
            return self._send(500, {"type": "https://tools.ietf.org/html/rfc9110#section-15.6.1",
                                    "title": "Internal server error", "status": 500,
                                    "detail": "An unexpected error occurred. "
                                              "See server logs for details.",
                                    "instance": "/api/4/projects/import-iom-file"})
        if xml_bytes.startswith(b"\xff\xfe") or xml_bytes[:2] == b"<\x00":
            txt = xml_bytes.decode("utf-16", "replace")
        else:
            txt = xml_bytes.decode("utf-8", "replace")
        txt = txt.lstrip("\ufeff\x00 \t\r\n")
        # trim anything after the document (the multipart tail)
        end = max((txt.rfind(t) + len(t) for t in
                   ("</OpenModelContainer>", "</OpenModel>")), default=0)
        if end:
            txt = txt[:end]
        try:
            root = parse_open_model(txt)
        except (ET.ParseError, ValueError) as exc:
            return self._send(400, {"title": f"IOM parse error: {exc}"})

        bolts = []
        bg = root.find("Connections/ConnectionData/BoltGrids/BoltGrid")
        if bg is not None:
            for p in bg.find("Positions"):
                bolts.append((float(p.find("X").text), float(p.find("Z").text)))
        load = dict(P=0.0, e=0.0, theta=0.0)
        STATE_load = load
        pid = str(uuid.uuid4())
        members = [
            {"id": 1, "name": "SupportPlate", "isBearing": True, "isContinuous": True,
             "crossSectionId": 1, "connectedBy": "begin"},
            {"id": 2, "name": "LoadStub", "isBearing": False, "isContinuous": False,
             "crossSectionId": 2, "connectedBy": "end"},
        ]
        STATE.projects[pid] = dict(iom=xml_bytes, bolts=bolts, members=members,
                                   connections=[{"id": 1, "identifier": str(uuid.uuid4()),
                                                 "name": "CON1",
                                                 "analysisType": "stress_Strain",
                                                 "isCalculated": False}],
                                   load_effects=[], summary=[], checks=[], raw=[])
        return self._send(200, {"projectId": pid,
                                "projectInfo": {"name": "mock", "description": ""},
                                "connections": STATE.projects[pid]["connections"]})

    # ------------------------------------------------------------ calculate


def finish_calc(pid):
    """Evaluate the imported model with the C&K ICR method (mock CBFEM)."""
    proj = STATE.projects[pid]
    bolts = proj["bolts"]
    les = proj["load_effects"]
    # member-local components of the load stub (vertical member, ZX LCS):
    #   N -> global Z,  Vz -> global X,  My(local) = -My(global Y)
    fx, fz, myl = 0.0, 0.0, 0.0
    for le in les:
        for ml in le.get("memberLoadings") or []:
            sl = ml.get("sectionLoad") or {}
            if ml.get("memberId") == 2:      # load stub carries the load
                fz += sl.get("n", 0.0)
                fx += sl.get("vz", 0.0)
                myl += sl.get("my", 0.0)
    P = math.hypot(fx, fz)
    res = None
    if bolts and P > 0:
        pdir = (fx / P, fz / P)              # load direction in plane (x, z)
        ecc = -myl / P                       # signed perpendicular eccentricity
        res = solve_from_eccentricity(bolts, pdir, ecc)
    rult = 1.0
    # per-bolt utilisation: force at applied load / Rult (C&K distribution)
    bolt_res = []
    raw_bolts = []
    if res is not None and res.C > 0:
        for i, mag in enumerate(res.bolt_magnitudes):
            util = float(mag / res.C)        # R_i(P) / Rult
            bolt_res.append({"name": f"B{i+1}", "unityCheck": round(util, 4),
                             "checkStatus": util <= 1.0})
            raw_bolts.append({"name": f"B{i+1}",
                              "shearForce": {"x": 0.0, "z": float(mag) * 1.0},
                              "normalForce": 0.0})
    max_util = max([b["unityCheck"] for b in bolt_res], default=0.0)
    proj["summary"] = [{
        "id": 1, "passed": bool(max_util <= 1.0),
        "resultSummary": [
            {"name": "Bolt group (C&K ICR mock)", "checkValue": round(max_util, 4),
             "checkStatus": bool(max_util <= 1.0), "loadCaseId": 1,
             "unityCheckMessage": f"utilization {max_util:.2f}", "skipped": False}]}]
    proj["checks"] = [{
        "id": 1, "connectionId": "1", "name": "CON1",
        "checkResSummary": proj["summary"][0]["resultSummary"],
        "checkResBolt": bolt_res,
        "checkResPlate": [
            {"name": "SupportPlate", "checkStatus": True, "loadCaseId": 1,
             "maxStrain": 0.001, "maxStress": 200e6},
            {"name": "LoadedPlate", "checkStatus": True, "loadCaseId": 1,
             "maxStrain": 0.001, "maxStress": 210e6}],
        "checkResWeld": [], "checkResAnchor": [], "checkResConcreteBlock": [],
        "bucklingResults": [], "messages": None}]
    proj["raw"] = [json.dumps({"C": (res.C if res else None),
                               "bolts": raw_bolts})]


class MockIdeaService:
    """Context manager running the mock service in a background thread."""

    def __init__(self, port: int = 0, reject_first_n: int = 0):
        self.reject_first_n = reject_first_n
        outer = self

        class H(MockHandler):
            def route_post(self):
                path = urlparse(self.path).path
                m = re.match(r".*/projects/([^/]+)/connections/calculate$", path)
                if m:
                    self._body()
                    outer_finish(m.group(1))
                    return self._send(200, STATE.projects[m.group(1)].get("summary", []))
                m = re.match(r".*/projects/([^/]+)/connections/(results|rawresults-text)$", path)
                if m:
                    self._body()
                    outer_finish(m.group(1))
                    key = "checks" if m.group(2) == "results" else "raw"
                    return self._send(200, STATE.projects[m.group(1)].get(key, []))
                return super().route_post()

        def outer_finish(pid):
            if not STATE.projects.get(pid, {}).get("done"):
                STATE.projects[pid]["done"] = True
                finish_calc(pid)

        self.handler = H
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.reject_first_n = reject_first_n
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        return False



def main():
    """CLI: python -m eccbolt.mock_service [--port N] - run the mock service
    in the foreground (used by tests and for offline exploration)."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), MockHandler)
    print(f"mock IDEA StatiCa Connection API on http://127.0.0.1:{args.port}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
