"""Lightweight orchestrator — spawns agents, deduplicates, presents results."""

import logging

from src.models import AgentResult, GapAnalysis

logger = logging.getLogger(__name__)


def deduplicate_gaps(results: list[AgentResult]) -> list[GapAnalysis]:
    """Deduplicate gaps across all agents by bug key."""
    seen_bugs: dict[str, GapAnalysis] = {}

    for result in results:
        for gap in result.gaps:
            key = gap.bug.key
            if key not in seen_bugs:
                seen_bugs[key] = gap
            else:
                existing = seen_bugs[key]
                if gap.confidence_score > existing.confidence_score:
                    seen_bugs[key] = gap

    deduped = sorted(seen_bugs.values(), key=lambda g: g.confidence_score, reverse=True)
    logger.info("Deduplicated %d gaps from %d agents", len(deduped), len(results))
    return deduped


def format_approval_queue(gaps: list[GapAnalysis]) -> str:
    """Format gaps as a human-readable approval queue."""
    lines = []
    lines.append("=" * 60)
    lines.append("krkn-chaos-coordinator — Approval Queue")
    lines.append("=" * 60)
    lines.append("")

    for i, gap in enumerate(gaps, 1):
        level = gap.confidence_level.value.upper()
        action = gap.action_type.value.replace("_", " ").upper()
        lines.append(f"{i}. [{level} {gap.confidence_score}/100] {action}")
        lines.append(f"   Bug: {gap.bug.key} ({gap.bug.summary[:60]})")
        lines.append(f"   Component: {gap.bug.component}")
        lines.append(f"   Reasoning: {gap.reasoning}")
        if gap.base_scenario:
            lines.append(f"   Base scenario: {gap.base_scenario}")
        if gap.modifications:
            lines.append(f"   Modifications: {', '.join(gap.modifications)}")
        lines.append(f"   → [Approve] [Edit] [Reject]")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_summary(
    results: list[AgentResult],
    discovery_note: str | None = None,
) -> str:
    """Format a summary of all agent results."""
    lines = []
    lines.append("=" * 60)
    lines.append("krkn-chaos-coordinator — Run Summary")
    lines.append("=" * 60)
    lines.append("")
    if discovery_note:
        lines.append(discovery_note)
        lines.append("")

    total_bugs = 0
    total_relevant = 0
    total_skipped = 0
    total_matched = 0
    total_gaps = 0

    for result in results:
        discovered = len(result.bugs_discovered)
        skipped = len(result.bugs_filtered_out)
        relevant = discovered - skipped
        matched = len(result.bugs_matched)
        gaps = len(result.gaps)

        total_bugs += discovered
        total_relevant += relevant
        total_skipped += skipped
        total_matched += matched
        total_gaps += gaps

        lines.append(f"Agent: {result.agent_name}")
        lines.append(f"  Discovered: {discovered} bugs")
        lines.append(f"  Filtered:   {skipped} skipped (not chaos-relevant)")
        lines.append(f"  Relevant:   {relevant}")
        lines.append(f"  Matched:    {matched} (existing coverage)")
        lines.append(f"  Gaps:       {gaps}")
        lines.append("")

    lines.append("-" * 40)
    lines.append(f"TOTAL: {total_bugs} bugs scanned, {total_relevant} chaos-relevant, "
                 f"{total_gaps} gaps identified")
    lines.append("=" * 60)
    return "\n".join(lines)
