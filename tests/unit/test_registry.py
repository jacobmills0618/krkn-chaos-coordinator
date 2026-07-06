"""Tests for pluggable agent registry."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from src.agents.registry import AgentConfig, discover_agents, _load_agent_config


def _write_yaml(directory: Path, name: str, data: dict) -> Path:
    path = directory / f"{name}.yaml"
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


class TestLoadAgentConfig:

    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "test_agent", {
            "name": "test_agent",
            "description": "A test agent",
            "components": ["CompA", "CompB"],
        })
        config = _load_agent_config(path)

        assert config.name == "test_agent"
        assert config.description == "A test agent"
        assert config.components == ("CompA", "CompB")
        assert isinstance(config, AgentConfig)

    def test_config_is_frozen(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "frozen", {
            "name": "frozen", "description": "", "components": ["X"],
        })
        config = _load_agent_config(path)

        with pytest.raises(AttributeError):
            config.name = "mutated"

    def test_raises_on_missing_name(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "bad", {
            "components": ["A"],
        })
        with pytest.raises(ValueError, match="missing required 'name'"):
            _load_agent_config(path)

    def test_raises_on_empty_components(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "empty", {
            "name": "empty", "components": [],
        })
        with pytest.raises(ValueError, match="empty"):
            _load_agent_config(path)

    def test_description_defaults_to_empty(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "nodesc", {
            "name": "nodesc", "components": ["X"],
        })
        config = _load_agent_config(path)
        assert config.description == ""

    def test_loads_discovery_jql(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, "virt", {
            "name": "virt",
            "components": ["Virtualization"],
            "discovery_jql": 'project = OCPBUGS AND text ~ "kubevirt"',
        })
        config = _load_agent_config(path)
        assert config.discovery_jql == 'project = OCPBUGS AND text ~ "kubevirt"'


class TestDiscoverAgents:

    def test_discovers_all_yaml_files(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "alpha", {"name": "alpha", "components": ["A"]})
        _write_yaml(tmp_path, "beta", {"name": "beta", "components": ["B"]})

        agents = discover_agents(config_dir=tmp_path)

        assert len(agents) == 2
        assert "alpha" in agents
        assert "beta" in agents

    def test_returns_empty_for_missing_directory(self, tmp_path: Path) -> None:
        agents = discover_agents(config_dir=tmp_path / "nonexistent")
        assert agents == {}

    def test_skips_invalid_yaml(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "good", {"name": "good", "components": ["A"]})
        _write_yaml(tmp_path, "bad", {"components": []})

        agents = discover_agents(config_dir=tmp_path)

        assert len(agents) == 1
        assert "good" in agents

    def test_skips_duplicate_names(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "aaa_first", {"name": "dupe", "components": ["A"]})
        _write_yaml(tmp_path, "zzz_second", {"name": "dupe", "components": ["B"]})

        agents = discover_agents(config_dir=tmp_path)

        assert len(agents) == 1
        assert agents["dupe"].components == ("A",)

    def test_ignores_non_yaml_files(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "real", {"name": "real", "components": ["A"]})
        (tmp_path / "README.md").write_text("not yaml")
        (tmp_path / "notes.txt").write_text("not yaml")

        agents = discover_agents(config_dir=tmp_path)

        assert len(agents) == 1

    def test_discovers_real_config_agents(self) -> None:
        agents = discover_agents()

        assert len(agents) >= 6
        assert "control_plane" in agents
        assert "networking" in agents
        assert "virtualization" in agents
        assert len(agents["control_plane"].components) > 0

    def test_new_yaml_file_auto_discovered(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "existing", {"name": "existing", "components": ["A"]})
        agents_before = discover_agents(config_dir=tmp_path)
        assert len(agents_before) == 1

        _write_yaml(tmp_path, "new_agent", {"name": "new_agent", "components": ["B", "C"]})
        agents_after = discover_agents(config_dir=tmp_path)

        assert len(agents_after) == 2
        assert "new_agent" in agents_after
        assert agents_after["new_agent"].components == ("B", "C")
