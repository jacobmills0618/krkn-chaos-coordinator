"""Interactive and programmatic filter review after virt scans."""

from __future__ import annotations

from src.coordinator.filter_review import (
    collect_filter_results,
    format_filter_pass_list,
    format_filter_skip_list,
    write_filter_review_json,
)
from src.models import AgentResult, FilterResult


def prompt_filter_review(
    results: list[AgentResult],
    *,
    agent_name: str = "virtualization",
    export_path: str | None = None,
) -> None:
    """Ask the user to view full PASS and/or SKIP filter lists."""
    passed, skipped = collect_filter_results(results, agent_name=agent_name)
    if not passed and not skipped:
        print("\nNo filter results to review.")
        return

    metadata = {
        "agent": agent_name,
        "passed": len(passed),
        "skipped": len(skipped),
    }
    if export_path:
        path = write_filter_review_json(export_path, passed, skipped, metadata=metadata)
        print(f"\nFilter review saved to {path}")

    print("\n" + "=" * 60)
    print("Filter review — validate PASS / SKIP results")
    print("=" * 60)
    print(f"  PASS: {len(passed)}  |  SKIP: {len(skipped)}")
    print()
    print("  What would you like to see?")
    print("    1. All PASS bugs (with filter confidence)")
    print("    2. All SKIP bugs (with skip reasons)")
    print("    3. Both")
    print("    4. Skip review")
    print()

    try:
        choice = input("  → ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped.")
        return

    if choice in ("4", "skip", "none", "n", ""):
        print("  Skipped.")
        return

    if choice in ("1", "pass", "passed"):
        print(format_filter_pass_list(passed))
    elif choice in ("2", "skip", "skipped"):
        print(format_filter_skip_list(skipped))
    elif choice in ("3", "both", "all"):
        print(format_filter_pass_list(passed))
        print()
        print(format_filter_skip_list(skipped))
    else:
        print("  Invalid input. Skipped.")


def prompt_filter_review_from_results(
    passed: list[FilterResult],
    skipped: list[FilterResult],
    *,
    export_path: str | None = None,
    title_pass: str = "OCP Virt — Filter PASS",
    title_skip: str = "OCP Virt — Filter SKIP",
) -> None:
    """Filter review prompt when pass/skip lists are already available (eval script)."""
    if export_path:
        path = write_filter_review_json(
            export_path,
            passed,
            skipped,
            metadata={"passed": len(passed), "skipped": len(skipped)},
        )
        print(f"\nFilter review saved to {path}")

    print("\n" + "=" * 60)
    print("Filter review — validate PASS / SKIP results")
    print("=" * 60)
    print(f"  PASS: {len(passed)}  |  SKIP: {len(skipped)}")
    print()
    print("  What would you like to see?")
    print("    1. All PASS bugs (with filter confidence)")
    print("    2. All SKIP bugs (with skip reasons)")
    print("    3. Both")
    print("    4. Skip review")
    print()

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
