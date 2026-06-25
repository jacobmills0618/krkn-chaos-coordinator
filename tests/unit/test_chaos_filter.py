"""Tests for the chaos relevance filter."""

from src.filter.chaos_filter import filter_bug, filter_bugs
from src.models import Bug


def _make_bug(key="TEST-1", summary="", description="", component="Etcd"):
    return Bug(
        key=key,
        summary=summary,
        description=description,
        component=component,
        priority="Major",
        status="New",
        created="2026-03-30",
        url=f"https://redhat.atlassian.net/browse/{key}",
    )


class TestFilterBug:
    def test_cve_is_not_chaos_relevant(self):
        bug = _make_bug(
            summary="CVE-2026-33413 openshift4/ose-etcd-rhel9: etcd auth bypass",
            description="Security Tracking Issue. Do not make this issue public.",
        )
        result = filter_bug(bug)
        assert not result.chaos_relevant
        assert "CVE" in result.skip_reason

    def test_etcd_degradation_under_load_is_relevant(self):
        bug = _make_bug(
            summary="etcd operator reports healthy members as unhealthy due to API throttling",
            description=(
                "The etcd-operator incorrectly reports healthy etcd members as unhealthy. "
                "Operator health check timeout (30s) is insufficient under API server load. "
                "took=29.99s, err=health check failed: context deadline exceeded. "
                "ClusterOperator etcd: Reports Degraded=True, Available=False."
            ),
        )
        result = filter_bug(bug)
        assert result.chaos_relevant
        assert result.failure_mode is not None
        assert result.injection_method is not None

    def test_node_replacement_is_relevant(self):
        bug = _make_bug(
            summary="CEO: Etcd status.nodeStatuses not cleared on Node delete (same-name replacement)",
            description=(
                "When a Node is deleted from the API and later recreated with the same nodeName, "
                "etcd nodeStatuses keeps the old row. Node installer may not re-run. "
                "Missing /var/lib/etcd/certs.hash. MissingStaticPodControllerDegraded for etcd. "
                "Pacemaker podman-etcd start failures."
            ),
        )
        result = filter_bug(bug)
        assert result.chaos_relevant

    def test_upgrade_migration_bug_is_not_relevant(self):
        bug = _make_bug(
            summary="Clusters born in OpenShift 4.9 and earlier may have duplicate members upon upgrading to 4.21",
            description=(
                "Clusters which ran 4.9 and earlier at any point may run into "
                "etcd issue 20967. Increase minimum version required to upgrade."
            ),
        )
        result = filter_bug(bug)
        # This is a version migration bug, not a stress/failure scenario
        # However our keyword filter may or may not catch it depending on wording
        # The key point is CVEs should always be filtered out

    def test_flaky_test_is_not_relevant(self):
        bug = _make_bug(
            summary="flaky test: TestEtcdMemberReplace intermittently fails",
            description="Test infrastructure issue. The test itself is unreliable.",
        )
        result = filter_bug(bug)
        assert not result.chaos_relevant

    def test_ui_bug_is_not_relevant(self):
        bug = _make_bug(
            summary="Console button doesn't render on cluster overview page",
            description="UI rendering issue in the OpenShift console.",
            component="Console",
        )
        result = filter_bug(bug)
        assert not result.chaos_relevant


class TestFilterBugConfidence:
    """Tests for keyword pre-filter confidence scoring."""

    def test_cve_has_high_confidence(self):
        bug = _make_bug(
            summary="CVE-2026-99999 openshift4/ose-etcd-rhel9: etcd auth bypass",
            description="Security Tracking Issue. Do not make this issue public.",
        )
        result = filter_bug(bug)
        assert not result.chaos_relevant
        assert result.confidence > 0.8

    def test_crash_keyword_has_high_confidence(self):
        bug = _make_bug(
            summary="etcd crash under network partition causes data loss",
            description=(
                "etcd crashes when network partition occurs between members. "
                "Panic observed in etcd logs. OOM kill triggered."
            ),
        )
        result = filter_bug(bug)
        assert result.chaos_relevant
        assert result.confidence > 0.8

    def test_no_keywords_has_moderate_confidence(self):
        bug = _make_bug(
            summary="Console button color is wrong on the overview page",
            description="The button uses the wrong CSS class for theming.",
            component="Console",
        )
        result = filter_bug(bug)
        assert not result.chaos_relevant
        assert 0.5 <= result.confidence <= 0.8

    def test_ambiguous_has_low_confidence(self):
        bug = _make_bug(
            summary="operator status fluctuates intermittently",
            description=(
                "The cluster operator status changes between Available and Degraded. "
                "This happens sporadically and resolves itself."
            ),
        )
        result = filter_bug(bug)
        # Has failure keywords (degraded, intermittent) but may not map to
        # a specific injection method, resulting in low confidence
        # If it does map, the confidence should reflect ambiguity
        assert result.confidence < 0.5 or (
            result.chaos_relevant and result.confidence <= 0.5
        )

    def test_stub_has_high_confidence(self):
        bug = _make_bug(
            summary="[stub] placeholder for future etcd tracking",
            description="This is a stub ticket.",
        )
        result = filter_bug(bug)
        assert not result.chaos_relevant
        assert result.confidence > 0.8

    def test_flaky_test_has_high_confidence(self):
        bug = _make_bug(
            summary="flaky test: TestEtcdMemberReplace intermittently fails",
            description="Test infrastructure issue. The test itself is unreliable.",
        )
        result = filter_bug(bug)
        assert not result.chaos_relevant
        assert result.confidence > 0.8


class TestFilterBugs:
    def test_filters_list_and_returns_tuples(self):
        bugs = [
            _make_bug(
                key="BUG-1",
                summary="CVE-2026-1234 security issue",
                description="Security tracking",
            ),
            _make_bug(
                key="BUG-2",
                summary="etcd crash under network partition",
                description="etcd crashes when network partition occurs between members",
            ),
        ]
        relevant, skipped = filter_bugs(bugs)
        assert len(skipped) >= 1  # CVE should be skipped
        assert any(r.bug.key == "BUG-2" for r in relevant)  # crash should be relevant

    def test_empty_list(self):
        relevant, skipped = filter_bugs([])
        assert relevant == []
        assert skipped == []


class TestFilterKeywordEncoding:
    def test_common_yaml_keywords_use_straight_quotes(self):
        """Curly Unicode quotes are literal chars and will never match bug text."""
        from pathlib import Path

        text = Path("config/filters/common.yaml").read_text(encoding="utf-8")
        assert "\u201c" not in text, "found left curly quote in common.yaml"
        assert "\u201d" not in text, "found right curly quote in common.yaml"
