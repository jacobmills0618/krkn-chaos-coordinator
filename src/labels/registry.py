"""Label category registry — pluggable JIRA label discovery categories from YAML."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config" / "labels"
_CATEGORY_ID_SUFFIX = re.compile(r"\(([^)]+)\)\s*$")


@dataclass(frozen=True)
class LabelCategoryConfig:
    """Configuration for a JIRA label discovery category."""
    name: str
    description: str
    label_substrings: tuple[str, ...]
    prompt_label: str | None = None


def _load_label_category_config(path: Path) -> LabelCategoryConfig:
    with open(path) as f:
        data = yaml.safe_load(f)

    name = data.get("name")
    if not name:
        raise ValueError(f"{path.name}: missing required 'name' field")

    substrings_raw = data.get("label_substrings", [])
    if not substrings_raw:
        raise ValueError(f"{path.name}: 'label_substrings' list is empty")

    label_substrings = tuple(str(s).strip() for s in substrings_raw if str(s).strip())
    if not label_substrings:
        raise ValueError(f"{path.name}: 'label_substrings' list is empty")

    prompt_label = data.get("prompt_label")
    if prompt_label is not None:
        prompt_label = str(prompt_label).strip() or None

    return LabelCategoryConfig(
        name=name,
        description=data.get("description", ""),
        label_substrings=label_substrings,
        prompt_label=prompt_label,
    )


def format_label_category_prompt_option(config: LabelCategoryConfig) -> str:
    """Build an AskUserQuestion option label for a label category."""
    if config.prompt_label:
        return config.prompt_label
    title = config.name.replace("_", " ").title()
    blurb = config.description.strip() or title
    return f"{title} — {blurb} ({config.name})"


def _load_fixed_label_prompt_labels(config_dir: Path | None = None) -> dict[str, str]:
    path = (config_dir or CONFIG_DIR) / "scan_prompt.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return {str(k): str(v) for k, v in (data.get("fixed_labels") or {}).items()}


def discover_label_categories(
    config_dir: Path | None = None,
) -> dict[str, LabelCategoryConfig]:
    """Scan config/labels/*.yaml and return registered label categories."""
    directory = config_dir or CONFIG_DIR

    if not directory.is_dir():
        logger.warning("Label category config directory not found: %s", directory)
        return {}

    categories: dict[str, LabelCategoryConfig] = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "scan_prompt.yaml":
            continue
        try:
            config = _load_label_category_config(path)
            if config.name in categories:
                logger.warning(
                    "Duplicate label category '%s' in %s (already loaded), skipping",
                    config.name, path.name,
                )
                continue
            categories[config.name] = config
        except Exception as e:
            logger.error("Failed to load label category %s: %s", path.name, e)

    logger.info("Discovered %d label categories from %s", len(categories), directory)
    return categories


def list_label_category_prompt_options(config_dir: Path | None = None) -> list[str]:
    """AskUserQuestion options for label category multi-select."""
    directory = config_dir or CONFIG_DIR
    categories = discover_label_categories(directory)
    fixed = _load_fixed_label_prompt_labels(directory)
    return [
        fixed[name] if name in fixed else format_label_category_prompt_option(cfg)
        for name, cfg in sorted(categories.items())
    ]


def parse_category_ids_from_prompt_options(options: list[str]) -> list[str]:
    """Extract category IDs from AskUserQuestion option labels."""
    ids: list[str] = []
    for option in options:
        match = _CATEGORY_ID_SUFFIX.search(option.strip())
        if match:
            ids.append(match.group(1))
    return ids


def resolve_label_substrings(
    category_ids: list[str],
    config_dir: Path | None = None,
) -> tuple[str, ...]:
    """Merge label substrings from the selected category IDs (deduplicated, sorted)."""
    categories = discover_label_categories(config_dir)
    merged: set[str] = set()
    for category_id in category_ids:
        config = categories.get(category_id)
        if not config:
            raise ValueError(f"Unknown label category: {category_id}")
        merged.update(config.label_substrings)
    return tuple(sorted(merged))


def format_label_category_display_name(
    category_id: str,
    config_dir: Path | None = None,
) -> str:
    """Human-readable category name for disclaimers and logs."""
    categories = discover_label_categories(config_dir)
    config = categories.get(category_id)
    if not config:
        return category_id
    if config.prompt_label:
        return config.prompt_label.rsplit("(", 1)[0].strip() or category_id
    title = config.name.replace("_", " ").title()
    return title


def format_label_discovery_disclaimer(
    category_ids: list[str],
    config_dir: Path | None = None,
) -> str:
    """Disclaimer printed when label discovery backfill is enabled."""
    names = [
        f"{format_label_category_display_name(cid, config_dir)} ({cid})"
        for cid in category_ids
    ]
    joined = ", ".join(names)
    return (
        f"DISCOVERY NOTE: JIRA bug search included label discovery using: {joined}. "
        "Component-based search also ran for each agent. "
        'For component-only discovery, select "No" for label discovery.'
    )


def list_label_discovery_ask_options(config_dir: Path | None = None) -> list[str]:
    """AskUserQuestion options for label discovery (mirrors agent Input flow).

    Two buttons only: skip label backfill, or prompt user to type category ID(s)
    in chat. Category list belongs in the question text, not as button options.
    """
    del config_dir  # options are fixed; categories listed in question text
    return [
        "No — component search only (Recommended)",
        "Input Label Category(s)",
    ]


def format_label_categories_bullet_list(config_dir: Path | None = None) -> str:
    """Bullet list of category_id + label for AskUserQuestion question text."""
    bullets: list[str] = []
    for label in list_label_category_prompt_options(config_dir):
        match = _CATEGORY_ID_SUFFIX.search(label.strip())
        category_id = match.group(1) if match else label
        short = label.rsplit("(", 1)[0].strip() or category_id
        bullets.append(f"- {category_id} — {short}")
    return "\n".join(bullets)
