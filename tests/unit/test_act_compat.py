"""Tests for tolerant model coercion and issue title helpers."""

from datetime import datetime

from src.agents.act import build_issue_title
from src.models import ActionType, Bug, Confidence, ConfidenceLevel, GapAnalysis


def test_confidence_level_alias():
    assert ConfidenceLevel is Confidence
    assert ConfidenceLevel.LOW is Confidence.LOW


def test_bug_coerces_datetime_created():
    bug = Bug(
        key="OCP-1",
        summary="s",
        description="",
        component="etcd",
        priority="Major",
        status="Open",
        created=datetime(2026, 6, 12),
        url="https://example.com",
        release_version="4.21",
    )
    assert bug.created.startswith("2026")
    assert bug.fixed_in_release == "4.21"


def test_gap_analysis_accepts_llm_extra_fields_and_string_modifications():
    bug = Bug(
        key="OCPBUGS-88315",
        summary="Test panic",
        description="",
        component="MCO",
        priority="Medium",
        status="NEW",
        created="2026-06-12",
        url="https://issues.redhat.com/browse/OCPBUGS-88315",
    )
    gap = GapAnalysis(
        bug=bug,
        agent="upgrade_lifecycle",
        confidence_score=22,
        confidence_level=ConfidenceLevel.LOW,
        action_type="github_issue",
        reasoning="No existing scenario",
        modifications="Create new scenario",
        krkn_plugin="pod_scenarios",
        repos_to_update="krkn, krkn-hub",
    )
    assert gap.modifications == ["Create new scenario"]
    assert gap.action_type == ActionType.GITHUB_ISSUE
    assert gap.agent == "upgrade_lifecycle"


def test_build_issue_title_legacy_three_arg_form():
    title = build_issue_title("OCPBUGS-88315", "node delete stale state", 22)
    assert "OCPBUGS-88315" in title
    assert "[LOW]" in title
