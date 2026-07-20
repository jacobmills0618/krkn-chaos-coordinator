"""Tests for LLM-enhanced MAP and ANALYZE reasoning."""

import json
from unittest.mock import patch

from src.models import Bug, FilterResult, MatchResult, ScenarioMatch, GapAnalysis, Confidence, ActionType
from src.reasoning import llm_map_match, llm_analyze_gap


def _make_bug(key="TEST-1", summary="", description="", component="Etcd"):
    return Bug(
        key=key,
        summary=summary,
        description=description,
        component=component,
        priority="Major",
        status="New",
        created="2026-04-06",
        url=f"https://redhat.atlassian.net/browse/{key}",
    )


def _make_filter_result(bug, failure_mode="pod crash", injection_method="pod kill"):
    return FilterResult(
        bug=bug,
        chaos_relevant=True,
        failure_mode=failure_mode,
        injection_method=injection_method,
    )


class TestLlmMapMatch:
    """Test LLM-enhanced scenario matching."""

    @patch("src.reasoning.call_llm")
    def test_full_match_when_llm_says_exact_coverage(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "match": "FULL_MATCH",
            "matched_scenario": "scenarios/openshift/etcd.yml",
            "explanation": "This scenario kills etcd containers which directly tests the failure mode described in the bug.",
        })

        bug = _make_bug(
            summary="etcd crash under network partition",
            description="etcd members crash when network is partitioned",
        )
        filter_result = _make_filter_result(bug)
        scenario_hits = [
            {"text": "Scenario file: scenarios/openshift/etcd.yml\nkill etcd container", "distance": 0.3},
        ]
        doc_hits = [
            {"text": "etcd is the key-value store for Kubernetes", "distance": 0.4},
        ]

        result = llm_map_match(bug, filter_result, scenario_hits, doc_hits)

        assert result.match_result == MatchResult.FULL_MATCH
        assert result.matched_scenario == "scenarios/openshift/etcd.yml"
        assert result.similarity_score > 0.0
        mock_llm.assert_called_once()

    @patch("src.reasoning.call_llm")
    def test_partial_match_when_same_component_different_failure(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "match": "PARTIAL_MATCH",
            "matched_scenario": "scenarios/openshift/network_chaos.yaml",
            "explanation": "Scenario tests network latency but not CPU-induced OVN state desync.",
        })

        bug = _make_bug(
            summary="OVN state desync under CPU load",
            component="Networking / ovn-kubernetes",
        )
        filter_result = _make_filter_result(bug, "state desync", "CPU hog")
        scenario_hits = [
            {"text": "Scenario file: scenarios/openshift/network_chaos.yaml\nnetwork_chaos: latency: 50ms", "distance": 0.5},
        ]

        result = llm_map_match(bug, filter_result, scenario_hits, [])

        assert result.match_result == MatchResult.PARTIAL_MATCH
        assert result.matched_scenario == "scenarios/openshift/network_chaos.yaml"

    @patch("src.reasoning.call_llm")
    def test_no_match_when_nothing_covers_failure(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "match": "NO_MATCH",
            "matched_scenario": None,
            "explanation": "No existing scenario tests admission webhook pressure on CVO.",
        })

        bug = _make_bug(
            summary="CVO bootstrap deadlock due to ClusterResourceQuota",
            component="Cluster Version Operator",
        )
        filter_result = _make_filter_result(bug, "deadlock", "resource pressure")

        result = llm_map_match(bug, filter_result, [], [])

        assert result.match_result == MatchResult.NO_MATCH
        assert result.matched_scenario is None
        assert result.filter_injection_method == "resource pressure"

    @patch("src.reasoning.call_llm")
    def test_soft_partial_when_no_match_but_chroma_hit(self, mock_llm):
        mock_llm.return_value = json.dumps({
            "match": "NO_MATCH",
            "matched_scenario": None,
            "explanation": "Nothing exact.",
        })
        bug = _make_bug(summary="etcd under load")
        filter_result = _make_filter_result(bug)
        hits = [{
            "text": "Scenario file: scenarios/openshift/etcd.yml\netcd kill",
            "distance": 0.7,
        }]
        result = llm_map_match(bug, filter_result, hits, [])
        assert result.match_result == MatchResult.PARTIAL_MATCH
        assert result.matched_scenario == "scenarios/openshift/etcd.yml"
        assert result.similarity_score < 0.5

    @patch("src.reasoning.call_llm")
    def test_falls_back_on_invalid_json(self, mock_llm):
        mock_llm.return_value = "I don't understand the question"

        bug = _make_bug(summary="some bug")
        filter_result = _make_filter_result(bug)

        result = llm_map_match(bug, filter_result, [], [])

        assert result.match_result == MatchResult.NO_MATCH

    @patch("src.reasoning.call_llm")
    def test_falls_back_on_llm_exception(self, mock_llm):
        mock_llm.side_effect = Exception("Ollama connection refused")

        bug = _make_bug(summary="some bug")
        filter_result = _make_filter_result(bug)

        result = llm_map_match(bug, filter_result, [{"text": "scenario", "distance": 0.4}], [])

        assert isinstance(result, ScenarioMatch)


