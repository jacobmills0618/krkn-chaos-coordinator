"""Tests for scenario indexing."""

import tempfile
from pathlib import Path

import yaml

from src.knowledge.scenario_index import index_scenarios_from_repo, index_plugins_from_repo


class TestIndexScenariosFromRepo:
    def test_indexes_yaml_files(self, tmp_path):
        scenarios_dir = tmp_path / "scenarios" / "openshift"
        scenarios_dir.mkdir(parents=True)

        scenario_config = [
            {
                "pod_scenarios": {
                    "namespace": "openshift-etcd",
                    "label_selector": "app=etcd",
                }
            }
        ]
        yaml_file = scenarios_dir / "etcd_pod_scenarios.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(scenario_config, f)

        scenarios = index_scenarios_from_repo(tmp_path)
        assert len(scenarios) == 1
        assert scenarios[0].scenario_type == "pod_scenarios"
        assert scenarios[0].plugin_name == "pod"
        assert "openshift-etcd" in scenarios[0].description

    def test_handles_missing_directory(self, tmp_path):
        scenarios = index_scenarios_from_repo(tmp_path / "nonexistent")
        assert scenarios == []

    def test_handles_invalid_yaml(self, tmp_path):
        scenarios_dir = tmp_path / "scenarios"
        scenarios_dir.mkdir()
        bad_file = scenarios_dir / "bad.yaml"
        bad_file.write_text("{{invalid yaml content")

        scenarios = index_scenarios_from_repo(tmp_path)
        assert scenarios == []

    def test_handles_non_list_yaml(self, tmp_path):
        scenarios_dir = tmp_path / "scenarios"
        scenarios_dir.mkdir()
        yaml_file = scenarios_dir / "simple.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump({"key": "value"}, f)

        scenarios = index_scenarios_from_repo(tmp_path)
        assert scenarios == []


class TestIndexPluginsFromRepo:
    def test_lists_plugin_directories(self, tmp_path):
        plugins_dir = tmp_path / "krkn" / "scenario_plugins"
        (plugins_dir / "pod_disruption").mkdir(parents=True)
        (plugins_dir / "node_actions").mkdir(parents=True)
        (plugins_dir / "__pycache__").mkdir(parents=True)

        plugins = index_plugins_from_repo(tmp_path)
        assert "pod_disruption" in plugins
        assert "node_actions" in plugins
        assert "__pycache__" not in plugins

    def test_handles_missing_directory(self, tmp_path):
        plugins = index_plugins_from_repo(tmp_path)
        assert plugins == []


class TestBuildKrknCatalog:
    def test_discovers_plugins_and_scenarios(self, tmp_path):
        plugins_dir = tmp_path / "krkn" / "scenario_plugins"
        (plugins_dir / "network_chaos").mkdir(parents=True)
        (plugins_dir / "hogs").mkdir(parents=True)
        scenarios_dir = tmp_path / "scenarios" / "openshift"
        scenarios_dir.mkdir(parents=True)
        (scenarios_dir / "etcd.yml").write_text("[]\n")

        from src.knowledge.scenario_index import build_krkn_catalog, format_krkn_catalog_for_prompt

        catalog = build_krkn_catalog(tmp_path)
        assert catalog["source"] == "repo"
        assert "network_chaos" in catalog["plugins"]
        assert "hogs" in catalog["plugins"]
        assert any(p.endswith("etcd.yml") for p in catalog["scenarios"])

        block = format_krkn_catalog_for_prompt(catalog)
        assert "network_chaos" in block
        assert "scenarios/openshift/etcd.yml" in block

    def test_fallback_when_repo_empty(self, tmp_path):
        from src.knowledge.scenario_index import build_krkn_catalog

        catalog = build_krkn_catalog(tmp_path)
        assert catalog["source"] == "fallback"
        assert "pod_disruption" in catalog["plugins"]
