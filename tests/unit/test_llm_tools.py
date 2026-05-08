"""Tests for typed tool functions in src/filter/llm_tools.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.filter.llm_config import LLMBackendConfig, LLMProvider
from src.models import (
    ActionType,
    AnalyzeContext,
    Bug,
    Confidence,
    FilterContext,
    FilterResult,
    GapAnalysis,
    MapContext,
    MatchResult,
    Observation,
    ScenarioMatch,
)
from src.filter.llm_tools import analyze_gap_llm, filter_bug_llm, map_match_llm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_bug() -> Bug:
    return Bug(
        key="OCPBUGS-1234",
        summary="etcd leader election fails under CPU pressure",
        description="When CPU hog runs on master nodes, etcd loses quorum.",
        component="etcd",
        priority="Critical",
        status="New",
        created="2026-01-15",
        url="https://issues.redhat.com/browse/OCPBUGS-1234",
    )


@pytest.fixture()
def dummy_config() -> LLMBackendConfig:
    return LLMBackendConfig(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-6",
        api_key="test-key",
    )


@pytest.fixture()
def relevant_filter_result(sample_bug: Bug) -> FilterResult:
    return FilterResult(
        bug=sample_bug,
        chaos_relevant=True,
        failure_mode="etcd leader election failure under CPU stress",
        injection_method="resource_stress",
        confidence=0.92,
    )


@pytest.fixture()
def irrelevant_filter_result(sample_bug: Bug) -> FilterResult:
    return FilterResult(
        bug=sample_bug,
        chaos_relevant=False,
        skip_reason="This is a code logic bug, not a resilience issue",
        confidence=0.85,
    )


@pytest.fixture()
def partial_match(sample_bug: Bug) -> ScenarioMatch:
    return ScenarioMatch(
        bug=sample_bug,
        match_result=MatchResult.PARTIAL_MATCH,
        matched_scenario="scenarios/openshift/etcd.yml",
        matched_repo="krkn-chaos/krkn",
        similarity_score=0.6,
    )


@pytest.fixture()
def full_match(sample_bug: Bug) -> ScenarioMatch:
    return ScenarioMatch(
        bug=sample_bug,
        match_result=MatchResult.FULL_MATCH,
        matched_scenario="scenarios/openshift/etcd.yml",
        matched_repo="krkn-chaos/krkn",
        similarity_score=1.0,
    )


@pytest.fixture()
def no_match(sample_bug: Bug) -> ScenarioMatch:
    return ScenarioMatch(
        bug=sample_bug,
        match_result=MatchResult.NO_MATCH,
        similarity_score=0.0,
    )


# ---------------------------------------------------------------------------
# FILTER tests
# ---------------------------------------------------------------------------


class TestFilterBugLlm:
    """Tests for filter_bug_llm."""

    @patch("src.filter.llm_filter.llm_filter_bug")
    def test_filter_returns_typed_result_and_observation(
        self,
        mock_llm_filter: object,
        sample_bug: Bug,
        relevant_filter_result: FilterResult,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_filter.return_value = relevant_filter_result  # type: ignore[attr-defined]

        result, obs = filter_bug_llm(
            sample_bug, FilterContext(), config=dummy_config,
        )

        assert isinstance(result, FilterResult)
        assert isinstance(obs, Observation)

    @patch("src.filter.llm_filter.llm_filter_bug")
    def test_filter_relevant_bug_has_proceed_action(
        self,
        mock_llm_filter: object,
        sample_bug: Bug,
        relevant_filter_result: FilterResult,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_filter.return_value = relevant_filter_result  # type: ignore[attr-defined]

        result, obs = filter_bug_llm(
            sample_bug, FilterContext(), config=dummy_config,
        )

        assert result.chaos_relevant is True
        assert obs.status == "success"
        assert "proceed_to_map" in obs.next_actions
        assert obs.artifacts["bug_key"] == "OCPBUGS-1234"

    @patch("src.filter.llm_filter.llm_filter_bug")
    def test_filter_irrelevant_bug_has_skip_action(
        self,
        mock_llm_filter: object,
        sample_bug: Bug,
        irrelevant_filter_result: FilterResult,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_filter.return_value = irrelevant_filter_result  # type: ignore[attr-defined]

        result, obs = filter_bug_llm(
            sample_bug, FilterContext(), config=dummy_config,
        )

        assert result.chaos_relevant is False
        assert obs.status == "success"
        assert "skip" in obs.next_actions

    @patch("src.filter.llm_filter.llm_filter_bug")
    def test_filter_error_falls_back_to_keyword(
        self,
        mock_llm_filter: object,
        sample_bug: Bug,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_filter.side_effect = RuntimeError("API timeout")  # type: ignore[attr-defined]

        result, obs = filter_bug_llm(
            sample_bug, FilterContext(), config=dummy_config,
        )

        # The keyword filter should still produce a FilterResult
        assert isinstance(result, FilterResult)
        assert result.bug.key == "OCPBUGS-1234"

    @patch("src.filter.llm_filter.llm_filter_bug")
    def test_filter_error_observation_has_error_status(
        self,
        mock_llm_filter: object,
        sample_bug: Bug,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_filter.side_effect = RuntimeError("API timeout")  # type: ignore[attr-defined]

        _, obs = filter_bug_llm(
            sample_bug, FilterContext(), config=dummy_config,
        )

        assert obs.status == "error"
        assert "used_keyword_fallback" in obs.next_actions
        assert "API timeout" in obs.summary
        assert obs.artifacts["error"] == "API timeout"


# ---------------------------------------------------------------------------
# MAP tests
# ---------------------------------------------------------------------------


class TestMapMatchLlm:
    """Tests for map_match_llm."""

    @patch("src.reasoning.llm_map_match")
    def test_map_returns_typed_match_and_observation(
        self,
        mock_llm_map: object,
        sample_bug: Bug,
        relevant_filter_result: FilterResult,
        partial_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_map.return_value = partial_match  # type: ignore[attr-defined]

        result, obs = map_match_llm(
            sample_bug, relevant_filter_result, MapContext(), config=dummy_config,
        )

        assert isinstance(result, ScenarioMatch)
        assert isinstance(obs, Observation)

    @patch("src.reasoning.llm_map_match")
    def test_map_full_match_has_skip_action(
        self,
        mock_llm_map: object,
        sample_bug: Bug,
        relevant_filter_result: FilterResult,
        full_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_map.return_value = full_match  # type: ignore[attr-defined]

        result, obs = map_match_llm(
            sample_bug, relevant_filter_result, MapContext(), config=dummy_config,
        )

        assert result.match_result == MatchResult.FULL_MATCH
        assert obs.status == "success"
        assert "skip_full_match" in obs.next_actions

    @patch("src.reasoning.llm_map_match")
    def test_map_no_match_has_analyze_action(
        self,
        mock_llm_map: object,
        sample_bug: Bug,
        relevant_filter_result: FilterResult,
        no_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_map.return_value = no_match  # type: ignore[attr-defined]

        result, obs = map_match_llm(
            sample_bug, relevant_filter_result, MapContext(), config=dummy_config,
        )

        assert result.match_result == MatchResult.NO_MATCH
        assert obs.status == "success"
        assert "proceed_to_analyze" in obs.next_actions

    @patch("src.reasoning.llm_map_match")
    def test_map_error_uses_fallback(
        self,
        mock_llm_map: object,
        sample_bug: Bug,
        relevant_filter_result: FilterResult,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_map.side_effect = RuntimeError("Connection refused")  # type: ignore[attr-defined]

        result, obs = map_match_llm(
            sample_bug, relevant_filter_result, MapContext(), config=dummy_config,
        )

        assert isinstance(result, ScenarioMatch)
        assert obs.status == "error"
        assert "used_distance_fallback" in obs.next_actions
        assert obs.artifacts["error"] == "Connection refused"


# ---------------------------------------------------------------------------
# ANALYZE tests
# ---------------------------------------------------------------------------


class TestAnalyzeGapLlm:
    """Tests for analyze_gap_llm."""

    @patch("src.reasoning.llm_analyze_gap")
    def test_analyze_returns_typed_gap_and_observation(
        self,
        mock_llm_analyze: object,
        sample_bug: Bug,
        partial_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        gap = GapAnalysis(
            bug=sample_bug,
            confidence_score=75,
            confidence_level=Confidence.HIGH,
            action_type=ActionType.DRAFT_PR,
            reasoning="Strong match with existing etcd scenario",
            base_scenario="scenarios/openshift/etcd.yml",
            modifications=["Add CPU hog test case"],
        )
        mock_llm_analyze.return_value = gap  # type: ignore[attr-defined]

        result, obs = analyze_gap_llm(
            sample_bug, partial_match, AnalyzeContext(), config=dummy_config,
        )

        assert isinstance(result, GapAnalysis)
        assert isinstance(obs, Observation)

    @patch("src.reasoning.llm_analyze_gap")
    def test_analyze_high_confidence_has_pr_action(
        self,
        mock_llm_analyze: object,
        sample_bug: Bug,
        partial_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        gap = GapAnalysis(
            bug=sample_bug,
            confidence_score=85,
            confidence_level=Confidence.HIGH,
            action_type=ActionType.DRAFT_PR,
            reasoning="High confidence",
        )
        mock_llm_analyze.return_value = gap  # type: ignore[attr-defined]

        result, obs = analyze_gap_llm(
            sample_bug, partial_match, AnalyzeContext(), config=dummy_config,
        )

        assert result.confidence_score == 85
        assert obs.status == "success"
        assert "create_pr" in obs.next_actions

    @patch("src.reasoning.llm_analyze_gap")
    def test_analyze_low_confidence_has_log_action(
        self,
        mock_llm_analyze: object,
        sample_bug: Bug,
        partial_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        gap = GapAnalysis(
            bug=sample_bug,
            confidence_score=20,
            confidence_level=Confidence.LOW,
            action_type=ActionType.GITHUB_ISSUE,
            reasoning="Low confidence",
        )
        mock_llm_analyze.return_value = gap  # type: ignore[attr-defined]

        result, obs = analyze_gap_llm(
            sample_bug, partial_match, AnalyzeContext(), config=dummy_config,
        )

        assert result.confidence_score == 20
        assert obs.status == "success"
        assert "log_gap_only" in obs.next_actions

    @patch("src.reasoning.llm_analyze_gap")
    def test_analyze_medium_confidence_has_issue_action(
        self,
        mock_llm_analyze: object,
        sample_bug: Bug,
        partial_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        gap = GapAnalysis(
            bug=sample_bug,
            confidence_score=55,
            confidence_level=Confidence.MEDIUM,
            action_type=ActionType.GITHUB_ISSUE,
            reasoning="Medium confidence",
        )
        mock_llm_analyze.return_value = gap  # type: ignore[attr-defined]

        result, obs = analyze_gap_llm(
            sample_bug, partial_match, AnalyzeContext(), config=dummy_config,
        )

        assert result.confidence_score == 55
        assert obs.status == "success"
        assert "create_issue" in obs.next_actions

    @patch("src.reasoning.llm_analyze_gap")
    def test_analyze_error_returns_zero_confidence(
        self,
        mock_llm_analyze: object,
        sample_bug: Bug,
        partial_match: ScenarioMatch,
        dummy_config: LLMBackendConfig,
    ) -> None:
        mock_llm_analyze.side_effect = RuntimeError("Model overloaded")  # type: ignore[attr-defined]

        result, obs = analyze_gap_llm(
            sample_bug, partial_match, AnalyzeContext(), config=dummy_config,
        )

        assert isinstance(result, GapAnalysis)
        assert result.confidence_score == 0
        assert result.confidence_level == Confidence.LOW
        assert obs.status == "error"
        assert "log_gap_only" in obs.next_actions
        assert obs.artifacts["error"] == "Model overloaded"
