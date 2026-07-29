"""ACT phase — create GitHub issues or draft PRs for identified gaps."""

import logging

from src.apis.github_client import GitHubClient
from src.knowledge.scenario_index import scenario_github_url
from src.models import (
    ActionType,
    CONFIDENCE_FACTOR_LABELS,
    CONFIDENCE_FACTOR_LOW_DEFAULTS,
    GapAnalysis,
)

logger = logging.getLogger(__name__)

# Lazy-loaded knowledge base for validated command generation
_kb = None


def _get_knowledgebase():
    """Lazy-load the scenario knowledge base."""
    global _kb
    if _kb is None:
        try:
            from src.knowledge.scenario_knowledgebase import ScenarioKnowledgeBase
            _kb = ScenarioKnowledgeBase()
            logger.info("Scenario knowledge base loaded")
        except Exception as e:
            logger.warning("Knowledge base not available: %s", e)
    return _kb

LABEL = "chaos-coordinator"


def resolve_injection_method(
    gap: GapAnalysis,
) -> tuple[str, str, str, str]:
    """Pass through ANALYZE → FILTER fields for issue text.

    Returns (injection_method, krkn_plugin, configuration, source_label).
    """
    plugin = (gap.krkn_plugin or "").strip()
    injection = (gap.injection_method or gap.filter_injection_method or "").strip()
    configuration = (gap.configuration or "").strip()
    mods = [m.strip() for m in gap.modifications if isinstance(m, str) and m.strip()]

    if not configuration:
        parts = []
        if gap.starter_scenario:
            parts.append(f"Start from `{gap.starter_scenario}`.")
        if gap.related_map_note:
            parts.append(gap.related_map_note)
        elif gap.base_scenario and plugin:
            parts.append(
                f"MAP closest file: `{gap.base_scenario}` "
                "(confirm it matches the ANALYZE plugin before copying)."
            )
        if mods:
            parts.append(mods[0])
        configuration = " ".join(parts)

    if plugin:
        return (
            injection or "(see Analysis / Next Steps)",
            plugin,
            configuration or f"See ANALYZE modifications for `{plugin}`.",
            "ANALYZE",
        )
    if gap.base_scenario:
        return (
            injection or "(see Analysis / Next Steps)",
            "(none — ANALYZE did not recommend a plugin)",
            configuration or f"MAP closest scenario: `{gap.base_scenario}`.",
            "MAP",
        )
    if injection:
        return (injection, "(none)", configuration or injection, "FILTER")
    return (
        "(none)",
        "(none)",
        configuration or "No plugin or injection method from ANALYZE/MAP/FILTER.",
        "none",
    )


