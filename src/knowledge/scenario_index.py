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
    return sorted(plugins)


def list_scenario_files(krkn_repo_path: Path, limit: int | None = None) -> list[str]:
    """List relative paths of scenario YAML files under ``scenarios/``."""
    scenarios_dir = krkn_repo_path / "scenarios"
    if not scenarios_dir.exists():
        return []

    paths: list[str] = []
    for yaml_file in sorted(scenarios_dir.rglob("*.y*ml")):
        try:
            paths.append(str(yaml_file.relative_to(krkn_repo_path)))
        except ValueError:
            continue
        if limit is not None and len(paths) >= limit:
            break
    return paths


def build_krkn_catalog(
    krkn_repo_path: Path | None = None,
    *,
    max_scenarios: int | None = None,
) -> dict:
    """Discover plugins + scenario files from a local krkn clone.

    Looks at:
      - ``{repo}/krkn/scenario_plugins/*`` (plugin directories)
      - ``{repo}/scenarios/**/*.y*ml`` (example scenario configs)

    Returns ``{"plugins": [...], "scenarios": [...], "source": "repo"|"unavailable",
    "repo_path": "..."}``.
    """
    import os

    path = krkn_repo_path or Path(
        os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))
    )
    plugins = index_plugins_from_repo(path) if path.exists() else []
    scenarios = (
        list_scenario_files(path, limit=max_scenarios) if path.exists() else []
    )

    if plugins:
        return {
            "plugins": plugins,
            "scenarios": scenarios,
            "source": "repo",
            "repo_path": str(path),
        }

    # No static registry — ANALYZE needs a local krkn clone for a live catalog
    return {
        "plugins": [],
        "scenarios": scenarios,
        "source": "unavailable",
        "repo_path": str(path),
    }


def format_krkn_catalog_for_prompt(catalog: dict | None = None) -> str:
    """Render a compact catalog block for ANALYZE / compact-plugin prompts."""
    catalog = catalog or build_krkn_catalog()
    plugins = catalog.get("plugins") or []
    scenarios = catalog.get("scenarios") or []
    source = catalog.get("source", "unknown")
    repo = catalog.get("repo_path", "")

    lines = [
        f"krkn catalog source: {source} ({repo})",
        "Plugin directories under krkn/scenario_plugins/:",
        ", ".join(plugins) if plugins else "(none found)",
    ]
    if scenarios:
        lines.append(f"Example scenario files under scenarios/ ({len(scenarios)} total):")
        # Keep prompt bounded; full list remains on the catalog object for validation
        shown = scenarios[:80]
        for rel in shown:
            lines.append(f"  - {rel}")
        if len(scenarios) > len(shown):
            lines.append(f"  ... and {len(scenarios) - len(shown)} more (use catalog paths only)")
    else:
        lines.append("Example scenario files: (none indexed)")
    return "\n".join(lines)


def read_scenario_yaml(
    relative_path: str | None,
    krkn_repo_path: Path | None = None,
    *,
    max_chars: int = 3000,
) -> str | None:
    """Read a scenario YAML from the local krkn clone for ANALYZE context.

    Restricts reads to files under the krkn repo root. Returns None if the
    path is missing, outside the repo, or unreadable.
    """
    import os

    if not relative_path:
        return None
    path = krkn_repo_path or Path(
        os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))
    )
    rel = relative_path.replace("\\", "/").lstrip("./")
    candidate = (path / rel).resolve()
    try:
        repo_root = path.resolve()
        if repo_root not in candidate.parents and candidate != repo_root:
            return None
    except OSError:
        return None
    if not candidate.is_file():
        return None
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > max_chars:
        return text[:max_chars] + "\n... (truncated)"
    return text


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
