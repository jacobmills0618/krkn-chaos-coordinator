"""Tests for issue plugin resolution (MAP / ANALYZE / keyword fallback)."""

from src.agents.act import (
    base_scenario_matches_plugin,
    build_issue_body,
    normalize_krkn_plugin,
    resolve_injection_method,
    _plugin_from_base_scenario,
    _plugin_path,
)
from src.models import ActionType, Bug, Confidence, FactorConfidence, GapAnalysis


def _bug(summary: str, description: str = "") -> Bug:
    return Bug(
        key="OCPBUGS-1",
        summary=summary,
        description=description,
        component="Etcd",
        priority="Major",
        status="New",
        created="2026-01-01",
        url="https://issues.redhat.com/browse/OCPBUGS-1",
    )


def _gap(**kwargs) -> GapAnalysis:
    defaults = dict(
        bug=_bug("some failure"),
        confidence_score=50,
        confidence_level=Confidence.MEDIUM,
        action_type=ActionType.GITHUB_ISSUE,
    )
    defaults.update(kwargs)
    return GapAnalysis(**defaults)


class TestNormalizeKrknPlugin:
    def test_plugin_dir(self):
        assert normalize_krkn_plugin("network_chaos") == _plugin_path("network_chaos")

    def test_scenario_type(self):
        assert normalize_krkn_plugin("hog_scenarios") == _plugin_path("hogs")

    def test_full_path(self):
        assert normalize_krkn_plugin("krkn/scenario_plugins/node_actions/") == _plugin_path(
            "node_actions"
        )

    def test_unknown(self):
        assert normalize_krkn_plugin("not_a_real_plugin") is None


class TestResolveInjectionMethod:
    def test_analyze_plugin_wins(self):
        gap = _gap(
            bug=_bug("machine Failed state"),
            krkn_plugin="node_actions",
            base_scenario="scenarios/openshift/network_chaos.yaml",
        )
        _method, plugin, _hint, source = resolve_injection_method(gap)
        assert plugin == _plugin_path("node_actions")
        assert "ANALYZE" in source

    def test_map_base_scenario_when_no_analyze(self):
        gap = _gap(
            bug=_bug("unrelated wording"),
            base_scenario="scenarios/openshift/network_chaos.yaml",
        )
        _method, plugin, _hint, source = resolve_injection_method(gap)
        assert plugin == _plugin_path("network_chaos")
        assert "MAP match" in source

    def test_map_hog_filename(self):
        gap = _gap(base_scenario="scenarios/kube/cpu-hog.yml")
        _method, plugin, _hint, source = resolve_injection_method(gap)
        assert plugin == _plugin_path("hogs")
        assert "MAP" in source

    def test_keyword_default_labeled(self):
        gap = _gap(bug=_bug("bridge server can crash if you mash refresh"))
        _method, plugin, hint, source = resolve_injection_method(gap)
        assert plugin == _plugin_path("pod_disruption")
        assert "default" in source.lower()
        assert "fallback" in hint.lower()

    def test_filter_injection_method(self):
        gap = _gap(
            bug=_bug("bridge server can crash"),
            filter_injection_method="network partition between pods",
        )
        _method, plugin, _hint, source = resolve_injection_method(gap)
        assert plugin == _plugin_path("network_chaos")
        assert "FILTER" in source

    def test_issue_body_includes_plugin_source(self):
        gap = _gap(
            bug=_bug("network partition"),
            krkn_plugin="network_chaos",
        )
        body = build_issue_body(gap, "networking")
        assert "**Plugin source:** ANALYZE" in body
        assert "krkn/scenario_plugins/network_chaos/" in body

    def test_config_hint_mismatch_hogs_vs_container_ovn(self):
        """ANALYZE hogs + MAP container_ovn must not claim hog_scenarios on that file."""
        gap = _gap(
            bug=_bug("OVS flow causing OVN port binding timeouts"),
            krkn_plugin="hogs",
            base_scenario="scenarios/openshift/container_ovn.yml",
        )
        _method, plugin, hint, source = resolve_injection_method(gap)
        assert plugin == _plugin_path("hogs")
        assert "ANALYZE" in source
        assert "hog_scenarios" in hint
        assert "cpu-hog.yml" in hint or "Author or extend" in hint
        assert "different" in hint.lower()
        assert "container_ovn.yml" in hint
        # Must NOT say: Start from container_ovn.yml (scenario type hog_scenarios)
        assert "Start from `scenarios/openshift/container_ovn.yml` (scenario type `hog_scenarios`)" not in hint

        body = build_issue_body(gap, "networking")
        assert "**Configuration:**" in body
        assert "different" in body.lower() or "differs" in body.lower()
        assert "not as the YAML template" in body or "do not copy" in body.lower()

    def test_config_hint_match_keeps_start_from(self):
        gap = _gap(
            krkn_plugin="hogs",
            base_scenario="scenarios/kube/cpu-hog.yml",
        )
        _method, plugin, hint, _source = resolve_injection_method(gap)
        assert plugin == _plugin_path("hogs")
        assert "Start from `scenarios/kube/cpu-hog.yml`" in hint
        assert "hog_scenarios" in hint
        assert "different" not in hint.lower()

    def test_base_scenario_matches_plugin(self):
        assert base_scenario_matches_plugin(
            "scenarios/kube/cpu-hog.yml", _plugin_path("hogs")
        )
        assert not base_scenario_matches_plugin(
            "scenarios/openshift/container_ovn.yml", _plugin_path("hogs")
        )

    def test_issue_body_includes_confidence_factor_fields(self):
        gap = _gap(
            bug=_bug("network partition"),
            krkn_plugin="network_chaos",
            reproduction_confidence=FactorConfidence.HIGH,
            scenario_confidence=FactorConfidence.LOW,
            understanding_confidence=FactorConfidence.HIGH,
            plugin_confidence=FactorConfidence.HIGH,
            domain_confidence=FactorConfidence.LOW,
            history_confidence=FactorConfidence.LOW,
            confidence_factor_reasons=(
                ("reproduction_confidence", "Clear PF shutdown + pod create steps (+20)"),
                ("scenario_confidence", "No existing scenario to extend (+0)"),
                ("understanding_confidence", "SR-IOV operator drain on PF down (+20)"),
                ("plugin_confidence", "network_chaos injects link failure (+15)"),
                ("domain_confidence", "Does not clearly match the agent domain (+0)"),
                ("history_confidence", "No similar resolved bug found (+0)"),
            ),
        )
        body = build_issue_body(gap, "networking")
        assert "| **Reproduction Confidence** | HIGH |" in body
        assert "| **Scenario Confidence** | LOW |" in body
        assert "| **Understanding Confidence** | HIGH |" in body
        assert "| **Plugin Confidence** | HIGH |" in body
        assert "| **Domain Confidence** | LOW |" in body
        assert "| **History Confidence** | LOW |" in body
        assert (
            "**Reproduction Confidence:** HIGH — Clear PF shutdown + pod create steps (+20)"
            in body
        )
        assert "**Scenario Confidence:** LOW — No existing scenario to extend (+0)" in body



class TestPluginFromBaseScenario:
    def test_kubevirt_path(self):
        assert _plugin_from_base_scenario(
            "scenarios/kubevirt/kubevirt-vm-outage.yaml"
        ) == _plugin_path("kubevirt_vm_outage")
