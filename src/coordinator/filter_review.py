"""Format and export FILTER pass/skip lists for manual review."""

from __future__ import annotations

import json
from pathlib import Path

from src.models import AgentResult, FilterResult


def format_filter_pass_list(
    passed: list[FilterResult],
    *,
    title: str = "OCP Virt — Filter PASS",
) -> str:
    """Human-readable list of bugs that passed the filter."""
    lines = ["=" * 60, title, "=" * 60, ""]
    if not passed:
        lines.append("(none)")
        lines.append("=" * 60)
        return "\n".join(lines)

    sorted_passed = sorted(passed, key=lambda r: r.confidence, reverse=True)
    lines.append(f"Total: {len(sorted_passed)} bugs")
    lines.append("")

    for i, result in enumerate(sorted_passed, 1):
        bug = result.bug
        lines.append(f"{i}. [{result.confidence:.0%}] {bug.key}")
        lines.append(f"   {bug.summary[:90]}")
        if result.failure_mode:
            lines.append(f"   Failure mode: {result.failure_mode}")
        if result.injection_method:
            lines.append(f"   Injection: {result.injection_method}")
        lines.append(f"   {bug.url}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_filter_skip_list(
    skipped: list[FilterResult],
    *,
    title: str = "OCP Virt — Filter SKIP",
) -> str:
    """Human-readable list of bugs skipped by the filter."""
    lines = ["=" * 60, title, "=" * 60, ""]
    if not skipped:
        lines.append("(none)")
        lines.append("=" * 60)
        return "\n".join(lines)

    sorted_skipped = sorted(skipped, key=lambda r: r.bug.key)
    lines.append(f"Total: {len(sorted_skipped)} bugs")
    lines.append("")

    for i, result in enumerate(sorted_skipped, 1):
        bug = result.bug
        lines.append(f"{i}. [{result.confidence:.0%}] {bug.key}")
        lines.append(f"   {bug.summary[:90]}")
        lines.append(f"   Skip reason: {result.skip_reason or 'unknown'}")
        lines.append(f"   {bug.url}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def collect_filter_results(
    results: list[AgentResult],
    agent_name: str | None = None,
) -> tuple[list[FilterResult], list[FilterResult]]:
    """Merge pass/skip FilterResults from one or more agent runs."""
    passed: list[FilterResult] = []
    skipped: list[FilterResult] = []
    seen_pass: set[str] = set()
    seen_skip: set[str] = set()

    for result in results:
        if agent_name and result.agent_name != agent_name:
            continue
        for fr in result.bugs_passed_filter:
            if fr.bug.key not in seen_pass:
                passed.append(fr)
                seen_pass.add(fr.bug.key)
        for fr in result.bugs_filtered_out:
            if fr.bug.key not in seen_skip:
                skipped.append(fr)
                seen_skip.add(fr.bug.key)

    return passed, skipped


def filter_results_to_dict(
    passed: list[FilterResult],
    skipped: list[FilterResult],
    *,
    metadata: dict | None = None,
) -> dict:
    """Serialize pass/skip lists for JSON export or Claude review."""

    def _row(result: FilterResult, *, outcome: str) -> dict:
        return {
            "key": result.bug.key,
            "summary": result.bug.summary,
            "component": result.bug.component,
            "url": result.bug.url,
            "confidence": round(result.confidence, 2),
            "outcome": outcome,
            "failure_mode": result.failure_mode,
            "injection_method": result.injection_method,
            "skip_reason": result.skip_reason,
        }

    return {
        "metadata": metadata or {},
        "passed": [_row(r, outcome="pass") for r in passed],
        "skipped": [_row(r, outcome="skip") for r in skipped],
        "counts": {"passed": len(passed), "skipped": len(skipped)},
    }


def write_filter_review_json(
    path: str | Path,
    passed: list[FilterResult],
    skipped: list[FilterResult],
    *,
    metadata: dict | None = None,
) -> Path:
    """Write pass/skip review data to a JSON file."""
    out = Path(path)
    payload = filter_results_to_dict(passed, skipped, metadata=metadata)
    out.write_text(json.dumps(payload, indent=2))
    return out
