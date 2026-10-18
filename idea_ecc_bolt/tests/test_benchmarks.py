"""Tests of the experimental-data comparison.

The data (bolt patterns, measured material strengths, observed failure loads)
is in :mod:`eccbolt.benchmarks`; see its module docstring for the sources.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eccbolt.benchmarks import (ACCEPTANCE, BADAWI, CK_BAND, compare_case,
                                experimental_cases, format_comparison,
                                tune_and_compare)


def test_only_experimental_sources():
    cases = experimental_cases()
    assert len(cases) == 10
    assert {c.source for c in cases} == {BADAWI}
    for c in cases:
        assert c.failure_load > 0
        assert c.eccentricity > 0
        assert c.bolt_rult > 0
        assert len(c.bolts_xy) >= 2


@pytest.mark.parametrize("name", [c.name for c in experimental_cases()])
def test_specimen_input_roundtrip(name):
    case = next(c for c in experimental_cases() if c.name == name)
    ci = case.connection_input()
    assert ci.n_bolts == len(case.bolts_xy)
    assert ci.bolt_rult() == pytest.approx(case.bolt_rult)
    assert ci.load.P == pytest.approx(case.failure_load)
    assert ci.load.e == pytest.approx(case.eccentricity)
    # the measured tensile strength must reach the IDEA material
    assert ci.plate_a.material.fu == pytest.approx(case.plate_fu)


def test_analytical_bias_matches_the_published_band():
    """C&K predictions sit 5-16 % above the observed failure loads."""
    comps = [compare_case(c) for c in experimental_cases()]
    mean = sum(c.analytical_ratio for c in comps) / len(comps)
    assert CK_BAND[0] <= mean <= CK_BAND[1]
    assert all(c.idea_ratio is None for c in comps)   # no service connected


def test_format_comparison_lists_every_specimen():
    comps = [compare_case(c) for c in experimental_cases()]
    text = format_comparison(comps)
    for c in comps:
        assert c.case.name in text
    assert "measured" in text and "C&K" in text


class _FakeWorkflow:
    """Stands in for the REST workflow: no capacity at first, then a capacity -
    the tuning loop must find it."""

    def __init__(self, succeed_after=2):
        self.calls = []
        self.succeed_after = succeed_after

    def analyse(self, ci, out_dir=".", save_project=True, save_report=True,
                settings=None):
        self.calls.append(settings)
        from eccbolt.idea_service import IdeaAnalysisResult
        res = IdeaAnalysisResult(ok=True)
        if len(self.calls) > self.succeed_after:
            res.capacity = ci.load.P / 1.05
            res.capacity_source = "fake"
        else:
            res.checks = ["Analysis: Singularity"]
        return res

    def close(self):
        pass


def test_tuning_steps_are_tried_until_a_result_appears():
    case = experimental_cases()[0]
    wf = _FakeWorkflow(succeed_after=2)
    comp = tune_and_compare(case, wf)
    assert len(wf.calls) == 3
    assert comp.idea_capacity is not None
    assert comp.settings.get("step") == "fine mesh"
    assert ACCEPTANCE[0] <= comp.idea_ratio <= ACCEPTANCE[1]


def test_tuning_reports_the_error_when_nothing_succeeds():
    case = experimental_cases()[0]
    wf = _FakeWorkflow(succeed_after=99)
    comp = tune_and_compare(case, wf)
    assert comp.idea_capacity is None
    assert "Singularity" in comp.idea_error
    assert len(wf.calls) == 5
