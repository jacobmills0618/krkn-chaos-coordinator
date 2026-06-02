"""Base domain agent with the DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER pipeline."""

from __future__ import annotations

import logging
import time
from abc import ABC
from dataclasses import asdict

from src.apis.jira_client import JiraClient
from src.apis.sippy_client import SippyClient
from src.apis.github_client import GitHubClient
from src.filter.chaos_filter import filter_bug, filter_bugs
from src.filter.llm_filter import get_token_usage, reset_token_usage
from src.filter.llm_tools import filter_bug_llm, map_match_llm, analyze_gap_llm
from src.knowledge.chromadb_store import ChromaStore
from src.knowledge.component_map import get_components_for_agent
from src.knowledge.filter_cache import SemanticFilterCache
from src.knowledge.neo4j_store import Neo4jStore
from src.knowledge.scenario_index import ScenarioInfo
from src.logging_util import StructuredLogger
from src.models import (
    AgentResult,
    AnalyzeContext,
    Bug,
    FilterContext,
    FilterResult,
    GapAnalysis,
    MapContext,
    MatchResult,
    Observation,
    RunMetrics,
    ScenarioMatch,
)

logger = logging.getLogger(__name__)


class BaseDomainAgent(ABC):
    """Base class for domain-specific chaos coverage agents."""

    def __init__(
        self,
        agent_name: str,
        jira: JiraClient,
        sippy: SippyClient,
        github: GitHubClient,
        chroma: ChromaStore,
        scenarios: list[ScenarioInfo],
        release: str,
        neo4j_store: Neo4jStore,
        use_llm: bool = False,
        max_bugs: int = 2000,
        days: int = 14,
    ):
        self.agent_name = agent_name
        self.jira = jira
        self.sippy = sippy
        self.github = github
        self.chroma = chroma
        self.scenarios = scenarios
        self.release = release
        self.neo4j = neo4j_store
        self.use_llm = use_llm
        self.max_bugs = max_bugs
        self.days = days
        self.components = get_components_for_agent(agent_name)
        self._slog = StructuredLogger(f"coordinator.{agent_name}")

        try:
            self._filter_cache: SemanticFilterCache | None = SemanticFilterCache(
                self.chroma.client,
            )
        except Exception as e:
            logger.warning("Semantic filter cache unavailable: %s", e)
            self._filter_cache = None

    def run(self) -> AgentResult:
        """Execute the full pipeline: DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER."""
        logger.info("=== %s agent starting ===", self.agent_name)
        self._slog.clear()
        reset_token_usage()
        metrics = RunMetrics()

        # DISCOVER
        bugs = self._discover()
        logger.info("DISCOVER: found %d bugs", len(bugs))
        bugs = self._enrich_with_changelog(bugs)

        # Split into new vs known bugs
        known_keys = self._get_known_bugs()
        new_bugs = [b for b in bugs if b.key not in known_keys]
        known_bugs = [b for b in bugs if b.key in known_keys]

        if known_bugs:
            logger.info("DISCOVER: %d known bugs — updating status", len(known_bugs))
            self._update_known_bugs(known_bugs)

        if new_bugs:
            logger.info("DISCOVER: %d new bugs to analyze", len(new_bugs))

        metrics.bugs_processed = len(new_bugs)

        # FILTER
        filter_start = time.monotonic()
        relevant, skipped = self._filter(new_bugs, metrics)
        metrics.filter_duration_sec = round(time.monotonic() - filter_start, 2)
        logger.info("FILTER: %d relevant, %d skipped", len(relevant), len(skipped))

        # MAP
        map_start = time.monotonic()
        matched, unmatched = self._map(relevant, metrics)
        metrics.map_duration_sec = round(time.monotonic() - map_start, 2)
        logger.info("MAP: %d matched, %d unmatched", len(matched), len(unmatched))

        # ANALYZE
        analyze_start = time.monotonic()
        gaps = self._analyze(unmatched, metrics)
        metrics.analyze_duration_sec = round(time.monotonic() - analyze_start, 2)
        logger.info("ANALYZE: %d gaps identified", len(gaps))

        metrics.bugs_succeeded = len(relevant) + len(skipped)

        result = AgentResult(
            agent_name=self.agent_name,
            bugs_discovered=bugs,
            bugs_filtered_out=skipped,
            bugs_matched=matched,
            gaps=gaps,
        )

        # REMEMBER
        self._remember(result)

        usage = get_token_usage()
        metrics.total_input_tokens = usage["input_tokens"]
        metrics.total_output_tokens = usage["output_tokens"]
        self._store_metrics(metrics)

        logger.info(
            "TOKEN USAGE: %d input + %d output = %d total, cost=$%.4f, calls=%d",
            usage["input_tokens"], usage["output_tokens"],
            usage["total_tokens"], usage["cost_usd"], usage["call_count"],
        )
        logger.info("=== %s agent complete ===", self.agent_name)
        return result

    # ── DISCOVER ──────────────────────────────────────────────────

    def _discover(self) -> list[Bug]:
        return self.jira.get_bugs_by_components(
            self.components, days=self.days, max_results=self.max_bugs,
            release=self.release,
        )

    def _enrich_with_changelog(self, bugs: list[Bug]) -> list[Bug]:
        try:
            from src.apis.release_client import get_all_fixed_bugs
            fixed_map = get_all_fixed_bugs(self.release)
        except Exception as e:
            logger.warning("Changelog enrichment failed: %s", e)
            return bugs

        if not fixed_map:
            return bugs

        enriched = []
        fixed_count = 0
        for bug in bugs:
            bug_fix = fixed_map.get(bug.key)
            if bug_fix:
                bug = Bug(
                    key=bug.key, summary=bug.summary, description=bug.description,
                    component=bug.component, priority=bug.priority, status=bug.status,
                    created=bug.created, url=bug.url, all_components=bug.all_components,
                    fixed_in_release=bug_fix.fixed_in,
                    fix_commits=bug_fix.commits, fix_image=bug_fix.image,
                )
                fixed_count += 1
            enriched.append(bug)

        if fixed_count:
            logger.info("DISCOVER: %d bugs already fixed in z-streams", fixed_count)
        return enriched

    def _update_known_bugs(self, known_bugs: list[Bug]) -> None:
        self.neo4j.update_bug_statuses(known_bugs)

    def _get_known_bugs(self) -> set[str]:
        return self.neo4j.get_analyzed_bug_keys_sync()

    # ── FILTER ────────────────────────────────────────────────────

    def _filter(
        self, bugs: list[Bug], metrics: RunMetrics,
    ) -> tuple[list[FilterResult], list[FilterResult]]:
        if self.use_llm:
            logger.info("Using tiered filter: keyword → cache → LLM")
            return self._tiered_filter(bugs, metrics)
        return filter_bugs(bugs, agent_name=self.agent_name)

    def _tiered_filter(
        self, bugs: list[Bug], metrics: RunMetrics,
    ) -> tuple[list[FilterResult], list[FilterResult]]:
        """Three-tier filter: keyword → semantic cache → LLM."""
        relevant: list[FilterResult] = []
        skipped: list[FilterResult] = []
        needs_llm: list[Bug] = []

        # Layer 1: Keyword pre-filter
        for bug in bugs:
            kw_result = filter_bug(bug, agent_name=self.agent_name)
            if kw_result.confidence > 0.8:
                if kw_result.chaos_relevant:
                    relevant.append(kw_result)
                else:
                    skipped.append(kw_result)
                metrics.keyword_filter_hits += 1
                self._slog.log_phase("filter", "success", f"{bug.key}: keyword ({kw_result.confidence:.0%})",
                                     bug_key=bug.key, cache_hit=True)
            elif kw_result.confidence < 0.2:
                skipped.append(kw_result)
                metrics.keyword_filter_hits += 1
                self._slog.log_phase("filter", "success", f"{bug.key}: keyword skip",
                                     bug_key=bug.key, cache_hit=True)
            else:
                needs_llm.append(bug)

        # Layer 2: Semantic cache
        still_needs_llm: list[Bug] = []
        for bug in needs_llm:
            cached = self._filter_cache.get(bug.summary) if self._filter_cache else None
            if cached is not None:
                result = FilterResult(
                    bug=bug, chaos_relevant=cached.chaos_relevant,
                    failure_mode=cached.failure_mode,
                    injection_method=cached.injection_method, confidence=0.9,
                )
                if result.chaos_relevant:
                    relevant.append(result)
                else:
                    skipped.append(result)
                metrics.semantic_cache_hits += 1
                self._slog.log_phase("filter", "success", f"{bug.key}: cache hit",
                                     bug_key=bug.key, cache_hit=True)
            else:
                still_needs_llm.append(bug)

        # Layer 3: LLM via typed tool
        for bug in still_needs_llm:
            components = bug.all_components or (bug.component,)
            ocp_docs = self.chroma.search_per_component(
                components, bug.summary, collection="all", n_results=3,
            )
            krkn_docs = self.chroma.search_per_component(
                components, bug.summary, collection="krkn_docs", n_results=3,
            )
            ctx = FilterContext(ocp_docs=tuple(ocp_docs), krkn_docs=tuple(krkn_docs))

            result, obs = filter_bug_llm(bug, ctx)
            metrics.llm_filter_calls += 1
            if obs.status == "error":
                metrics.filter_retries += 1

            self._slog.log_phase("filter", obs.status, obs.summary, bug_key=bug.key)

            if result.chaos_relevant:
                relevant.append(result)
            else:
                skipped.append(result)

            if self._filter_cache:
                self._filter_cache.put(bug.summary, result)

        logger.info("FILTER tiers: %d keyword, %d cached, %d LLM",
                     len(bugs) - len(needs_llm),
                     len(needs_llm) - len(still_needs_llm),
                     len(still_needs_llm))
        return relevant, skipped

    # ── MAP ───────────────────────────────────────────────────────

    def _map(
        self, relevant: list[FilterResult], metrics: RunMetrics,
    ) -> tuple[list[ScenarioMatch], list[ScenarioMatch]]:
        matched = []
        unmatched = []

        for filter_result in relevant:
            bug = filter_result.bug
            match, obs = self._find_scenario_match(bug, filter_result, metrics)
            self._slog.log_phase("map", obs.status, obs.summary, bug_key=bug.key)

            if match.match_result == MatchResult.FULL_MATCH:
                matched.append(match)
            else:
                unmatched.append(match)

        return matched, unmatched

    def _find_scenario_match(
        self, bug: Bug, filter_result: FilterResult, metrics: RunMetrics,
    ) -> tuple[ScenarioMatch, 'Observation']:
        summary_parts = [bug.summary]
        if filter_result.failure_mode:
            summary_parts.append(filter_result.failure_mode)
        if filter_result.injection_method:
            summary_parts.append(filter_result.injection_method)
        summary = " ".join(summary_parts)

        components = bug.all_components or (bug.component,)
        scenario_hits = self.chroma.search_per_component(
            components, summary, collection="scenarios", n_results=5,
        )
        doc_hits = self.chroma.search_per_component(
            components, summary, collection="krkn_docs", n_results=5,
        )

        kb_context = self._lookup_knowledgebase(bug, filter_result)

        if self.use_llm:
            ctx = MapContext(
                scenario_hits=tuple(scenario_hits),
                doc_hits=tuple(doc_hits),
                kb_context=kb_context,
            )
            match, obs = map_match_llm(bug, filter_result, ctx)
            metrics.llm_map_calls += 1
            if obs.status == "error":
                metrics.map_fallbacks += 1
            return match, obs

        from src.reasoning import _fallback_match
        from src.models import Observation
        match = _fallback_match(bug, scenario_hits)
        obs = Observation(
            status="success",
            summary=f"{bug.key}: distance-based match={match.match_result.value}",
            next_actions=("proceed_to_analyze",) if match.match_result != MatchResult.FULL_MATCH else ("skip_full_match",),
        )
        return match, obs

    def _lookup_knowledgebase(self, bug: Bug, filter_result: FilterResult) -> dict | None:
        try:
            from src.knowledge.scenario_knowledgebase import ScenarioKnowledgeBase
            from src.generator.scenario_generator import match_scenario

            kb = ScenarioKnowledgeBase()
            temp_gap = GapAnalysis(
                bug=bug, reasoning=filter_result.injection_method or "",
                modifications=[filter_result.failure_mode or ""],
            )
            matched_kb = match_scenario(temp_gap, kb)
            if matched_kb:
                return {
                    "scenario_name": matched_kb.get("scenario_name"),
                    "title": matched_kb.get("title"),
                    "description": matched_kb.get("description", "")[:200],
                    "parameters": [p.get("name") for p in matched_kb.get("parameters", [])],
                }
        except Exception as e:
            logger.debug("Knowledge base lookup in MAP: %s", e)
        return None

    # ── ANALYZE ───────────────────────────────────────────────────

    def _analyze(
        self, unmatched: list[ScenarioMatch], metrics: RunMetrics,
    ) -> list[GapAnalysis]:
        gaps = []

        for match in unmatched:
            bug = match.bug

            if self.use_llm:
                components = bug.all_components or (bug.component,)
                ocp_docs = self.chroma.search_per_component(
                    components, bug.summary, collection="all", n_results=5,
                )
                krkn_docs = self.chroma.search_per_component(
                    components, bug.summary, collection="krkn_docs", n_results=3,
                )
                neo4j_history = []
                try:
                    for comp in components:
                        neo4j_history.extend(self.neo4j.get_similar_resolved_bugs(comp))
                except Exception as e:
                    logger.warning("Neo4j history lookup failed: %s", e)

                ctx = AnalyzeContext(
                    ocp_docs=tuple(ocp_docs),
                    krkn_docs=tuple(krkn_docs),
                    neo4j_history=tuple(neo4j_history),
                )
                gap, obs = analyze_gap_llm(bug, match, ctx)
                metrics.llm_analyze_calls += 1
                if obs.status == "error":
                    metrics.analyze_retries += 1
            else:
                from src.reasoning import _fallback_analyze
                gap = _fallback_analyze(bug, match)
                obs = Observation(
                    status="success",
                    summary=f"{bug.key}: keyword-based analysis, confidence={gap.confidence_score}",
                )

            self._slog.log_phase("analyze", obs.status, obs.summary,
                                 bug_key=bug.key, confidence=gap.confidence_score)
            gaps.append(gap)

        return gaps

    # ── REMEMBER ──────────────────────────────────────────────────

    def _remember(self, result: AgentResult) -> None:
        self.neo4j.remember_result_sync(result)
        logger.info("REMEMBER: stored in Neo4j")

    def _store_metrics(self, metrics: RunMetrics) -> None:
        metrics_dict = {**asdict(metrics), "agent": self.agent_name}
        try:
            self.neo4j.store_run_metrics(metrics_dict)
            logger.info(
                "METRICS: filter=%d kw/%d cache/%d llm, map=%d, analyze=%d, tokens=%d",
                metrics.keyword_filter_hits, metrics.semantic_cache_hits,
                metrics.llm_filter_calls, metrics.llm_map_calls,
                metrics.llm_analyze_calls,
                self._slog.total_tokens(),
            )
        except Exception as e:
            logger.warning("Failed to store RunMetrics: %s", e)
