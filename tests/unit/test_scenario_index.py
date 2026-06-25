"""Tests for scenario indexing."""

import tempfile
from pathlib import Path

import yaml

from src.knowledge.scenario_index import (
    index_plugins_from_repo,
    index_scenarios_from_repo,
    scenario_github_url,
)


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


class TestScenarioGithubUrl:
    def test_builds_url_for_indexed_path(self):
        path = "scenarios/openshift/etcd_pod_scenarios.yaml"
        assert scenario_github_url(path) == (
            "https://github.com/krkn-chaos/krkn/blob/main/scenarios/openshift/etcd_pod_scenarios.yaml"
        )

    def test_strips_leading_slash(self):
        assert scenario_github_url("/scenarios/openshift/etcd.yml") == (
            "https://github.com/krkn-chaos/krkn/blob/main/scenarios/openshift/etcd.yml"
        )

    def test_returns_existing_url_unchanged(self):
        url = "https://github.com/krkn-chaos/krkn/blob/main/scenarios/foo.yaml"
        assert scenario_github_url(url) == url

    def test_returns_none_for_non_scenario_path(self):
        assert scenario_github_url("node-cpu-hog") is None
        assert scenario_github_url("krkn/scenario_plugins/pod_disruption/") is None
        assert scenario_github_url(None) is None
        assert scenario_github_url("") is None


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
