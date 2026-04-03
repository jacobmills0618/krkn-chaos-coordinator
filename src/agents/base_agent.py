"""Base domain agent with the DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER pipeline."""

import logging
from abc import ABC

from src.apis.jira_client import JiraClient
from src.apis.sippy_client import SippyClient
from src.apis.github_client import GitHubClient
from src.filter.chaos_filter import filter_bugs
from src.filter.llm_filter import llm_filter_bugs
from src.knowledge.chromadb_store import ChromaStore
from src.knowledge.component_map import get_components_for_agent
from src.knowledge.memory import MemoryStore
from src.knowledge.neo4j_store import Neo4jStore
from src.knowledge.scenario_index import ScenarioInfo
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

logger = logging.getLogger(__name__)

# Threshold: below this ChromaDB distance = strong semantic match
FULL_MATCH_THRESHOLD = 0.35
PARTIAL_MATCH_THRESHOLD = 0.65


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
        memory: MemoryStore | None = None,
        neo4j_store: Neo4jStore | None = None,
        use_llm_filter: bool = False,
    ):
        self.agent_name = agent_name
        self.jira = jira
        self.sippy = sippy
        self.github = github
        self.chroma = chroma
        self.scenarios = scenarios
        self.release = release
        self.memory = memory or MemoryStore()
        self.neo4j = neo4j_store
        self.use_llm_filter = use_llm_filter
        self.components = get_components_for_agent(agent_name)

    def run(self) -> AgentResult:
        """Execute the full pipeline: DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER."""
        import asyncio
        logger.info("=== %s agent starting ===", self.agent_name)

        # DISCOVER
        bugs = self._discover()
        logger.info("DISCOVER: found %d bugs", len(bugs))

        # Skip already-analyzed bugs (Neo4j first, JSON fallback)
        known_keys = self._get_known_bugs()
        new_bugs = [b for b in bugs if b.key not in known_keys]
        if len(bugs) != len(new_bugs):
            logger.info("REMEMBER: skipping %d already-analyzed bugs", len(bugs) - len(new_bugs))

        # FILTER
        relevant, skipped = self._filter(new_bugs)
        logger.info("FILTER: %d relevant, %d skipped", len(relevant), len(skipped))

        # MAP
        matched, unmatched = self._map(relevant)
        logger.info("MAP: %d matched, %d unmatched", len(matched), len(unmatched))

        # ANALYZE
        gaps = self._analyze(unmatched)
        logger.info("ANALYZE: %d gaps identified", len(gaps))

        result = AgentResult(
            agent_name=self.agent_name,
            bugs_discovered=bugs,
            bugs_filtered_out=skipped,
            bugs_matched=matched,
            gaps=gaps,
        )

        # REMEMBER (Neo4j + JSON fallback)
        self._remember(result)

        logger.info("=== %s agent complete ===", self.agent_name)
        return result

    def _get_known_bugs(self) -> set[str]:
        """Get already-analyzed bug keys from Neo4j or JSON memory."""
        if self.neo4j:
            try:
                return self.neo4j.get_analyzed_bug_keys_sync()
            except Exception as e:
                logger.warning("Neo4j lookup failed, using JSON fallback: %s", e)
        return self.memory.get_analyzed_bug_keys()

    def _remember(self, result: AgentResult) -> None:
        """Store results in Neo4j and JSON memory."""
        # Always store in JSON (fast, reliable fallback)
        self.memory.remember_result(result)

        # Also store in Neo4j if available
        if self.neo4j:
            try:
                self.neo4j.remember_result_sync(result)
                logger.info("REMEMBER: stored in Neo4j")
            except Exception as e:
                logger.warning("Neo4j remember failed (JSON fallback used): %s", e)

    def _discover(self) -> list[Bug]:
        """DISCOVER: Query JIRA and Sippy for bugs and regressions."""
        return self.jira.get_bugs_by_components(self.components)

    def _filter(self, bugs: list[Bug]) -> tuple[list[FilterResult], list[FilterResult]]:
        """FILTER: Determine chaos relevance of each bug."""
        if self.use_llm_filter:
            logger.info("Using LLM-enhanced filter (Ollama)")
            return llm_filter_bugs(bugs)
        return filter_bugs(bugs)

    def _map(self, relevant: list[FilterResult]) -> tuple[list[ScenarioMatch], list[ScenarioMatch]]:
        """MAP: Match bugs against existing krkn scenarios using ChromaDB + local index."""
        matched = []
        unmatched = []

        for filter_result in relevant:
            bug = filter_result.bug
            match = self._find_scenario_match(bug, filter_result)
            if match.match_result == MatchResult.FULL_MATCH:
                matched.append(match)
            else:
                unmatched.append(match)

        return matched, unmatched

    def _find_scenario_match(self, bug: Bug, filter_result: FilterResult) -> ScenarioMatch:
        """Search for existing krkn scenarios that match a bug.

        Uses three search strategies:
        1. ChromaDB semantic search on scenario_docs (scenario YAMLs + plugin code)
        2. ChromaDB semantic search on krkn_docs (website + krkn-hub docs)
        3. Local scenario index keyword matching
        """
        # Build a rich query from the bug + filter result
        query_parts = [bug.component, bug.summary]
        if filter_result.failure_mode:
            query_parts.append(filter_result.failure_mode)
        if filter_result.injection_method:
            query_parts.append(filter_result.injection_method)
        query = " ".join(query_parts)

        # Strategy 1: Search scenario configs and plugin code
        scenario_hits = self.chroma.search_scenarios(query, n_results=5)

        # Strategy 2: Search krkn documentation
        doc_hits = self.chroma.search_krkn_docs(query, n_results=5)

        # Strategy 3: Local scenario index keyword matching
        component_lower = bug.component.lower()
        summary_words = set(bug.summary.lower().split())
        matching_scenarios = [
            s for s in self.scenarios
            if component_lower in s.name.lower()
            or component_lower in s.scenario_type.lower()
            or any(kw in s.file_path.lower() for kw in component_lower.split())
            or any(word in s.name.lower() for word in summary_words if len(word) > 4)
        ]

        # Determine best match from all strategies
        best_scenario_dist = scenario_hits[0].get("distance", 1.0) if scenario_hits else 1.0
        best_doc_dist = doc_hits[0].get("distance", 1.0) if doc_hits else 1.0
        best_dist = min(best_scenario_dist, best_doc_dist)

        # Extract the best scenario file path from hits
        best_scenario_path = None
        if matching_scenarios:
            best_scenario_path = matching_scenarios[0].file_path
        elif scenario_hits:
            # Try to extract path from ChromaDB result text
            text = scenario_hits[0].get("text", "")
            if "Scenario file:" in text:
                line = text.split("Scenario file:")[1].split("\n")[0].strip()
                best_scenario_path = line

        # Determine match level
        if best_dist < FULL_MATCH_THRESHOLD and best_scenario_path:
            return ScenarioMatch(
                bug=bug,
                match_result=MatchResult.FULL_MATCH,
                matched_scenario=best_scenario_path,
                matched_repo="krkn-chaos/krkn",
                similarity_score=1.0 - best_dist,
            )

        if best_dist < PARTIAL_MATCH_THRESHOLD or matching_scenarios:
            scenario_path = best_scenario_path or (
                matching_scenarios[0].file_path if matching_scenarios else None
            )
            return ScenarioMatch(
                bug=bug,
                match_result=MatchResult.PARTIAL_MATCH,
                matched_scenario=scenario_path,
                matched_repo="krkn-chaos/krkn",
                similarity_score=1.0 - best_dist,
            )

        return ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)

    def _analyze(self, unmatched: list[ScenarioMatch]) -> list[GapAnalysis]:
        """ANALYZE: Score confidence and determine action for each gap.

        Uses OCP docs from ChromaDB to enrich the analysis.
        """
        gaps = []

        for match in unmatched:
            bug = match.bug
            score = 0
            reasoning_parts = []

            # Clear repro steps? (+20)
            if bug.description and len(bug.description) > 200:
                score += 20
                reasoning_parts.append("Clear repro steps (+20)")

            # Existing scenario to extend? (+25)
            if match.match_result == MatchResult.PARTIAL_MATCH:
                score += 25
                reasoning_parts.append(f"Partial match: {match.matched_scenario} (+25)")

            # Known failure mode? (+20)
            failure_keywords = [
                "timeout", "crash", "unavailable", "degraded", "unhealthy",
                "not cleared", "failure", "failed", "outage", "disruption",
                "quorum", "leader election", "not ready", "eviction",
            ]
            if any(kw in bug.summary.lower() for kw in failure_keywords):
                score += 20
                reasoning_parts.append("Known failure mode (+20)")

            # Similar patterns in krkn docs? (+15)
            doc_query = f"{bug.component} {bug.summary}"
            doc_hits = self.chroma.search_krkn_docs(doc_query, n_results=1)
            if doc_hits and doc_hits[0].get("distance", 1.0) < 0.5:
                score += 15
                reasoning_parts.append("Similar pattern in krkn docs (+15)")

            # Agent domain match? (+10)
            if bug.component.lower() in " ".join(self.components).lower():
                score += 10
                reasoning_parts.append("Domain match (+10)")

            # Determine confidence level and action
            if score >= 70:
                confidence = Confidence.HIGH
                action = ActionType.DRAFT_PR
            elif score >= 40:
                confidence = Confidence.MEDIUM
                action = ActionType.GITHUB_ISSUE
            else:
                confidence = Confidence.LOW
                action = ActionType.GITHUB_ISSUE

            modifications = []
            if match.match_result == MatchResult.PARTIAL_MATCH and match.matched_scenario:
                modifications.append(f"Extend {match.matched_scenario}")

            gaps.append(
                GapAnalysis(
                    bug=bug,
                    confidence_score=score,
                    confidence_level=confidence,
                    action_type=action,
                    reasoning="; ".join(reasoning_parts),
                    base_scenario=match.matched_scenario,
                    modifications=modifications,
                )
            )

        return gaps
