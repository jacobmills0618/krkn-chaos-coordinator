"""Tests for optional draft-PR path in create_issues_for_gaps."""

from unittest.mock import MagicMock, patch

from src.agents.act import create_issues_for_gaps
from src.models import ActionType, Bug, Confidence, GapAnalysis


def _gap(*, action=ActionType.DRAFT_PR, base="scenarios/openshift/etcd.yml", key="OCPBUGS-1"):
    return GapAnalysis(
        bug=Bug(
            key=key,
            summary="test",
            description="",
            component="Etcd",
            priority="Major",
            status="New",
            created="2026-01-01",
            url=f"https://issues.redhat.com/browse/{key}",
        ),
        confidence_score=85,
        confidence_level=Confidence.HIGH,
        action_type=action,
        base_scenario=base,
    )


class TestTryDraftPr:
    def test_default_stays_issues_only(self):
        github = MagicMock()
        with patch("src.agents.pr_creator.create_scenario_pr") as mock_pr:
            create_issues_for_gaps(github, [_gap()], "coordinator", dry_run=True)
        mock_pr.assert_not_called()

    def test_try_draft_pr_calls_pr_creator(self):
        github = MagicMock()
        with patch(
            "src.agents.pr_creator.create_scenario_pr",
            return_value={"dry_run": True, "files": ["a.yaml"]},
        ) as mock_pr:
            results = create_issues_for_gaps(
                github, [_gap()], "coordinator", dry_run=True, try_draft_pr=True,
            )
        mock_pr.assert_called_once()
        assert results[0]["bug_key"] == "OCPBUGS-1"
        github.create_issue.assert_not_called()

    def test_pr_failure_falls_back_to_issue(self):
        github = MagicMock()
        github.create_issue.return_value = {"html_url": "https://example/issues/1"}
        with patch("src.agents.pr_creator.create_scenario_pr", return_value=None):
            results = create_issues_for_gaps(
                github, [_gap()], "coordinator", dry_run=False, try_draft_pr=True,
            )
        assert results[0]["html_url"].endswith("/issues/1")

    def test_no_base_scenario_skips_pr(self):
        github = MagicMock()
        with patch("src.agents.pr_creator.create_scenario_pr") as mock_pr:
            create_issues_for_gaps(
                github,
                [_gap(base=None)],
                "coordinator",
                dry_run=True,
                try_draft_pr=True,
            )
        mock_pr.assert_not_called()
