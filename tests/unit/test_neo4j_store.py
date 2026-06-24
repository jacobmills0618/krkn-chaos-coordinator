"""Tests for MemoryRepository protocol and Neo4j store compliance.

Uses a FakeMemoryRepository backed by in-memory dicts to verify
protocol semantics without requiring a running Neo4j instance.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import (
    AgentResult,
    Bug,
    Confidence,
    ActionType,
    FilterResult,
    GapAnalysis,
    MatchResult,
    MemoryRepository,
    ScenarioMatch,
)


class FakeMemoryRepository:
    """In-memory implementation of MemoryRepository for testing."""

    def __init__(self) -> None:
        self._bugs: dict[str, dict] = {}
        self._gaps: dict[str, dict] = {}
        self._runs: list[dict] = []
        self._data: dict[str, list] = {}
        self._connected: bool = False

    def connect(self) -> bool:
        self._connected = True
        return True

    def remember_result(self, result: AgentResult) -> dict:
        timestamp = datetime.now(timezone.utc).isoformat()
        new_bugs = 0
        new_gaps = 0

        for bug in result.bugs_discovered:
            if bug.key not in self._bugs:
                self._bugs[bug.key] = {
                    "summary": bug.summary,
                    "component": bug.component,
                    "priority": bug.priority,
                    "status": bug.status,
                    "analyzed_at": timestamp,
                    "agent": result.agent_name,
                }
                new_bugs += 1

        for gap in result.gaps:
            gap_id = f"{gap.bug.key}_{result.agent_name}"
            if gap_id not in self._gaps:
                self._gaps[gap_id] = {
                    "bug_key": gap.bug.key,
                    "confidence": gap.confidence_score,
                    "action_type": gap.action_type.value,
                    "reasoning": gap.reasoning,
                    "base_scenario": gap.base_scenario,
                    "status": "open",
                    "agent": result.agent_name,
                    "opened_at": timestamp,
                }
                new_gaps += 1

        self._runs.append({
            "agent": result.agent_name,
            "timestamp": timestamp,
            "bugs_discovered": len(result.bugs_discovered),
            "gaps_found": len(result.gaps),
        })

        return {"new_bugs": new_bugs, "new_gaps": new_gaps}

    def get_analyzed_bug_keys(self) -> set[str]:
        return set(self._bugs.keys())

    def is_bug_analyzed(self, bug_key: str) -> bool:
        return bug_key in self._bugs

    def update_bug_statuses(self, bugs: list) -> dict:
        updated = 0
        for bug in bugs:
            if bug.key in self._bugs:
                self._bugs[bug.key]["status"] = bug.status
                self._bugs[bug.key]["priority"] = bug.priority
                updated += 1
        return {"updated": updated, "gaps_closed": 0}

    def get_open_gaps(self) -> list[dict]:
        return [g for g in self._gaps.values() if g.get("status") == "open"]

    def get_similar_resolved_bugs(self, component: str) -> list[dict]:
        return [
            {"bug_key": g["bug_key"], "reasoning": g["reasoning"]}
            for g in self._gaps.values()
            if g.get("status") == "resolved"
        ]

    def mark_gap_resolved(self, bug_key: str, issue_url: str) -> None:
        for gap in self._gaps.values():
            if gap["bug_key"] == bug_key and gap["status"] == "open":
                gap["status"] = "resolved"
                gap["resolved_at"] = datetime.now(timezone.utc).isoformat()
                gap["issue_url"] = issue_url

    def get_run_history(self, limit: int = 20) -> list[dict]:
        return self._runs[-limit:]

    def store_run_metrics(self, metrics: dict) -> None:
        if "run_metrics" not in self._data:
            self._data["run_metrics"] = []
        self._data["run_metrics"] = [*self._data["run_metrics"], metrics]

    def close(self) -> None:
        self._connected = False


def _make_bug(key: str = "OCPBUGS-100", component: str = "Etcd") -> Bug:
    return Bug(
        key=key,
        summary=f"Test bug {key}",
        description="Some description",
        component=component,
        priority="Critical",
        status="New",
        created="2026-01-01",
        url=f"https://issues.redhat.com/browse/{key}",
    )


def _make_gap(bug: Bug, score: int = 55) -> GapAnalysis:
    return GapAnalysis(
        bug=bug,
        confidence_score=score,
        confidence_level=Confidence.MEDIUM,
        action_type=ActionType.GITHUB_ISSUE,
        reasoning="Test gap reasoning",
        base_scenario="scenarios/etcd/etcd_kill.yml",
    )


def _make_result(
    bugs: list[Bug] | None = None,
    gaps: list[GapAnalysis] | None = None,
) -> AgentResult:
    if bugs is None:
        bugs = [_make_bug()]
    if gaps is None:
        gaps = []
    return AgentResult(
        agent_name="test_agent",
        bugs_discovered=bugs,
        gaps=gaps,
    )


class TestFakeRepoRememberResultStoresBugs:
    """Verify remember_result stores bugs and returns correct counts."""

    def test_stores_single_bug(self) -> None:
        repo = FakeMemoryRepository()
        bug = _make_bug("OCPBUGS-1")
        result = _make_result(bugs=[bug])

        summary = repo.remember_result(result)

        assert summary["new_bugs"] == 1
        assert "OCPBUGS-1" in repo._bugs

    def test_stores_multiple_bugs(self) -> None:
        repo = FakeMemoryRepository()
        bugs = [_make_bug(f"OCPBUGS-{i}") for i in range(5)]
        result = _make_result(bugs=bugs)

        summary = repo.remember_result(result)

        assert summary["new_bugs"] == 5
        assert len(repo._bugs) == 5

    def test_stores_bugs_with_gaps(self) -> None:
        repo = FakeMemoryRepository()
        bug = _make_bug("OCPBUGS-10")
        gap = _make_gap(bug, score=75)
        result = _make_result(bugs=[bug], gaps=[gap])

        summary = repo.remember_result(result)

        assert summary["new_bugs"] == 1
        assert summary["new_gaps"] == 1
        assert len(repo._gaps) == 1

    def test_records_run_history(self) -> None:
        repo = FakeMemoryRepository()
        result = _make_result()

        repo.remember_result(result)

        history = repo.get_run_history()
        assert len(history) == 1
        assert history[0]["agent"] == "test_agent"


class TestFakeRepoGetAnalyzedBugKeys:
    """Verify get_analyzed_bug_keys returns all stored bug keys."""

    def test_empty_repo_returns_empty_set(self) -> None:
        repo = FakeMemoryRepository()
        assert repo.get_analyzed_bug_keys() == set()

    def test_returns_stored_keys(self) -> None:
        repo = FakeMemoryRepository()
        bugs = [_make_bug("OCPBUGS-1"), _make_bug("OCPBUGS-2")]
        repo.remember_result(_make_result(bugs=bugs))

        keys = repo.get_analyzed_bug_keys()

        assert keys == {"OCPBUGS-1", "OCPBUGS-2"}

    def test_is_bug_analyzed_returns_true_for_stored(self) -> None:
        repo = FakeMemoryRepository()
        repo.remember_result(_make_result(bugs=[_make_bug("OCPBUGS-99")]))

        assert repo.is_bug_analyzed("OCPBUGS-99") is True

    def test_is_bug_analyzed_returns_false_for_unknown(self) -> None:
        repo = FakeMemoryRepository()

        assert repo.is_bug_analyzed("OCPBUGS-999") is False


class TestFakeRepoSkipAlreadyAnalyzed:
    """Verify that re-analyzing the same bug does not create duplicates."""

    def test_duplicate_bug_not_counted_as_new(self) -> None:
        repo = FakeMemoryRepository()
        bug = _make_bug("OCPBUGS-50")

        repo.remember_result(_make_result(bugs=[bug]))
        summary = repo.remember_result(_make_result(bugs=[bug]))

        assert summary["new_bugs"] == 0
        assert len(repo._bugs) == 1

    def test_duplicate_gap_not_counted_as_new(self) -> None:
        repo = FakeMemoryRepository()
        bug = _make_bug("OCPBUGS-50")
        gap = _make_gap(bug)

        repo.remember_result(_make_result(bugs=[bug], gaps=[gap]))
        summary = repo.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        assert summary["new_gaps"] == 0
        assert len(repo._gaps) == 1


class TestFakeRepoMarkGapResolved:
    """Verify mark_gap_resolved updates gap status."""

    def test_resolves_open_gap(self) -> None:
        repo = FakeMemoryRepository()
        bug = _make_bug("OCPBUGS-200")
        gap = _make_gap(bug)
        repo.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        repo.mark_gap_resolved("OCPBUGS-200", "https://github.com/org/repo/issues/1")

        open_gaps = repo.get_open_gaps()
        assert len(open_gaps) == 0

    def test_resolved_gap_has_issue_url(self) -> None:
        repo = FakeMemoryRepository()
        bug = _make_bug("OCPBUGS-200")
        gap = _make_gap(bug)
        repo.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        repo.mark_gap_resolved("OCPBUGS-200", "https://github.com/org/repo/issues/1")

        gap_data = list(repo._gaps.values())[0]
        assert gap_data["status"] == "resolved"
        assert gap_data["issue_url"] == "https://github.com/org/repo/issues/1"

    def test_noop_for_unknown_bug(self) -> None:
        repo = FakeMemoryRepository()
        bug = _make_bug("OCPBUGS-200")
        gap = _make_gap(bug)
        repo.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        repo.mark_gap_resolved("OCPBUGS-UNKNOWN", "https://example.com")

        assert len(repo.get_open_gaps()) == 1


class TestProtocolCompliance:
    """Verify that Neo4jStore has all methods required by MemoryRepository."""

    @pytest.mark.skipif(
        not pytest.importorskip("neo4j", reason="neo4j driver not installed"),
        reason="neo4j driver not installed",
    )
    def test_neo4j_store_has_all_protocol_methods(self) -> None:
        from src.knowledge.neo4j_store import Neo4jStore

        required_methods = [
            "connect",
            "remember_result",
            "get_analyzed_bug_keys",
            "is_bug_analyzed",
            "update_bug_statuses",
            "get_open_gaps",
            "get_similar_resolved_bugs",
            "mark_gap_resolved",
            "get_run_history",
            "store_run_metrics",
            "get_metrics_history",
            "close",
        ]
        for method_name in required_methods:
            assert hasattr(Neo4jStore, method_name), (
                f"Neo4jStore missing protocol method: {method_name}"
            )
            assert callable(getattr(Neo4jStore, method_name)), (
                f"Neo4jStore.{method_name} is not callable"
            )

    def test_fake_repo_is_protocol_compliant(self) -> None:
        """FakeMemoryRepository should be structurally compatible with MemoryRepository."""
        required_methods = [
            "connect",
            "remember_result",
            "get_analyzed_bug_keys",
            "is_bug_analyzed",
            "update_bug_statuses",
            "get_open_gaps",
            "get_similar_resolved_bugs",
            "mark_gap_resolved",
            "get_run_history",
            "store_run_metrics",
            "close",
        ]
        for method_name in required_methods:
            assert hasattr(FakeMemoryRepository, method_name), (
                f"FakeMemoryRepository missing protocol method: {method_name}"
            )
            assert callable(getattr(FakeMemoryRepository, method_name)), (
                f"FakeMemoryRepository.{method_name} is not callable"
            )


class TestFakeRepoStoreRunMetrics:
    """Verify store_run_metrics stores and retrieves metrics."""

    def test_fake_repo_store_run_metrics(self) -> None:
        repo = FakeMemoryRepository()
        metrics = {
            "agent": "control_plane",
            "bugs_processed": 10,
            "bugs_succeeded": 8,
            "filter_retries": 1,
            "filter_escalations": 0,
            "total_input_tokens": 5000,
            "total_output_tokens": 1200,
        }

        repo.store_run_metrics(metrics)

        stored = repo._data["run_metrics"]
        assert len(stored) == 1
        assert stored[0]["agent"] == "control_plane"
        assert stored[0]["bugs_processed"] == 10
        assert stored[0]["bugs_succeeded"] == 8
        assert stored[0]["total_input_tokens"] == 5000

    def test_fake_repo_store_multiple_metrics(self) -> None:
        repo = FakeMemoryRepository()
        metrics_run1 = {
            "agent": "control_plane",
            "bugs_processed": 5,
            "bugs_succeeded": 3,
        }
        metrics_run2 = {
            "agent": "control_plane",
            "bugs_processed": 12,
            "bugs_succeeded": 10,
        }

        repo.store_run_metrics(metrics_run1)
        repo.store_run_metrics(metrics_run2)

        stored = repo._data["run_metrics"]
        assert len(stored) == 2
        assert stored[0]["bugs_processed"] == 5
        assert stored[1]["bugs_processed"] == 12


class TestNeo4jStoreEnvLoading:
    def test_loads_password_from_env_via_project_dotenv(self, monkeypatch) -> None:
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

        def _fake_load() -> None:
            monkeypatch.setenv("NEO4J_PASSWORD", "from-dotenv")

        monkeypatch.setattr(
            "src.knowledge.neo4j_store.load_project_env",
            _fake_load,
        )

        from src.knowledge.neo4j_store import Neo4jStore

        store = Neo4jStore()
        assert store._password == "from-dotenv"

    def test_explicit_password_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("NEO4J_PASSWORD", "from-env")
        from src.knowledge.neo4j_store import Neo4jStore

        store = Neo4jStore(password="explicit")
        assert store._password == "explicit"
