"""Format, export, and prompt for FILTER pass/skip review."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from src.models import AgentResult, Bug, FilterResult

_REVIEW_MENU = (
    "\n" + "=" * 60 + "\n"
    "Filter review — validate PASS / SKIP results\n"
    + "=" * 60 + "\n"
    "  PASS: {passed}  |  SKIP: {skipped}\n\n"
    "  What would you like to see?\n"
    "    1. All PASS bugs (with filter confidence)\n"
    "    2. All SKIP bugs (with skip reasons)\n"
    "    3. Both\n"
    "    4. Skip review\n"
)


def _format_filter_list(
    results: list[FilterResult],
    *,
    title: str,
    outcome: str,
) -> str:
    """Format PASS or SKIP filter results as a human-readable numbered list."""
    lines = ["=" * 60, title, "=" * 60, ""]
    if not results:
        lines.extend(["(none)", "=" * 60])
        return "\n".join(lines)

    ordered = (
        sorted(results, key=lambda r: r.confidence, reverse=True)
        if outcome == "pass"
        else sorted(results, key=lambda r: r.bug.key)
    )
    lines.extend([f"Total: {len(ordered)} bugs", ""])

    for i, result in enumerate(ordered, 1):
        bug = result.bug
        lines.append(f"{i}. [{result.confidence:.0%}] {bug.key}")
        lines.append(f"   {bug.summary[:90]}")
        if outcome == "pass":
            if result.failure_mode:
                lines.append(f"   Failure mode: {result.failure_mode}")
            if result.injection_method:
                lines.append(f"   Injection: {result.injection_method}")
        else:
            lines.append(f"   Skip reason: {result.skip_reason or 'unknown'}")
        lines.append(f"   {bug.url}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_neo4j_relevance_inventory(
    total: int,
    by_filter_agent: list[dict],
    by_component: list[dict],
    *,
    property_name: str,
    top_components: int = 15,
) -> str:
    """Format historical Neo4j inventory for virt_relevant or chaos_relevant."""
    if property_name not in ("virt_relevant", "chaos_relevant"):
        raise ValueError(f"Unsupported property: {property_name}")

    if property_name == "virt_relevant":
        headline = "Neo4j inventory — bugs with virt_relevant=true"
        meaning = (
            "(domain / ocp-virt keyword matches only — NOT chaos-tested relevance; "
            "NOT the same as this-run Filter PASS)"
        )
    else:
        headline = "Neo4j inventory — bugs with chaos_relevant=true"
        meaning = (
            "(passed chaos/injection filter — NOT domain-only; "
            "NOT the same as this-run Filter PASS)"
        )

    lines = [
        "=" * 60,
        headline,
        meaning,
        "=" * 60,
        "",
        f"Total stored: {total} bugs",
        "",
        "By last_filter_agent:",
    ]
    if by_filter_agent:
        for row in by_filter_agent:
            lines.append(f"  {row['filter_agent']}: {row['bugs']}")
    else:
        lines.append("  (none)")

    lines.extend(["", "By JIRA component:"])
    if by_component:
        for row in by_component[:top_components]:
            lines.append(f"  {row['component']}: {row['bugs']}")
        if len(by_component) > top_components:
            lines.append(f"  … and {len(by_component) - top_components} more components")
    else:
        lines.append("  (none)")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_neo4j_virt_relevant_inventory(
    total: int,
    by_filter_agent: list[dict],
    by_component: list[dict],
    *,
    top_components: int = 15,
) -> str:
    """Format historical Neo4j virt_relevant inventory (domain filter)."""
    return format_neo4j_relevance_inventory(
        total,
        by_filter_agent,
        by_component,
        property_name="virt_relevant",
        top_components=top_components,
    )


def format_neo4j_chaos_relevant_inventory(
    total: int,
    by_filter_agent: list[dict],
    by_component: list[dict],
    *,
    top_components: int = 15,
) -> str:
    """Format historical Neo4j chaos_relevant inventory (chaos filter)."""
    return format_neo4j_relevance_inventory(
        total,
        by_filter_agent,
        by_component,
        property_name="chaos_relevant",
        top_components=top_components,
    )


def format_filter_pass_list(
    passed: list[FilterResult],
    *,
    title: str = "Filter PASS (this run only)",
) -> str:
    """Human-readable list of bugs that passed the filter this run."""
    return _format_filter_list(passed, title=title, outcome="pass")


def format_filter_skip_list(
    skipped: list[FilterResult],
    *,
    title: str = "Filter SKIP (this run only)",
) -> str:
    """Human-readable list of bugs skipped by the filter this run."""
    return _format_filter_list(skipped, title=title, outcome="skip")


def collect_filter_results(
    results: list[AgentResult],
    agent_name: str | None = None,
) -> tuple[list[FilterResult], list[FilterResult]]:
    """Merge pass/skip FilterResults from one or more agent runs, deduplicated by bug key."""
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
    """Write pass/skip review data to a JSON file and return the output path."""
    out = Path(path)
    out.write_text(json.dumps(filter_results_to_dict(passed, skipped, metadata=metadata), indent=2))
    return out


def load_filter_review_json(path: str | Path) -> tuple[list[FilterResult], list[FilterResult]]:
    """Load pass/skip lists written by write_filter_review_json."""
    data = json.loads(Path(path).read_text())

    def _row_to_result(row: dict) -> FilterResult:
        return FilterResult(
            bug=Bug(
                key=row["key"],
                summary=row["summary"],
                description="",
                component=row["component"],
                priority="",
                status="",
                created="",
                url=row["url"],
            ),
            chaos_relevant=row["outcome"] == "pass",
            failure_mode=row.get("failure_mode"),
            injection_method=row.get("injection_method"),
            skip_reason=row.get("skip_reason"),
            confidence=row["confidence"],
        )

    passed = [_row_to_result(r) for r in data.get("passed", [])]
    skipped = [_row_to_result(r) for r in data.get("skipped", [])]
    return passed, skipped


def prompt_filter_review(
    *,
    results: list[AgentResult] | None = None,
    passed: list[FilterResult] | None = None,
    skipped: list[FilterResult] | None = None,
    agent_name: str | None = "virtualization",
    export_path: str | None = None,
    title_pass: str = "Filter PASS (this run only)",
    title_skip: str = "Filter SKIP (this run only)",
) -> None:
    """Interactive PASS/SKIP review from agent runs or pre-built lists."""
    if results is not None:
        passed, skipped = collect_filter_results(results, agent_name=agent_name)
    else:
        passed = passed or []
        skipped = skipped or []

    if not passed and not skipped:
        print("\nNo filter results to review.")
        return

    metadata = {"agent": agent_name, "passed": len(passed), "skipped": len(skipped)}
    if export_path:
        print(f"\nFilter review saved to {write_filter_review_json(export_path, passed, skipped, metadata=metadata)}")

    if not sys.stdin.isatty():
        print(
            "\nNon-interactive terminal — skipping filter review menu. "
            "Use krkn-chaos-scan Batch 3 (AskUserQuestion) or re-run in a TTY without --no-filter-review."
        )
        return

    print(_REVIEW_MENU.format(passed=len(passed), skipped=len(skipped)))

    try:
        choice = input("  → ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped.")
        return

    if choice in ("4", "skip", "none", "n", ""):
        print("  Skipped.")
        return
    if choice in ("1", "pass", "passed"):
        print(format_filter_pass_list(passed, title=title_pass))
    elif choice in ("2", "skip", "skipped"):
        print(format_filter_skip_list(skipped, title=title_skip))
    elif choice in ("3", "both", "all"):
        print(format_filter_pass_list(passed, title=title_pass))
        print()
        print(format_filter_skip_list(skipped, title=title_skip))
    else:
        print("  Invalid input. Skipped.")
