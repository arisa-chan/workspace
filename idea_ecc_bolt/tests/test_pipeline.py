"""End-to-end test of the REST pipeline against the mock IDEA service."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eccbolt.idea_service import IdeaWorkflow
from eccbolt.mock_service import MockIdeaService
from eccbolt.model import (BoltMaterial, ConnectionInput, INCH, KIP, LoadSpec,
                           Plate, SteelMaterial)
from eccbolt.presets import crawford_kulak_example


def test_pipeline_mock():
    ci = crawford_kulak_example()
    with tempfile.TemporaryDirectory() as td:
        with MockIdeaService() as svc:
            wf = IdeaWorkflow(base_url=svc.url, logger=lambda s: print("   |", s))
            wf.connect()
            res = wf.analyse(ci, out_dir=td, save_project=True, save_report=True)
            wf.close()
            assert res.ok
            assert res.passed is not None
            assert len(res.bolts) == ci.n_bolts
            assert res.ideacon_path and os.path.exists(res.ideacon_path)
            assert res.report_path and os.path.exists(res.report_path)
    print("   pipeline OK; bolt utilizations:",
          [round(b.unity_check, 3) for b in res.bolts])


if __name__ == "__main__":
    test_pipeline_mock()
