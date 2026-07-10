"""Tests for JIRA label-based discovery helpers."""

from __future__ import annotations

from src.apis.jira_client import build_label_discovery_jql, filter_labels_by_substrings


class TestFilterLabelsBySubstrings:

    def test_matches_case_insensitive_substrings(self) -> None:
        labels = [
            "CNV-QE",
            "kubevirt-ci",
            "openshift-virtualization-4.21",
            "virtualmachine-migration",
            "virtual_machine_test",
            "virtual-machine",
            "unrelated",
            "Arc:positive",
        ]
        substrings = (
            "cnv",
            "kubevirt",
            "openshift-virtualization",
            "virtualmachine",
            "virtual_machine",
            "virtual-machine",
        )
        matched = filter_labels_by_substrings(labels, substrings)
        assert matched == [
            "CNV-QE",
            "kubevirt-ci",
            "openshift-virtualization-4.21",
            "virtual-machine",
            "virtual_machine_test",
            "virtualmachine-migration",
        ]

    def test_returns_empty_when_no_match(self) -> None:
        assert filter_labels_by_substrings(["foo", "bar"], ("cnv",)) == []

    def test_returns_empty_for_empty_substrings(self) -> None:
        assert filter_labels_by_substrings(["cnv"], ()) == []


class TestBuildLabelDiscoveryJql:

    def test_builds_single_clause(self) -> None:
        jqls = build_label_discovery_jql(["kubevirt", "cnv-qe"])
        assert len(jqls) == 1
        assert jqls[0] == 'project = OCPBUGS AND labels in ("kubevirt", "cnv-qe")'

    def test_batches_large_label_sets(self) -> None:
        labels = [f"label-{i}" for i in range(150)]
        jqls = build_label_discovery_jql(labels, batch_size=100)
        assert len(jqls) == 2
        assert "label-0" in jqls[0]
        assert "label-149" in jqls[1]

    def test_returns_empty_for_no_labels(self) -> None:
        assert build_label_discovery_jql([]) == []
