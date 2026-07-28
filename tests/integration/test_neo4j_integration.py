"""Integration tests for Neo4jStore against a live Neo4j instance.

Requires: Neo4j running on bolt://localhost:7687 with NEO4J_AUTH=neo4j/password
Start with: podman start neo4j-coordinator

Tests use a unique prefix per run so they don't collide with production data.
Cleanup happens in teardown.
"""

from __future__ import annotations

import os
import uuid

import pytest

neo4j_mod = pytest.importorskip("neo4j", reason="neo4j driver not installed")

from src.knowledge.neo4j_store import Neo4jStore
from src.models import (
    ActionType,
    AgentResult,
    Bug,
    Confidence,
    FilterResult,
    GapAnalysis,
    ScenarioMatch,
    MatchResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_PREFIX = f"TEST-{uuid.uuid4().hex[:8]}"


def _neo4j_reachable() -> bool:
    """Check if Neo4j is accepting connections."""
    try:
        store = Neo4jStore(password=os.environ.get("NEO4J_PASSWORD", "password"))
        ok = store.connect()
        store.close()
        return ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _neo4j_reachable(),
    reason="Neo4j not reachable at bolt://localhost:7687",
)


@pytest.fixture()
def store():
    """Provide a connected Neo4jStore and clean up test data after."""
    s = Neo4jStore(password=os.environ.get("NEO4J_PASSWORD", "password"))
    assert s.connect()
    yield s
    _cleanup(s)
    s.close()


def _cleanup(store: Neo4jStore) -> None:
    """Remove all test nodes created by this run."""
    with store._driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.key STARTS WITH $prefix DETACH DELETE n",
            prefix=_TEST_PREFIX,
        )
        session.run(
            "MATCH (n:Run) WHERE n.agent = $agent DETACH DELETE n",
            agent=f"{_TEST_PREFIX}_agent",
        )
        session.run(
            "MATCH (n:Agent) WHERE n.name = $agent DETACH DELETE n",
            agent=f"{_TEST_PREFIX}_agent",
        )
        session.run(
            "MATCH (n:RunMetrics) WHERE n.created_at STARTS WITH '9999' DETACH DELETE n",
        )


def _make_bug(seq: int = 1, component: str = "Etcd", status: str = "New") -> Bug:
    return Bug(
        key=f"{_TEST_PREFIX}-{seq}",
        summary=f"Integration test bug {seq}",
        description=f"Description for test bug {seq}",
        component=component,
        priority="Critical",
        status=status,
        created="2026-05-27",
        url=f"https://issues.redhat.com/browse/{_TEST_PREFIX}-{seq}",
    )


def _make_gap(bug: Bug, score: int = 55) -> GapAnalysis:
    level = Confidence.HIGH if score >= 70 else Confidence.MEDIUM if score >= 40 else Confidence.LOW
    action = ActionType.DRAFT_PR if score >= 70 else ActionType.GITHUB_ISSUE
    return GapAnalysis(
        bug=bug,
        confidence_score=score,
        confidence_level=level,
        action_type=action,
        reasoning="Integration test gap reasoning",
        base_scenario="scenarios/test/test.yml",
        modifications=["Step 1", "Step 2"],
    )


