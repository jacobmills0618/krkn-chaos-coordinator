"""Typed tool functions for FILTER, MAP, and ANALYZE phases.

Each function accepts domain objects, calls the underlying LLM function,
and returns a (result, observation) tuple. Error recovery is handled
internally -- callers never see raw exceptions.
"""

from __future__ import annotations

import logging
import time

from src.filter.llm_config import LLMBackendConfig, detect_llm_backend
from src.models import (
    AnalyzeContext,
    Bug,
    FilterContext,
    FilterResult,
    GapAnalysis,
    MapContext,
    Observation,
    ScenarioMatch,
)

logger = logging.getLogger(__name__)


def filter_bug_llm(
    bug: Bug,
    context: FilterContext,
    config: LLMBackendConfig | None = None,
) -> tuple[FilterResult, Observation]:
    """FILTER phase tool. Returns typed FilterResult + Observation."""
    if config is None:
        config = detect_llm_backend(phase="filter")

    start = time.monotonic()
    try:
        from src.filter.llm_filter import llm_filter_bug

        result = llm_filter_bug(
            bug,
            config=config,
            ocp_docs=list(context.ocp_docs),
            krkn_docs=list(context.krkn_docs),
        )
        elapsed = time.monotonic() - start

        if result.chaos_relevant:
            obs = Observation(
                status="success",
                summary=f"{bug.key} is chaos-relevant: {result.failure_mode}",
                next_actions=("proceed_to_map",),
                artifacts={
                    "bug_key": bug.key,
                    "injection": result.injection_method,
                    "elapsed": round(elapsed, 2),
                },
            )
        else:
            obs = Observation(
                status="success",
                summary=f"{bug.key} not chaos-relevant: {result.skip_reason}",
                next_actions=("skip",),
                artifacts={
                    "bug_key": bug.key,
                    "elapsed": round(elapsed, 2),
                },
            )
        return result, obs

    except Exception as e:
        elapsed = time.monotonic() - start
        logger.warning("filter_bug_llm failed for %s: %s", bug.key, e)
        from src.filter.chaos_filter import filter_bug

        fallback = filter_bug(bug)
        obs = Observation(
            status="error",
            summary=f"{bug.key}: LLM filter failed, using keyword fallback: {e}",
            next_actions=("used_keyword_fallback",),
            artifacts={
                "bug_key": bug.key,
                "error": str(e),
                "elapsed": round(elapsed, 2),
            },
        )
        return fallback, obs


def map_match_llm(
    bug: Bug,
    filter_result: FilterResult,
    context: MapContext,
    config: LLMBackendConfig | None = None,
) -> tuple[ScenarioMatch, Observation]:
    """MAP phase tool. Returns typed ScenarioMatch + Observation."""
    if config is None:
        config = detect_llm_backend(phase="map")

    start = time.monotonic()
    try:
        from src.reasoning import llm_map_match

        result = llm_map_match(
            bug,
            filter_result,
            scenario_hits=list(context.scenario_hits),
            doc_hits=list(context.doc_hits),
            config=config,
            kb_context=context.kb_context,
        )
        elapsed = time.monotonic() - start

        from src.models import MatchResult

        if result.match_result == MatchResult.FULL_MATCH:
            obs = Observation(
                status="success",
                summary=(
                    f"{bug.key} fully matched: {result.matched_scenario}"
                ),
                next_actions=("skip_full_match",),
                artifacts={
                    "bug_key": bug.key,
                    "scenario": result.matched_scenario,
                    "similarity": result.similarity_score,
                    "elapsed": round(elapsed, 2),
                },
            )
        else:
            obs = Observation(
                status="success",
                summary=(
                    f"{bug.key} match={result.match_result.value}, "
                    f"scenario={result.matched_scenario}"
                ),
                next_actions=("proceed_to_analyze",),
                artifacts={
                    "bug_key": bug.key,
                    "match_result": result.match_result.value,
                    "scenario": result.matched_scenario,
                    "similarity": result.similarity_score,
                    "elapsed": round(elapsed, 2),
                },
            )
        return result, obs

    except Exception as e:
        elapsed = time.monotonic() - start
        logger.warning("map_match_llm failed for %s: %s", bug.key, e)
        from src.reasoning import _fallback_match

        fallback = _fallback_match(bug, list(context.scenario_hits))
        obs = Observation(
            status="error",
            summary=f"{bug.key}: LLM map failed, using distance fallback: {e}",
            next_actions=("used_distance_fallback",),
            artifacts={
                "bug_key": bug.key,
                "error": str(e),
                "elapsed": round(elapsed, 2),
            },
        )
        return fallback, obs


def analyze_gap_llm(
    bug: Bug,
    match: ScenarioMatch,
    context: AnalyzeContext,
    config: LLMBackendConfig | None = None,
) -> tuple[GapAnalysis, Observation]:
    """ANALYZE phase tool. Returns typed GapAnalysis + Observation."""
    if config is None:
        config = detect_llm_backend(phase="analyze")

    start = time.monotonic()
    try:
        from src.reasoning import llm_analyze_gap

        result = llm_analyze_gap(
            bug,
            match,
            ocp_docs=list(context.ocp_docs),
            krkn_docs=list(context.krkn_docs),
            neo4j_history=list(context.neo4j_history),
            config=config,
        )
        elapsed = time.monotonic() - start

        from src.models import Confidence

        if result.confidence_level == Confidence.HIGH:
            action = "create_pr"
        elif result.confidence_level == Confidence.MEDIUM:
            action = "create_issue"
        else:
            action = "log_gap_only"

        obs = Observation(
            status="success",
            summary=(
                f"{bug.key} confidence={result.confidence_score} "
                f"({result.confidence_level.value}), action={action}"
            ),
            next_actions=(action,),
            artifacts={
                "bug_key": bug.key,
                "confidence_score": result.confidence_score,
                "confidence_level": result.confidence_level.value,
                "base_scenario": result.base_scenario,
                "elapsed": round(elapsed, 2),
            },
        )
        return result, obs

    except Exception as e:
        elapsed = time.monotonic() - start
        logger.warning("analyze_gap_llm failed for %s: %s", bug.key, e)
        from src.models import ActionType, Confidence

        fallback = GapAnalysis(
            bug=bug,
            confidence_score=0,
            confidence_level=Confidence.LOW,
            action_type=ActionType.GITHUB_ISSUE,
            reasoning=f"LLM analysis failed: {e}",
            base_scenario=match.matched_scenario,
        )
        obs = Observation(
            status="error",
            summary=f"{bug.key}: LLM analyze failed: {e}",
            next_actions=("log_gap_only",),
            artifacts={
                "bug_key": bug.key,
                "error": str(e),
                "elapsed": round(elapsed, 2),
            },
        )
        return fallback, obs
