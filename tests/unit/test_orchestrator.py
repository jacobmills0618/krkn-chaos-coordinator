"""Tests for the orchestrator."""

from src.coordinator.orchestrator import deduplicate_gaps, format_approval_queue, format_summary
from src.models import (
    ActionType,
    AgentResult,
    Bug,
    Confidence,
    GapAnalysis,
)


def _make_bug(key="TEST-1", summary="test bug"):
    return Bug(key=key, summary=summary, description="", component="Etcd",
               priority="Major", status="New", created="2026-03-30", url="")


def _make_gap(bug, score=50):
    confidence = Confidence.HIGH if score >= 70 else Confidence.MEDIUM if score >= 40 else Confidence.LOW
    action = ActionType.DRAFT_PR if score >= 70 else ActionType.GITHUB_ISSUE
    return GapAnalysis(bug=bug, confidence_score=score, confidence_level=confidence, action_type=action)


class TestDeduplicateGaps:
    def test_deduplicates_by_bug_key(self):
        bug = _make_bug("BUG-1")
        result1 = AgentResult(agent_name="a1", gaps=[_make_gap(bug, 50)])
        result2 = AgentResult(agent_name="a2", gaps=[_make_gap(bug, 80)])

        gaps = deduplicate_gaps([result1, result2])
        assert len(gaps) == 1
        assert gaps[0].confidence_score == 80  # keeps higher score

    def test_no_duplicates(self):
        bug1 = _make_bug("BUG-1")
        bug2 = _make_bug("BUG-2")
        result = AgentResult(agent_name="a1", gaps=[_make_gap(bug1, 50), _make_gap(bug2, 70)])

        gaps = deduplicate_gaps([result])
        assert len(gaps) == 2

    def test_sorted_by_confidence_desc(self):
        bugs = [_make_bug(f"BUG-{i}") for i in range(3)]
        result = AgentResult(
            agent_name="a1",
            gaps=[_make_gap(bugs[0], 30), _make_gap(bugs[1], 90), _make_gap(bugs[2], 60)],
        )

        gaps = deduplicate_gaps([result])
        assert gaps[0].confidence_score == 90
        assert gaps[1].confidence_score == 60
        assert gaps[2].confidence_score == 30

    def test_empty_results(self):
        gaps = deduplicate_gaps([])
        assert gaps == []


class TestFormatApprovalQueue:
    def test_formats_gaps(self):
        bug = _make_bug("BUG-1", "etcd crash under load")
        gap = _make_gap(bug, 85)
        output = format_approval_queue([gap])
        assert "BUG-1" in output
        assert "HIGH" in output
        assert "Approve" in output

    def test_empty_queue(self):
        output = format_approval_queue([])
        assert "Approval Queue" in output


class TestFormatSummary:
    def test_formats_agent_results(self):
        result = AgentResult(
            agent_name="control_plane",
            bugs_discovered=[_make_bug()],
            gaps=[_make_gap(_make_bug(), 50)],
            analyze_duration_sec=12.5,
            llm_calls=7,
            cost_usd=0.42,
        )
        output = format_summary([result])
        assert "control_plane" in output
        assert "1 discovered" in output
        assert "0 passed filter" in output
        assert "LLM calls:  7" in output
        assert "ANALYZE:    12.5s" in output
        assert "Cost:       $0.42" in output

    def test_passed_is_not_discovered_minus_skipped(self):
        """Known bugs inflate discovered; PASS must use bugs_passed_filter only."""
        from src.models import FilterResult

        bugs = [_make_bug(f"BUG-{i}") for i in range(10)]
        skipped = [
            FilterResult(
                bug=bugs[0], chaos_relevant=False, skip_reason="no virt keywords",
                confidence=0.7,
            )
        ]
        result = AgentResult(
            agent_name="control_plane",
            bugs_discovered=bugs,
            bugs_passed_filter=[],
            bugs_filtered_out=skipped,
        )
        output = format_summary([result])
        assert "Passed:     0 (this run only)" in output
        assert "Skipped:    1 (this run only)" in output
        assert "9 known" in output
        assert "0 passed filter" in output
        # Must NOT report 9 PASS (= discovered - skipped)
        assert "Passed:     9" not in output
