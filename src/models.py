"""Domain models for krkn-chaos-coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class ChaosRelevance(Enum):
    RELEVANT = "relevant"
    NOT_RELEVANT = "not_relevant"
    UNCERTAIN = "uncertain"


class Confidence(Enum):
    HIGH = "high"      # 70-100: draft PR
    MEDIUM = "medium"  # 40-69: GitHub issue with recommendation
    LOW = "low"        # 0-39: GitHub issue describing gap


class FactorConfidence(Enum):
    """Per-category score contribution: HIGH = points awarded, LOW = none."""
    HIGH = "high"
    LOW = "low"


class ActionType(Enum):
    DRAFT_PR = "draft_pr"
    GITHUB_ISSUE = "github_issue"
    SKIP = "skip"


class MatchResult(Enum):
    FULL_MATCH = "full_match"
    PARTIAL_MATCH = "partial_match"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class Bug:
    key: str
    summary: str
    description: str
    component: str  # All components joined with comma
    priority: str
    status: str
    created: str
    url: str
    all_components: tuple[str, ...] = ()  # Tuple for iteration
    fixed_in_release: str | None = None  # e.g. "4.21.6" if shipped in a z-stream
    fix_commits: tuple[str, ...] = ()    # Commit messages that fixed this bug
    fix_image: str | None = None         # Image that was updated (e.g. "machine-config-operator")


@dataclass(frozen=True)
class Regression:
    regression_id: int
    test_name: str
    component: str
    opened: str
    closed: str | None
    triaged: bool


@dataclass(frozen=True)
class FilterResult:
    bug: Bug
    chaos_relevant: bool
    failure_mode: str | None = None
    injection_method: str | None = None
    skip_reason: str | None = None
    confidence: float = 0.5  # 0.0-1.0, keyword filter certainty


@dataclass(frozen=True)
class ScenarioMatch:
    bug: Bug
    match_result: MatchResult
    matched_scenario: str | None = None
    matched_repo: str | None = None
    similarity_score: float = 0.0
    filter_injection_method: str | None = None  # from FILTER; free-text hint for ACT
    # MAP search artifacts forwarded to ANALYZE (avoid discarding paid Chroma/KB work)
    map_scenario_hits: tuple[dict, ...] = ()
    map_doc_hits: tuple[dict, ...] = ()
    kb_context: dict | None = None


@dataclass(frozen=True)
class GapAnalysis:
    bug: Bug
    reuse_plan: str | None = None
    confidence_score: int = 0
    confidence_level: Confidence = Confidence.LOW
    action_type: ActionType = ActionType.GITHUB_ISSUE
    reasoning: str = ""
    base_scenario: str | None = None
    krkn_plugin: str | None = None  # from ANALYZE; plugin dir or scenario_plugins path
    filter_injection_method: str | None = None  # from FILTER when MAP/ANALYZE lack plugin
    modifications: list[str] = field(default_factory=list)
    # Per-category confidence (HIGH = that scoring guide item awarded points)
    reproduction_confidence: FactorConfidence = FactorConfidence.LOW
    scenario_confidence: FactorConfidence = FactorConfidence.LOW
    understanding_confidence: FactorConfidence = FactorConfidence.LOW
    plugin_confidence: FactorConfidence = FactorConfidence.LOW
    domain_confidence: FactorConfidence = FactorConfidence.LOW
    history_confidence: FactorConfidence = FactorConfidence.LOW
    # Optional per-factor notes: ((field_name, reason), ...)
    confidence_factor_reasons: tuple[tuple[str, str], ...] = ()


# Display labels for issue bodies / UI (field name → human label)
CONFIDENCE_FACTOR_LABELS: tuple[tuple[str, str], ...] = (
    ("reproduction_confidence", "Reproduction Confidence"),
    ("scenario_confidence", "Extendable Scenario"),
    ("understanding_confidence", "Understanding Confidence"),
    ("plugin_confidence", "Injection Capability"),
    ("domain_confidence", "Domain Confidence"),
    ("history_confidence", "History Confidence"),
)

# Default LOW explanations when no specific reason was recorded
CONFIDENCE_FACTOR_LOW_DEFAULTS: dict[str, str] = {
    "reproduction_confidence": "Reproduction steps not clear enough (+0)",
    "scenario_confidence": "No existing scenario to extend (+0)",
    "understanding_confidence": "Failure mechanism not clear from docs (+0)",
    "plugin_confidence": "No matching krkn plugin identified (+0)",
    "domain_confidence": "Does not clearly match the agent domain (+0)",
    "history_confidence": "No similar resolved bug found (+0)",
}


@dataclass(frozen=True)
class AgentResult:
    agent_name: str
    bugs_discovered: list[Bug] = field(default_factory=list)
    bugs_filtered_out: list[FilterResult] = field(default_factory=list)
    bugs_matched: list[ScenarioMatch] = field(default_factory=list)
    gaps: list[GapAnalysis] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Observation:
    """Structured result from a tool call — status, summary, and next actions."""
    status: str            # "success" | "warning" | "error"
    summary: str           # One-line human-readable result
    next_actions: tuple[str, ...] = ()  # What the pipeline should do next
    artifacts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FilterContext:
    """Bundled inputs for the FILTER LLM call."""
    ocp_docs: tuple[dict, ...] = ()
    krkn_docs: tuple[dict, ...] = ()


@dataclass(frozen=True)
class MapContext:
    """Bundled inputs for the MAP LLM call."""
    scenario_hits: tuple[dict, ...] = ()
    doc_hits: tuple[dict, ...] = ()
    kb_context: dict | None = None


@dataclass(frozen=True)
class AnalyzeContext:
    """Bundled inputs for the ANALYZE LLM call."""
    ocp_docs: tuple[dict, ...] = ()
    krkn_docs: tuple[dict, ...] = ()
    neo4j_history: tuple[dict, ...] = ()
    scenario_hits: tuple[dict, ...] = ()  # MAP hits + dedicated scenarios search
    kb_context: dict | None = None  # forwarded from MAP knowledgebase lookup
    matched_scenario_yaml: str | None = None  # raw YAML for match.matched_scenario


@dataclass
class RunMetrics:
    """Per-run metrics for harness quality tracking."""
    bugs_processed: int = 0
    bugs_succeeded: int = 0
    filter_retries: int = 0
    filter_escalations: int = 0
    map_fallbacks: int = 0
    analyze_retries: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    keyword_filter_hits: int = 0
    semantic_cache_hits: int = 0
    llm_filter_calls: int = 0
    llm_map_calls: int = 0
    llm_analyze_calls: int = 0
    filter_duration_sec: float = 0.0
    map_duration_sec: float = 0.0
    analyze_duration_sec: float = 0.0


class MemoryRepository(Protocol):
    """Protocol for memory backends (Neo4j, in-memory, etc.)."""

    def connect(self) -> bool: ...
    def remember_result(self, result: AgentResult) -> dict: ...
    def get_analyzed_bug_keys(self) -> set[str]: ...
    def is_bug_analyzed(self, bug_key: str) -> bool: ...
    def update_bug_statuses(self, bugs: list) -> dict: ...
    def get_open_gaps(self) -> list[dict]: ...
    def get_similar_resolved_bugs(self, component: str) -> list[dict]: ...
    def mark_gap_resolved(self, bug_key: str, issue_url: str) -> None: ...
    def get_run_history(self, limit: int = 20) -> list[dict]: ...
    def store_run_metrics(self, metrics: dict) -> None: ...
    def close(self) -> None: ...
