"""Tests for LLM-passthrough issue creation (catalog-backed ANALYZE fields)."""

from src.agents.act import build_issue_body, resolve_injection_method
from src.models import ActionType, Bug, Confidence, FactorConfidence, GapAnalysis
from src.reasoning import _normalize_plugin_path


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


class TestNormalizePluginPath:
    def test_full_path(self):
        assert (
            _normalize_plugin_path("krkn/scenario_plugins/hogs/", ["hogs", "network_chaos"])
            == "krkn/scenario_plugins/hogs/"
        )

    def test_dir_name(self):
        assert (
            _normalize_plugin_path("hogs", ["hogs", "network_chaos"])
            == "krkn/scenario_plugins/hogs/"
        )

    def test_unknown_rejected_when_catalog_present(self):
        assert _normalize_plugin_path("not_a_plugin", ["hogs"]) is None


class TestResolveInjectionMethod:
    def test_analyze_configuration_passthrough(self):
        gap = _gap(
            krkn_plugin="krkn/scenario_plugins/hogs/",
            injection_method="Create resource pressure with CPU/memory hogs",
            configuration=(
                "Use scenarios/kube/cpu-hog.yml as shape reference. "
                "MAP file scenarios/openshift/container_ovn.yml is a different plugin."
            ),
            base_scenario="scenarios/openshift/container_ovn.yml",
            related_map_note=(
                "MAP match uses container, which differs from hogs — "
                "namespace/selectors only."
            ),
            starter_scenario="scenarios/kube/cpu-hog.yml",
        )
        method, plugin, config, source = resolve_injection_method(gap)
        assert source == "ANALYZE"
        assert plugin == "krkn/scenario_plugins/hogs/"
        assert method.startswith("Create resource pressure")
        assert "cpu-hog.yml" in config
        assert "different plugin" in config

    def test_does_not_invent_plugin_from_keywords(self):
        gap = _gap(
            bug=_bug(
                "etcd quorum loss after network partition and cpu hog",
                description="latency hog",
            ),
        )
        _method, plugin, _config, source = resolve_injection_method(gap)
        assert source == "none"
        assert plugin == "(none)"


class TestBuildIssueBody:
    def test_ocpbugs_99291_style_fields(self):
        """Issue body prints ANALYZE fields (no registry mismatch invention)."""
        gap = _gap(
            bug=_bug(
                "OVS Flow (600k+ flows) causing OVN Port Binding Timeouts when pods are starting",
                description="Worker nodes pressure from high OVS flows causing port binding timeouts. " * 3,
            ),
            confidence_score=73,
            confidence_level=Confidence.HIGH,
            action_type=ActionType.DRAFT_PR,
            krkn_plugin="krkn/scenario_plugins/hogs/",
            failure_mode="OVS flow-table pressure causes OVN port-binding timeouts",
            injection_method="Create resource pressure (CPU/memory/IO hog) and verify component health",
            configuration=(
                "Author or extend a scenario using plugin `krkn/scenario_plugins/hogs/`. "
                "Use `scenarios/kube/cpu-hog.yml` as a shape reference. "
                "MAP closest file `scenarios/openshift/container_ovn.yml` is a different "
                "plugin — reuse namespace/selectors only; do not copy it as the starter."
            ),
            starter_scenario="scenarios/kube/cpu-hog.yml",
            related_map_note=(
                "This MAP match uses the container plugin, which differs from the "
                "recommended hogs plugin above. Treat it as component context only."
            ),
            base_scenario="scenarios/openshift/container_ovn.yml",
            reasoning="ANALYZE picked hogs as a surrogate for OVS CPU saturation.",
            modifications=[
                "Add a hog scenario targeting workers under high OVS flow load",
                "Assert OVN port bindings still succeed under pressure",
            ],
            reproduction_confidence=FactorConfidence.HIGH,
            scenario_confidence=FactorConfidence.HIGH,
            understanding_confidence=FactorConfidence.HIGH,
            plugin_confidence=FactorConfidence.HIGH,
            domain_confidence=FactorConfidence.HIGH,
            history_confidence=FactorConfidence.LOW,
            confidence_factor_reasons=(
                ("reproduction_confidence", "Precise failure chain (+20)"),
                ("scenario_confidence", "container_ovn.yml nearby but different mode (+25)"),
                ("understanding_confidence", "OVS dataplane failure well-documented (+20)"),
                ("plugin_confidence", "hogs injects CPU pressure (+15)"),
                ("domain_confidence", "Networking domain (+10)"),
                ("history_confidence", "No similar resolved bugs (+0)"),
            ),
        )
        body = build_issue_body(gap, "networking")
        assert "**krkn plugin:** `krkn/scenario_plugins/hogs/`" in body
        assert "**Plugin source:** ANALYZE" in body
        assert "cpu-hog.yml" in body
        assert "different" in body.lower()
        assert "container_ovn.yml" in body
        assert "| **Extendable Scenario** | HIGH |" in body
        assert "| **Injection Capability** | HIGH |" in body
        assert "**Extendable Scenario:** HIGH — container_ovn.yml nearby" in body
        assert "Add a hog scenario targeting workers" in body
        # Must not invent registry scenario_type mashups
        assert "scenario type `hog_scenarios`" not in body