def build_issue_body(gap: GapAnalysis, agent_name: str) -> str:
    """Build a detailed GitHub issue body with actionable next steps."""
    lines = []
    failure_mode = (gap.failure_mode or "").strip() or gap.bug.summary
    method_desc, plugin, config_hint, plugin_source = resolve_injection_method(gap)
    low_confidence = gap.confidence_score < 40
    has_factors = bool(gap.confidence_factor_reasons)

    # Header
    lines.append("## Chaos Test Coverage Gap")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| **Bug** | [{gap.bug.key}]({gap.bug.url}) |")
    lines.append(f"| **Component** | {gap.bug.component} |")
    lines.append(f"| **Priority** | {gap.bug.priority} |")
    lines.append(f"| **Confidence** | {gap.confidence_level.value.upper()} ({gap.confidence_score}/100) |")
    if has_factors:
        for field_name, label in CONFIDENCE_FACTOR_LABELS:
            level = getattr(gap, field_name).value.upper()
            lines.append(f"| **{label}** | {level} |")
    lines.append(f"| **Action** | {'Draft PR recommended' if gap.action_type == ActionType.DRAFT_PR else 'Human review needed'} |")
    lines.append("")

    # What happened
    lines.append("### What Happened")
    lines.append("")
    lines.append(gap.bug.summary)
    lines.append("")
    # Include first 500 chars of description if available
    if gap.bug.description and len(gap.bug.description) > 50:
        desc_preview = gap.bug.description[:500].replace("\n", " ").strip()
        lines.append(f"> {desc_preview}{'...' if len(gap.bug.description) > 500 else ''}")
        lines.append("")

    # Failure mode
    lines.append("### Failure Mode")
    lines.append("")
    lines.append(f"**{failure_mode}**")
    lines.append("")
    if gap.base_scenario:
        lines.append(
            f"Closest existing coverage: `{gap.base_scenario}` — related, but does not "
            "cover this specific failure mode."
        )
    else:
        lines.append("This failure mode is not covered by any existing krkn chaos scenario.")
    lines.append("")

    # Recommendations only for MEDIUM/HIGH (LOW = gap description only)
    if not low_confidence:
        lines.append("### How to Chaos Test This")
        lines.append("")
        lines.append(f"**Injection method:** {method_desc}")
        lines.append("")
        lines.append(f"**krkn plugin:** `{plugin}`")
        lines.append("")
        lines.append(f"**Plugin source:** {plugin_source}")
        lines.append("")
        lines.append(f"**Configuration:** {config_hint}")
        lines.append("")

    if gap.reasoning:
        lines.append("### Analysis")
        lines.append("")
        lines.append(gap.reasoning)
        lines.append("")

    # Base scenario
    if gap.base_scenario and not low_confidence:
        lines.append("### Related Existing Scenario")
        lines.append("")
        scenario_url = scenario_github_url(gap.base_scenario)
        if scenario_url:
            lines.append(
                f"The closest existing scenario is [`{gap.base_scenario}`]({scenario_url}). "
            )
        else:
            lines.append(f"The closest existing scenario is `{gap.base_scenario}`. ")
        note = (gap.related_map_note or "").strip()
        if note:
            lines.append(note)
        else:
            lines.append(
                "This scenario tests a related failure mode but does not cover the "
                "specific condition described in this bug."
            )
        lines.append("")

    # Confidence breakdown — one line per factor: label, HIGH/LOW, and why
    lines.append("### Confidence Breakdown")
    lines.append("")
    lines.append(f"Score: **{gap.confidence_score}/100** ({gap.confidence_level.value.upper()})")
    lines.append("")
    if has_factors:
        reason_map = dict(gap.confidence_factor_reasons)
        for field_name, label in CONFIDENCE_FACTOR_LABELS:
            level = getattr(gap, field_name).value.upper()
            why = reason_map.get(field_name) or CONFIDENCE_FACTOR_LOW_DEFAULTS.get(
                field_name, ""
            )
            if why:
                lines.append(f"- **{label}:** {level} — {why}")
            else:
                lines.append(f"- **{label}:** {level}")
        lines.append("")
    else:
        lines.append("Per-factor confidence was not assessed for this gap.")
        lines.append("")

    # Generated commands from knowledge base (MEDIUM/HIGH only)
    if not low_confidence:
        kb = _get_knowledgebase()
        if kb:
            try:
                from src.generator.scenario_generator import generate_issue_section
                generated = generate_issue_section(gap, kb)
                if generated:
                    lines.append(generated)
            except Exception as e:
                logger.warning("Scenario generation failed for %s: %s", gap.bug.key, e)

    # Next steps
    lines.append("### Next Steps")
    lines.append("")
    if low_confidence:
        next_steps = [
            "Track this coverage gap; confidence is too low for an implementation recommendation.",
        ]
    else:
        next_steps = [
            m.strip() for m in gap.modifications if isinstance(m, str) and m.strip()
        ]
        if not next_steps:
            next_steps = [
                "Review the Analysis section and design a scenario using the "
                f"recommended plugin (`{plugin}`)."
            ]
    for i, step in enumerate(next_steps, 1):
        lines.append(f"{i}. {step}")
    lines.append("")

    # Repos to change (MEDIUM/HIGH only)
    if not low_confidence:
        lines.append("### Repos to Update")
        lines.append("")
        lines.append("| Repo | Change |")
        lines.append("|---|---|")
        lines.append(
            f"| `krkn-chaos/krkn` | Scenario YAML under `{plugin}`; register in "
            "`krkn/config/config.yaml`; extend plugin code there if needed |"
        )
        lines.append(
            "| `krkn-chaos/krkn-lib` | K8s/OpenShift helpers if new API calls are needed |"
        )
        if gap.action_type == ActionType.DRAFT_PR:
            lines.append(
                "| `krkn-chaos/krkn-hub` | Container wrapper "
                "(Dockerfile, env.sh, run.sh, build_config_file.py) |"
            )
            lines.append(
                "| `krkn-chaos/website` | Documentation "
                "(Hugo page with krkn/krkn-hub/krknctl tabs) |"
            )
            lines.append(
                "| `openshift/release` | Prow CI config (if adding to nightly runs) |"
            )
        lines.append("")

    lines.append("---")
    lines.append(f"*Generated by krkn-chaos-coordinator / {agent_name} agent*")

    return "\n".join(lines)


def build_issue_title(gap: GapAnalysis) -> str:
    """Build a GitHub issue title from a gap analysis."""
    level = gap.confidence_level.value.upper()
    summary = gap.bug.summary[:80]
    return f"[chaos-coordinator] [{level}] {gap.bug.key}: {summary}"


def create_issues_for_gaps(
    github: GitHubClient,
    gaps: list[GapAnalysis],
    agent_name: str,
    owner: str = "krkn-chaos",
    repo: str = "krkn",
    dry_run: bool = True,
) -> list[dict]:
    """Create GitHub issues for each gap.

    Args:
        github: GitHub API client
        gaps: List of gap analyses to create issues for
        agent_name: Name of the agent that found the gaps
        owner: GitHub repo owner
        repo: GitHub repo name
        dry_run: If True, print what would be created without creating

    Returns:
        List of created issue dicts (or dry run previews)
    """
    results = []

    for gap in gaps:
        title = build_issue_title(gap)
        body = build_issue_body(gap, agent_name)

        if dry_run:
            logger.info("DRY RUN — would create issue:")
            logger.info("  Title: %s", title)
            logger.info("  Repo: %s/%s", owner, repo)
            results.append({
                "dry_run": True,
                "title": title,
                "body": body,
                "bug_key": gap.bug.key,
                "confidence": gap.confidence_level.value,
            })
            print(f"\n{'='*60}")
            print(f"ISSUE PREVIEW: {title}")
            print(f"{'='*60}")
            print(body)
            print(f"{'='*60}\n")
        else:
            result = github.create_issue(
                owner=owner,
                repo=repo,
                title=title,
                body=body,
                labels=[LABEL],
            )
            if result:
                logger.info("Created issue: %s", result.get("html_url"))
                results.append(result)
            else:
                logger.error("Failed to create issue for %s", gap.bug.key)

    return results
