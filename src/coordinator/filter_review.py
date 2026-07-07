"""Format, export, and prompt for FILTER pass/skip review."""

from __future__ import annotations

import json
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


def format_filter_pass_list(
    passed: list[FilterResult],
    *,
    title: str = "OCP Virt — Filter PASS",
) -> str:
    """Human-readable list of bugs that passed the filter."""
    return _format_filter_list(passed, title=title, outcome="pass")


def format_filter_skip_list(
    skipped: list[FilterResult],
    *,
    title: str = "OCP Virt — Filter SKIP",
) -> str:
    """Human-readable list of bugs skipped by the filter."""
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
    agent_name: str = "virtualization",
    export_path: str | None = None,
    title_pass: str = "OCP Virt — Filter PASS",
    title_skip: str = "OCP Virt — Filter SKIP",
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
