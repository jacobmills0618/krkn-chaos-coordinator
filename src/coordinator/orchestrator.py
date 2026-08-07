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


def format_summary(results: list[AgentResult]) -> str:
    """Format a summary of all agent results.

    PASS/SKIP counts come from bugs filtered **this run** (new bugs only).
    Known bugs already in Neo4j are not re-filtered and must not be reported
    as domain/filter matches for the current agent selection.
    """
    lines = []
    lines.append("=" * 60)
    lines.append("krkn-chaos-coordinator — Run Summary")
    lines.append("=" * 60)
    lines.append("")

    total_bugs = 0
    total_relevant = 0
    total_skipped = 0
    total_known = 0
    total_matched = 0
    total_gaps = 0
    total_llm_calls = 0
    total_analyze_sec = 0.0
    total_cost = 0.0

    for result in results:
        discovered = len(result.bugs_discovered)
        skipped = len(result.bugs_filtered_out)
        relevant = len(result.bugs_passed_filter)
        filtered_this_run = relevant + skipped
        known = max(0, discovered - filtered_this_run)
        matched = len(result.bugs_matched)
        gaps = len(result.gaps)
        cost_str = f"${result.cost_usd:.2f}" if result.cost_usd > 0 else "free"

        total_bugs += discovered
        total_relevant += relevant
        total_skipped += skipped
        total_known += known
        total_matched += matched
        total_gaps += gaps
        total_llm_calls += result.llm_calls
        total_analyze_sec += result.analyze_duration_sec
        total_cost += result.cost_usd

        lines.append(f"Agent: {result.agent_name}")
        lines.append(f"  Discovered: {discovered} bugs ({filtered_this_run} new filtered this run, {known} known)")
        lines.append(f"  Passed:     {relevant} (this run only)")
        lines.append(f"  Skipped:    {skipped} (this run only)")
        lines.append(f"  Matched:    {matched} (existing coverage)")
        lines.append(f"  Gaps:       {gaps}")
        lines.append(f"  LLM calls:  {result.llm_calls}")
        lines.append(f"  ANALYZE:    {result.analyze_duration_sec}s")
        lines.append(f"  Cost:       {cost_str}")
        lines.append("")

    total_cost_str = f"${total_cost:.2f}" if total_cost > 0 else "free"
    lines.append("-" * 40)
    lines.append(
        f"TOTAL this run: {total_bugs} discovered, {total_relevant} passed filter, "
        f"{total_skipped} skipped, {total_known} known (not re-filtered), "
        f"{total_gaps} gaps, {total_llm_calls} LLM calls, "
        f"ANALYZE {round(total_analyze_sec, 2)}s, cost={total_cost_str}"
    )
    lines.append("=" * 60)
    return "\n".join(lines)
