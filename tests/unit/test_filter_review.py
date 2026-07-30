"""Tests for filter pass/skip review formatting."""

from src.coordinator.filter_review import (
    collect_filter_results,
    filter_results_to_dict,
    format_filter_pass_list,
    format_filter_skip_list,
    format_neo4j_chaos_relevant_inventory,
    format_neo4j_virt_relevant_inventory,
    load_filter_review_json,
)
from src.models import AgentResult, Bug, FilterResult


def _bug(key: str = "OCPBUGS-1") -> Bug:
    return Bug(
        key=key,
        summary=f"summary for {key}",
        description="",
        component="Virtualization",
        priority="Major",
        status="New",
        created="2026-01-01",
        url=f"https://issues.redhat.com/browse/{key}",
    )


def _pass_result(key: str = "OCPBUGS-1", confidence: float = 0.85) -> FilterResult:
    return FilterResult(
        bug=_bug(key),
        chaos_relevant=True,
        failure_mode="Domain indicators: kubevirt",
        injection_method="pod",
        confidence=confidence,
    )


def _skip_result(key: str = "OCPBUGS-2") -> FilterResult:
    return FilterResult(
        bug=_bug(key),
        chaos_relevant=False,
        skip_reason="Not domain-relevant: matches skip keyword 'documentation'",
        confidence=0.95,
    )


class TestFilterReviewFormat:
    def test_pass_list_includes_confidence_and_key(self):
        text = format_filter_pass_list([_pass_result()])
        assert "OCPBUGS-1" in text
        assert "85%" in text
        assert "Injection: pod" in text

    def test_skip_list_includes_reason(self):
        text = format_filter_skip_list([_skip_result()])
        assert "OCPBUGS-2" in text
        assert "documentation" in text

    def test_collect_from_agent_result(self):
        result = AgentResult(
            agent_name="virtualization",
            bugs_passed_filter=[_pass_result("OCPBUGS-10")],
            bugs_filtered_out=[_skip_result("OCPBUGS-11")],
        )
        passed, skipped = collect_filter_results([result], agent_name="virtualization")
        assert len(passed) == 1
        assert len(skipped) == 1
        assert passed[0].bug.key == "OCPBUGS-10"

    def test_filter_results_to_dict(self):
        data = filter_results_to_dict([_pass_result()], [_skip_result()])
        assert data["counts"] == {"passed": 1, "skipped": 1}
        assert data["passed"][0]["confidence"] == 0.85
        assert data["skipped"][0]["skip_reason"] is not None

    def test_load_filter_review_json_roundtrip(self, tmp_path):
        path = tmp_path / "review.json"
        from src.coordinator.filter_review import write_filter_review_json

        write_filter_review_json(path, [_pass_result()], [_skip_result()])
        passed, skipped = load_filter_review_json(path)
        assert len(passed) == 1
        assert len(skipped) == 1
        assert passed[0].bug.key == "OCPBUGS-1"

    def test_neo4j_inventory_format_is_labeled_historical(self):
        text = format_neo4j_chaos_relevant_inventory(
            total=121,
            by_filter_agent=[{"filter_agent": "virtualization", "bugs": 100}],
            by_component=[
                {"component": "HyperShift / OCP Virtualization", "bugs": 40},
                {"component": "Networking / ovn-kubernetes", "bugs": 10},
            ],
        )
        assert "121" in text
        assert "chaos_relevant=true" in text
        assert "NOT the same as this-run" in text
        assert "virtualization: 100" in text
        assert "HyperShift / OCP Virtualization: 40" in text
        assert "control_plane domain" not in text.lower()

    def test_virt_relevant_inventory_is_not_chaos_labeled(self):
        text = format_neo4j_virt_relevant_inventory(
            total=42,
            by_filter_agent=[{"filter_agent": "control_plane", "bugs": 5}],
            by_component=[{"component": "Etcd", "bugs": 3}],
        )
        assert "virt_relevant=true" in text
        assert "domain / ocp-virt" in text
        assert "chaos_relevant" not in text
        assert "control_plane: 5" in text
        assert "Etcd: 3" in text
