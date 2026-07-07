"""Evaluate ocp-virt keyword filter against JIRA virtualization bugs.

Usage:
    PYTHONPATH=. python -m src.evals.ocp_virt_filter_eval --release 4.21 --max-bugs 100
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from src.agents.registry import discover_agents
from src.apis.jira_client import JiraClient, JiraConfig
from src.coordinator.filter_review import prompt_filter_review, write_filter_review_json
from src.filter.chaos_filter import filter_bugs, filter_domain_bugs, get_filter_keywords

logger = logging.getLogger(__name__)
VIRT_AGENT = "virtualization"


def run_ocp_virt_filter_eval(
    release: str = "4.21",
    max_bugs: int = 100,
    days: int = 60,
    domain_only: bool = False,
) -> dict:
    """Fetch virt component bugs from JIRA and run the keyword filter.

    Returns a report dict with counts, sample results, and full pass/skip lists.
    """
    agents = discover_agents()
    if VIRT_AGENT not in agents:
        raise ValueError(f"Agent '{VIRT_AGENT}' not found in config/agents/")

    agent = agents[VIRT_AGENT]
    jira = JiraClient(
        JiraConfig(
            url=os.environ.get("JIRA_URL", "https://redhat.atlassian.net"),
            username=os.environ.get("JIRA_USERNAME", ""),
            api_token=os.environ.get("JIRA_API_TOKEN", ""),
        )
    )
    bugs = jira.discover_bugs(
        list(agent.components),
        days=days,
        max_results=max_bugs,
        release=release,
        discovery_jql=agent.discovery_jql,
    )
    logger.info("Fetched %d bugs from JIRA for virt components", len(bugs))

    filter_fn = filter_domain_bugs if domain_only else filter_bugs
    relevant, skipped = filter_fn(bugs, agent_name=VIRT_AGENT)

    _, virt_keywords = get_filter_keywords(VIRT_AGENT)
    keyword_hits = sum(
        1 for bug in bugs
        if any(kw.lower() in f"{bug.summary} {bug.description}".lower() for kw in virt_keywords)
    )

    return {
        "release": release,
        "max_bugs": max_bugs,
        "days": days,
        "domain_only": domain_only,
        "components": list(agent.components),
        "total_bugs": len(bugs),
        "keyword_hits": keyword_hits,
        "chaos_relevant": len(relevant),
        "skipped": len(skipped),
        "relevant": relevant,
        "skipped_results": skipped,
    }


def _print_report(report: dict) -> None:
    """Print summary stats and sample PASS/SKIP bugs from an eval report."""
    total = report["total_bugs"]
    mode = "domain-only" if report.get("domain_only") else "chaos filter"
    print(f"\nOCP Virt filter eval ({mode}) — release {report['release']}")
    print(f"Components: {len(report['components'])}  |  Bugs fetched: {total}")
    print(f"Matched ocp-virt keyword in text: {report['keyword_hits']}")
    pass_label = "Domain-relevant (PASS)" if report.get("domain_only") else "Chaos-relevant (PASS)"
    print(f"{pass_label}: {report['chaos_relevant']}  |  Skipped: {report['skipped']}")
    if total:
        print(f"Pass rate: {report['chaos_relevant'] / total:.1%}")
        print(f"Keyword hit rate: {report['keyword_hits'] / total:.1%}")

    print("\n--- Sample PASS (up to 10) ---")
    for result in report["relevant"][:10]:
        print(f"  {result.bug.key}: {result.bug.summary[:70]}")
        print(f"    injection={result.injection_method}, confidence={result.confidence:.0%}")

    print("\n--- Sample SKIP (up to 10) ---")
    for result in report["skipped_results"][:10]:
        print(f"  {result.bug.key}: {result.bug.summary[:70]}")
        print(f"    reason={result.skip_reason}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ocp-virt filter evaluation."""
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Evaluate ocp-virt keyword filter")
    parser.add_argument("--release", default="4.21")
    parser.add_argument("--max-bugs", type=int, default=100)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--domain-only", action="store_true")
    parser.add_argument("--filter-review-json", default=None, metavar="PATH")
    parser.add_argument("--no-filter-review", action="store_true")
    args = parser.parse_args(argv)

    if not os.environ.get("JIRA_API_TOKEN"):
        print("ERROR: JIRA_API_TOKEN required in .env", file=sys.stderr)
        return 1

    report = run_ocp_virt_filter_eval(
        release=args.release,
        max_bugs=args.max_bugs,
        days=args.days,
        domain_only=args.domain_only,
    )
    _print_report(report)

    mode = "domain-only" if report.get("domain_only") else "chaos filter"
    metadata = {
        "release": report["release"],
        "domain_only": report.get("domain_only"),
        "total_bugs": report["total_bugs"],
    }
    if not args.no_filter_review:
        prompt_filter_review(
            passed=report["relevant"],
            skipped=report["skipped_results"],
            export_path=args.filter_review_json,
            title_pass=f"OCP Virt — Filter PASS ({mode})",
            title_skip=f"OCP Virt — Filter SKIP ({mode})",
        )
    elif args.filter_review_json:
        write_filter_review_json(
            args.filter_review_json,
            report["relevant"],
            report["skipped_results"],
            metadata=metadata,
        )
        print(f"\nFilter review saved to {args.filter_review_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