def _make_result(
    bugs: list[Bug] | None = None,
    gaps: list[GapAnalysis] | None = None,
    filtered_out: list[FilterResult] | None = None,
    passed: list[FilterResult] | None = None,
    filter_mode: str = "chaos",
) -> AgentResult:
    if bugs is None:
        bugs = [_make_bug()]
    return AgentResult(
        agent_name=f"{_TEST_PREFIX}_agent",
        bugs_discovered=bugs,
        bugs_passed_filter=passed or [],
        bugs_filtered_out=filtered_out or [],
        gaps=gaps or [],
        filter_mode=filter_mode,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRememberAndQuery:
    """Full remember → query cycle against live Neo4j."""

    def test_remember_stores_bugs_and_gaps(self, store: Neo4jStore) -> None:
        bug = _make_bug(1)
        gap = _make_gap(bug, score=75)
        result = _make_result(bugs=[bug], gaps=[gap])

        summary = store.remember_result(result)

        assert summary["new_bugs"] == 1
        assert summary["new_gaps"] == 1

    def test_get_analyzed_keys_returns_stored(self, store: Neo4jStore) -> None:
        bug = _make_bug(2)
        store.remember_result(_make_result(bugs=[bug]))

        keys = store.get_analyzed_bug_keys()

        assert f"{_TEST_PREFIX}-2" in keys

    def test_is_bug_analyzed(self, store: Neo4jStore) -> None:
        bug = _make_bug(3)
        store.remember_result(_make_result(bugs=[bug]))

        assert store.is_bug_analyzed(f"{_TEST_PREFIX}-3") is True
        assert store.is_bug_analyzed(f"{_TEST_PREFIX}-NONEXISTENT") is False

    def test_duplicate_bug_not_double_counted(self, store: Neo4jStore) -> None:
        bug = _make_bug(4)
        store.remember_result(_make_result(bugs=[bug]))
        summary = store.remember_result(_make_result(bugs=[bug]))

        assert summary["new_bugs"] == 0


class TestGapLifecycle:
    """Test gap creation, querying, and resolution."""

    def test_open_gaps_returned(self, store: Neo4jStore) -> None:
        bug = _make_bug(10)
        gap = _make_gap(bug, score=60)
        store.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        open_gaps = store.get_open_gaps()
        test_gaps = [g for g in open_gaps if g["bug_key"] == f"{_TEST_PREFIX}-10"]

        assert len(test_gaps) == 1
        assert test_gaps[0]["confidence"] == 60

    def test_mark_gap_resolved(self, store: Neo4jStore) -> None:
        bug = _make_bug(11)
        gap = _make_gap(bug, score=50)
        store.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        store.mark_gap_resolved(f"{_TEST_PREFIX}-11", "https://github.com/test/issue/1")

        open_gaps = store.get_open_gaps()
        test_gaps = [g for g in open_gaps if g["bug_key"] == f"{_TEST_PREFIX}-11"]
        assert len(test_gaps) == 0


class TestBugStatusUpdate:
    """Test status updates and auto-closing of gaps for resolved bugs."""

    def test_update_status(self, store: Neo4jStore) -> None:
        bug = _make_bug(20, status="New")
        store.remember_result(_make_result(bugs=[bug]))

        updated_bug = Bug(
            key=bug.key, summary=bug.summary, description=bug.description,
            component=bug.component, priority="Blocker", status="Verified",
            created=bug.created, url=bug.url,
        )
        result = store.update_bug_statuses([updated_bug])

        assert result["updated"] == 1

    def test_resolved_bug_auto_closes_gap(self, store: Neo4jStore) -> None:
        bug = _make_bug(21, status="New")
        gap = _make_gap(bug, score=55)
        store.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        resolved_bug = Bug(
            key=bug.key, summary=bug.summary, description=bug.description,
            component=bug.component, priority=bug.priority, status="Closed",
            created=bug.created, url=bug.url,
        )
        result = store.update_bug_statuses([resolved_bug])

        assert result["gaps_closed"] == 1

        open_gaps = store.get_open_gaps()
        test_gaps = [g for g in open_gaps if g["bug_key"] == f"{_TEST_PREFIX}-21"]
        assert len(test_gaps) == 0


class TestFilterDecisions:
    """Test that filter decisions (skip reasons) are stored."""

    def test_filtered_out_bugs_marked(self, store: Neo4jStore) -> None:
        bug = _make_bug(30)
        filtered = FilterResult(
            bug=bug, chaos_relevant=False, skip_reason="CVE — not a resilience issue",
        )
        result = _make_result(bugs=[bug], filtered_out=[filtered])
        store.remember_result(result)

        with store._driver.session() as session:
            r = session.run(
                "MATCH (b:Bug {key: $key}) RETURN b.chaos_relevant AS relevant, b.skip_reason AS reason",
                key=f"{_TEST_PREFIX}-30",
            )
            record = r.single()
            assert record["relevant"] is False
            assert "CVE" in record["reason"]

    def test_domain_filter_sets_virt_relevant_not_chaos(self, store: Neo4jStore) -> None:
        bug = _make_bug(31)
        passed = FilterResult(
            bug=bug,
            chaos_relevant=True,  # FilterResult reuse; Neo4j maps by filter_mode
            failure_mode="Domain indicators: kubevirt",
            confidence=0.85,
        )
        result = _make_result(
            bugs=[bug], passed=[passed], filter_mode="domain",
        )
        store.remember_result(result)

        with store._driver.session() as session:
            r = session.run(
                """
                MATCH (b:Bug {key: $key})
                RETURN b.virt_relevant AS virt,
                       b.chaos_relevant AS chaos,
                       b.last_filter_mode AS mode
                """,
                key=f"{_TEST_PREFIX}-31",
            )
            record = r.single()
            assert record["virt"] is True
            assert record["chaos"] is None or record["chaos"] is False
            assert record["mode"] == "domain"

        assert store.count_virt_relevant_bugs() >= 1


class TestRunHistory:
    """Test run history and metrics storage."""

    def test_run_recorded_in_history(self, store: Neo4jStore) -> None:
        bug = _make_bug(40)
        store.remember_result(_make_result(bugs=[bug]))

        history = store.get_run_history(limit=50)
        test_runs = [h for h in history if h["agent"] == f"{_TEST_PREFIX}_agent"]

        assert len(test_runs) >= 1
        assert test_runs[0]["discovered"] == 1

    def test_store_and_retrieve_metrics(self, store: Neo4jStore) -> None:
        bug = _make_bug(41)
        store.remember_result(_make_result(bugs=[bug]))

        metrics = {
            "agent": f"{_TEST_PREFIX}_agent",
            "bugs_processed": 10,
            "bugs_succeeded": 8,
            "filter_retries": 1,
            "filter_escalations": 0,
            "map_fallbacks": 0,
            "analyze_retries": 0,
            "total_input_tokens": 5000,
            "total_output_tokens": 1200,
            "keyword_filter_hits": 3,
            "semantic_cache_hits": 2,
            "llm_filter_calls": 5,
            "llm_map_calls": 4,
            "llm_analyze_calls": 3,
            "filter_duration_sec": 1.5,
            "map_duration_sec": 2.0,
            "analyze_duration_sec": 3.5,
        }
        store.store_run_metrics(metrics)

        history = store.get_metrics_history(limit=50)
        test_metrics = [m for m in history if m["agent"] == f"{_TEST_PREFIX}_agent"]

        assert len(test_metrics) >= 1
        assert test_metrics[0]["bugs_processed"] == 10


class TestComponentRelationships:
    """Test component-bug relationship queries."""

    def test_component_gap_counts(self, store: Neo4jStore) -> None:
        bug = _make_bug(50, component="TestComponent-Integration")
        gap = _make_gap(bug, score=65)
        store.remember_result(_make_result(bugs=[bug], gaps=[gap]))

        counts = store.get_component_gap_counts()
        test_counts = [c for c in counts if c["component"] == "TestComponent-Integration"]

        assert len(test_counts) == 1
        assert test_counts[0]["gaps"] == 1

    def test_similar_resolved_bugs(self, store: Neo4jStore) -> None:
        bug = _make_bug(51, component="TestComponent-Resolved")
        gap = _make_gap(bug, score=70)
        store.remember_result(_make_result(bugs=[bug], gaps=[gap]))
        store.mark_gap_resolved(f"{_TEST_PREFIX}-51", "https://github.com/test/pr/99")

        similar = store.get_similar_resolved_bugs("TestComponent-Resolved")

        assert len(similar) >= 1
        assert similar[0]["bug_key"] == f"{_TEST_PREFIX}-51"
