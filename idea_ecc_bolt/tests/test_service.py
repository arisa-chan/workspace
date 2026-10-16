"""Tests for service discovery / auto-start / friendly failure handling."""
import os
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eccbolt.idea_service import (IdeaServiceNotRunning, IdeaWorkflow,
                                  ServiceManager, parse_base_url,
                                  service_is_up, wait_for_service)
from eccbolt.mock_service import MockIdeaService


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_service_is_up_false_on_dead_port():
    port = _free_port()
    assert service_is_up(f"http://127.0.0.1:{port}", timeout=0.5) is False


def test_service_is_up_true_with_heartbeat():
    with MockIdeaService() as svc:
        assert service_is_up(svc.url, timeout=2.0) is True


def test_service_manager_starts_and_stops():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    cmd = [sys.executable, "-m", "eccbolt.mock_service", "--port", str(port)]
    mgr = ServiceManager(url, logger=lambda s: None, start_cmd=cmd)
    assert service_is_up(url, timeout=0.5) is False
    assert mgr.ensure_running(wait_s=30) is True
    assert service_is_up(url, timeout=2.0) is True
    mgr.stop()
    time.sleep(0.5)
    assert service_is_up(url, timeout=0.5) is False


def test_connect_raises_friendly_error_when_no_service():
    port = _free_port()
    wf = IdeaWorkflow(base_url=f"http://127.0.0.1:{port}", auto_start=False,
                      logger=lambda s: None)
    with pytest.raises(IdeaServiceNotRunning) as ei:
        wf.connect()
    msg = str(ei.value)
    assert "connection refused" in msg
    assert "IdeaStatiCa.ConnectionRestApi.exe" in msg
    assert "mock" in msg


def test_connect_autostart_via_manager():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    cmd = [sys.executable, "-m", "eccbolt.mock_service", "--port", str(port)]
    wf = IdeaWorkflow(base_url=url, auto_start=True, logger=lambda s: None)
    # inject the test start command (no IDEA installation on Linux)
    import eccbolt.idea_service as isvc
    orig = isvc.ServiceManager

    class TestMgr(orig):
        def __init__(self, *a, **kw):
            kw["start_cmd"] = cmd
            super().__init__(*a, **kw)

    isvc.ServiceManager = TestMgr
    try:
        wf.connect()
        assert wf.service_manager is not None
        wf.close()
    finally:
        isvc.ServiceManager = orig
        wf.service_manager and wf.service_manager.stop()


def test_parse_base_url():
    assert parse_base_url("http://localhost:5000") == ("localhost", 5000)
    assert parse_base_url("http://127.0.0.1:5193/") == ("127.0.0.1", 5193)


def test_import_falls_back_when_service_rejects_first_variant(tmp_path):
    """Simulates a real service answering HTTP 500 problem+json to the first
    IOM variant (the failure mode reported from the field): the workflow must
    retry the remaining variants and succeed."""
    from eccbolt.idea_service import IdeaWorkflow
    from eccbolt.mock_service import MockIdeaService
    from eccbolt.presets import crawford_kulak_example
    logs = []
    with MockIdeaService(reject_first_n=1) as svc:
        wf = IdeaWorkflow(base_url=svc.url, logger=logs.append)
        wf.connect()
        res = wf.analyse(crawford_kulak_example(), out_dir=str(tmp_path),
                         save_project=False, save_report=False)
        wf.close()
    assert res.ok
    assert any("rejected" in l for l in logs)
    assert any("accepted" in l for l in logs)
    import os
    assert sorted(f for f in os.listdir(tmp_path)
                  if f.startswith("connection_iom_"))
