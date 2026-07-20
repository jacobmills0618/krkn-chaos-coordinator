"""Tests for domain models."""

from src.models import (
    ActionType,
    AgentResult,
    Bug,
    Confidence,
    FilterResult,
    GapAnalysis,
    MatchResult,
    ScenarioMatch,
)


def test_bug_is_immutable():
    bug = Bug(
        key="TEST-1",
        summary="test",
        description="desc",
        component="Etcd",
        priority="Major",
        status="New",
        created="2026-03-30",
        url="https://example.com",
    )
    assert bug.key == "TEST-1"


def test_filter_result_relevant():
    bug = Bug("K-1", "s", "d", "c", "p", "s", "c", "u")
    result = FilterResult(bug=bug, chaos_relevant=True, failure_mode="crash", injection_method="pod")
    assert result.chaos_relevant
    assert result.skip_reason is None


def test_filter_result_skipped():
    bug = Bug("K-1", "s", "d", "c", "p", "s", "c", "u")
    result = FilterResult(bug=bug, chaos_relevant=False, skip_reason="CVE")
    assert not result.chaos_relevant
    assert result.skip_reason == "CVE"


def test_scenario_match_no_match():
    bug = Bug("K-1", "s", "d", "c", "p", "s", "c", "u")
    match = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)
    assert match.matched_scenario is None


def test_gap_analysis_defaults():
    bug = Bug("K-1", "s", "d", "c", "p", "s", "c", "u")
    gap = GapAnalysis(bug=bug)
    assert gap.confidence_score == 0
    assert gap.confidence_level == Confidence.LOW
    assert gap.action_type == ActionType.GITHUB_ISSUE
    from src.models import FactorConfidence
    assert gap.reproduction_confidence == FactorConfidence.LOW
    assert gap.scenario_confidence == FactorConfidence.LOW
    assert gap.understanding_confidence == FactorConfidence.LOW
    assert gap.plugin_confidence == FactorConfidence.LOW
    assert gap.domain_confidence == FactorConfidence.LOW
    assert gap.history_confidence == FactorConfidence.LOW


def test_agent_result_empty():
    result = AgentResult(agent_name="test")
    assert result.bugs_discovered == []
    assert result.gaps == []
