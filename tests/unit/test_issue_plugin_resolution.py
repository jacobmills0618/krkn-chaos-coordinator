"""Tests for issue plugin resolution (MAP / ANALYZE / keyword fallback)."""

from src.agents.act import (
    build_issue_body,
    normalize_krkn_plugin,
    resolve_injection_method,
    _plugin_from_base_scenario,
    _plugin_path,
)
from src.models import ActionType, Bug, Confidence, GapAnalysis


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


class TestPluginFromBaseScenario:
    def test_kubevirt_path(self):
        assert _plugin_from_base_scenario(
            "scenarios/kubevirt/kubevirt-vm-outage.yaml"
        ) == _plugin_path("kubevirt_vm_outage")
