"""Index existing krkn chaos scenarios from local repos."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScenarioInfo:
    name: str
    file_path: str
    scenario_type: str
    plugin_name: str
    config: dict = field(default_factory=dict)
    description: str = ""


def index_scenarios_from_repo(krkn_repo_path: Path) -> list[ScenarioInfo]:
    """Scan the krkn repo's scenarios/ directory and index all scenario YAML files."""
    scenarios_dir = krkn_repo_path / "scenarios"
    if not scenarios_dir.exists():
        logger.warning("Scenarios directory not found: %s", scenarios_dir)
        return []

    scenarios = []
    for yaml_file in scenarios_dir.rglob("*.y*ml"):
        try:
            with open(yaml_file) as f:
                content = yaml.safe_load(f)
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Failed to parse %s: %s", yaml_file, e)
            continue

        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            for scenario_type, config in item.items():
                if not isinstance(config, dict):
                    continue
                scenarios.append(
                    ScenarioInfo(
                        name=yaml_file.stem,
                        file_path=str(yaml_file.relative_to(krkn_repo_path)),
                        scenario_type=scenario_type,
                        plugin_name=_type_to_plugin(scenario_type),
                        config=config,
                        description=_extract_description(config),
                    )
                )

    logger.info("Indexed %d scenarios from %s", len(scenarios), krkn_repo_path)
    return scenarios


def index_plugins_from_repo(krkn_repo_path: Path) -> list[str]:
    """List all scenario plugin directories in the krkn repo."""
    plugins_dir = krkn_repo_path / "krkn" / "scenario_plugins"
    if not plugins_dir.exists():
        return []

    plugins = []
    for item in plugins_dir.iterdir():
        if item.is_dir() and not item.name.startswith("_"):
            plugins.append(item.name)

    logger.info("Found %d plugins: %s", len(plugins), plugins)
    return plugins


def _type_to_plugin(scenario_type: str) -> str:
    """Map a scenario type key to the plugin directory name."""
    return scenario_type.replace("_scenarios", "").replace("_scenario", "")


def _extract_description(config: dict) -> str:
    """Extract a human-readable description from scenario config."""
    parts = []
    if "namespace" in config:
        parts.append(f"namespace={config['namespace']}")
    if "label_selector" in config:
        parts.append(f"selector={config['label_selector']}")
    if "node_name" in config:
        parts.append(f"node={config['node_name']}")
    if "scenario_type" in config:
        parts.append(f"type={config['scenario_type']}")
    return ", ".join(parts)


def scenario_github_url(
    path: str | None,
    repo: str = "krkn-chaos/krkn",
    branch: str = "main",
) -> str | None:
    """Build a GitHub URL for a krkn scenario or plugin path.

    Expects repo-relative paths as produced by index_scenarios_from_repo
    (e.g. ``scenarios/openshift/etcd.yml``), Chroma ingest
    (``Scenario file: scenarios/...``), or plugin paths under
    ``krkn/scenario_plugins/``.
    """
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    normalized = path.lstrip("/")
    if normalized.startswith("scenarios/"):
        return f"https://github.com/{repo}/blob/{branch}/{normalized}"
    if normalized.startswith("krkn/scenario_plugins/"):
        if normalized.endswith("/"):
            return f"https://github.com/{repo}/tree/{branch}/{normalized.rstrip('/')}"
        return f"https://github.com/{repo}/blob/{branch}/{normalized}"
    return None