class TestLlmAnalyzeGap:
    """Test LLM-enhanced gap analysis."""

    @patch("src.knowledge.scenario_index.build_krkn_catalog")
    @patch("src.reasoning.call_llm")
    def test_high_confidence_with_specific_modifications(self, mock_llm, mock_catalog):
        mock_catalog.return_value = {
            "plugins": ["hogs", "network_chaos"],
            "scenarios": ["scenarios/openshift/network_chaos.yaml"],
            "source": "repo",
            "repo_path": "/tmp/krkn",
        }
        mock_llm.return_value = json.dumps({
            "confidence_score": 85,
            "reasoning": "Clear failure mode: CPU stress causes OVN state desync. cpu_hog_scenarios plugin can inject this. OVN architecture is well documented.",
            "modifications": [
                "Create scenarios/openshift/ovn_cpu_stress.yaml using hog_scenarios plugin with CPU target 90%, duration 300s on worker nodes",
                "While hog is active, validate OVN state: ovs-vsctl show + ovn-sbctl list chassis",
                "Assert: ovn-controller chassis bindings remain synchronized under CPU pressure",
            ],
            "krkn_plugin": "hogs",
            "repos_to_update": ["krkn", "krkn-hub"],
        })

        bug = _make_bug(
            summary="OVN state desync under CPU load",
            component="Networking / ovn-kubernetes",
            description="Under high CPU load, OVS has interface but ovn-controller has no chassis binding.",
        )
        match = ScenarioMatch(
            bug=bug,
            match_result=MatchResult.PARTIAL_MATCH,
            matched_scenario="scenarios/openshift/network_chaos.yaml",
        )

        gap = llm_analyze_gap(
            bug=bug,
            match=match,
            ocp_docs=[{"text": "OVN-Kubernetes uses distributed virtual routing"}],
            krkn_docs=[{"text": "CPU Hog scenario creates CPU pressure on nodes"}],
            neo4j_history=[],
        )

        assert gap.confidence_score == 85
        assert gap.confidence_level == Confidence.HIGH
        assert gap.action_type == ActionType.DRAFT_PR
        assert gap.krkn_plugin == "hogs"
        assert len(gap.modifications) == 3
        assert "cpu" in gap.reasoning.lower()

    @patch("src.knowledge.scenario_index.build_krkn_catalog")
    @patch("src.reasoning.call_llm")
    def test_low_confidence_when_llm_uncertain(self, mock_llm, mock_catalog):
        mock_catalog.return_value = {
            "plugins": ["pod_disruption"],
            "scenarios": [],
            "source": "repo",
            "repo_path": "/tmp/krkn",
        }
        mock_llm.return_value = json.dumps({
            "confidence_score": 25,
            "reasoning": "Unclear failure mode. No obvious krkn plugin matches admission webhook delays.",
            "modifications": [],
            "krkn_plugin": None,
            "repos_to_update": [],
        })

        bug = _make_bug(
            summary="Obscure admission webhook issue",
            description="Something happens sometimes.",
        )
        match = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)

        gap = llm_analyze_gap(bug=bug, match=match, ocp_docs=[], krkn_docs=[], neo4j_history=[])

        assert gap.confidence_score == 25
        assert gap.confidence_level == Confidence.LOW
        assert gap.action_type == ActionType.GITHUB_ISSUE

    @patch("src.knowledge.scenario_index.build_krkn_catalog")
    @patch("src.reasoning.call_llm")
    def test_medium_without_plugin_triggers_compact_pick(self, mock_llm, mock_catalog):
        mock_catalog.return_value = {
            "plugins": ["network_chaos", "pod_disruption", "hogs"],
            "scenarios": ["scenarios/openshift/network_chaos.yaml"],
            "source": "repo",
            "repo_path": "/tmp/krkn",
        }
        mock_llm.side_effect = [
            json.dumps({
                "confidence_score": 55,
                "reasoning": "Clear gap but forgot plugin",
                "modifications": ["add scenario"],
                "krkn_plugin": None,
                "repos_to_update": ["krkn"],
            }),
            json.dumps({"krkn_plugin": "network_chaos", "reasoning": "network failure"}),
        ]
        bug = _make_bug(summary="network partition breaks SDN", description="x" * 250)
        match = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)
        gap = llm_analyze_gap(bug, match, [], [], [])
        assert gap.confidence_score == 55
        assert gap.confidence_level == Confidence.MEDIUM
        assert gap.krkn_plugin == "network_chaos"
        assert mock_llm.call_count == 2

    @patch("src.knowledge.scenario_index.build_krkn_catalog")
    @patch("src.reasoning.call_llm")
    def test_evidence_cap_without_plugin_or_base(self, mock_llm, mock_catalog):
        mock_catalog.return_value = {
            "plugins": ["pod_disruption"],
            "scenarios": [],
            "source": "repo",
            "repo_path": "/tmp/krkn",
        }
        mock_llm.side_effect = [
            json.dumps({
                "confidence_score": 50,
                "reasoning": "Looks like a gap",
                "modifications": [],
                "krkn_plugin": None,
                "repos_to_update": [],
            }),
            json.dumps({"krkn_plugin": None}),
        ]
        bug = _make_bug(summary="obscure failure")
        match = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)
        gap = llm_analyze_gap(bug, match, [], [], [])
        assert gap.confidence_score == 39
        assert gap.confidence_level == Confidence.LOW
        assert gap.krkn_plugin is None

    @patch("src.knowledge.scenario_index.build_krkn_catalog")
    @patch("src.reasoning.call_llm")
    def test_analyze_rejects_invented_plugin_not_in_repo(self, mock_llm, mock_catalog):
        mock_catalog.return_value = {
            "plugins": ["network_chaos", "hogs"],
            "scenarios": [],
            "source": "repo",
            "repo_path": "/tmp/krkn",
        }
        mock_llm.side_effect = [
            json.dumps({
                "confidence_score": 60,
                "reasoning": "gap",
                "modifications": ["x"],
                "krkn_plugin": "totally_fake_plugin",
                "repos_to_update": ["krkn"],
            }),
            json.dumps({"krkn_plugin": "hogs", "reasoning": "cpu pressure"}),
        ]
        bug = _make_bug(summary="API server throttling under load")
        match = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)
        gap = llm_analyze_gap(bug, match, [], [], [])
        assert gap.krkn_plugin == "hogs"
        assert gap.confidence_score == 60

    @patch("src.knowledge.scenario_index.build_krkn_catalog")
    @patch("src.reasoning.call_llm")
    def test_falls_back_on_invalid_json(self, mock_llm, mock_catalog):
        mock_catalog.return_value = {
            "plugins": ["pod_disruption"],
            "scenarios": [],
            "source": "fallback",
            "repo_path": "/tmp/krkn",
        }
        mock_llm.return_value = "This is not JSON"

        bug = _make_bug(summary="some bug")
        match = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)

        gap = llm_analyze_gap(bug=bug, match=match, ocp_docs=[], krkn_docs=[], neo4j_history=[])

        assert isinstance(gap, GapAnalysis)
        assert gap.confidence_level == Confidence.LOW
        assert gap.confidence_score < 40

    @patch("src.knowledge.scenario_index.build_krkn_catalog")
    @patch("src.reasoning.call_llm")
    def test_clamps_score_to_valid_range(self, mock_llm, mock_catalog):
        mock_catalog.return_value = {
            "plugins": ["pod_disruption"],
            "scenarios": [],
            "source": "repo",
            "repo_path": "/tmp/krkn",
        }
        mock_llm.return_value = json.dumps({
            "confidence_score": 150,
            "reasoning": "Very confident",
            "modifications": ["do something"],
            "krkn_plugin": "pod_disruption",
            "repos_to_update": ["krkn"],
        })

        bug = _make_bug(summary="etcd crash")
        match = ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)

        gap = llm_analyze_gap(bug=bug, match=match, ocp_docs=[], krkn_docs=[], neo4j_history=[])

        assert gap.confidence_score <= 100
        assert gap.krkn_plugin == "pod_disruption"