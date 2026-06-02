"""Agent registry — auto-discovers domain agents from YAML config files.

Drop a YAML file into config/agents/ to register a new agent.
No code changes needed — the system picks it up automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config" / "agents"


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for a domain agent, loaded from YAML."""
    name: str
    description: str
    components: tuple[str, ...]
    docs: tuple[dict, ...] = ()


def _load_agent_config(path: Path) -> AgentConfig:
    """Parse a single agent YAML file into an AgentConfig."""
    with open(path) as f:
        data = yaml.safe_load(f)

    name = data.get("name")
    if not name:
        raise ValueError(f"{path.name}: missing required 'name' field")

    components = data.get("components", [])
    if not components:
        raise ValueError(f"{path.name}: 'components' list is empty")

    docs_raw = data.get("docs", [])
    docs = []
    for d in docs_raw:
        if not isinstance(d, dict):
            continue
        doc_type = d.get("type", "github")
        if doc_type == "github" and all(k in d for k in ("owner", "repo", "path")):
            docs.append({"type": "github", "owner": d["owner"], "repo": d["repo"], "path": d["path"]})
        elif doc_type == "local" and "path" in d:
            docs.append({"type": "local", "path": d["path"]})
        elif doc_type == "url" and "url" in d:
            docs.append({"type": "url", "url": d["url"]})
        elif doc_type == "github" and all(k in d for k in ("owner", "repo")):
            docs.append({"type": "github", "owner": d["owner"], "repo": d["repo"], "path": d.get("path", "")})
    docs = tuple(docs)

    return AgentConfig(
        name=name,
        description=data.get("description", ""),
        components=tuple(components),
        docs=docs,
    )


def discover_agents(config_dir: Path | None = None) -> dict[str, AgentConfig]:
    """Scan config/agents/*.yaml and return registered agents.

    Returns:
        Dict of agent_name → AgentConfig, sorted by name.
    """
    directory = config_dir or CONFIG_DIR

    if not directory.is_dir():
        logger.warning("Agent config directory not found: %s", directory)
        return {}

    agents: dict[str, AgentConfig] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            config = _load_agent_config(path)
            if config.name in agents:
                logger.warning(
                    "Duplicate agent name '%s' in %s (already loaded), skipping",
                    config.name, path.name,
                )
                continue
            agents[config.name] = config
            logger.debug("Registered agent: %s (%d components)", config.name, len(config.components))
        except Exception as e:
            logger.error("Failed to load agent config %s: %s", path.name, e)

    logger.info("Discovered %d agents from %s", len(agents), directory)
    return agents


def get_agent_names(config_dir: Path | None = None) -> list[str]:
    """Return sorted list of all registered agent names."""
    return sorted(discover_agents(config_dir).keys())
