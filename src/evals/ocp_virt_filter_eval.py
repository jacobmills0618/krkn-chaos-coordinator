"""Evaluate ocp-virt keyword filter against JIRA virtualization component bugs.

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
from src.filter.chaos_filter import filter_bug, get_filter_keywords
from src.models import Bug

logger = logging.getLogger(__name__)

VIRT_AGENT = "virtualization"


def _matches_virt_keyword(bug: Bug, virt_keywords: list[str]) -> bool:
    text = f"{bug.summary} {bug.description}".lower()
    return any(kw.lower() in text for kw in virt_keywords)


def run_ocp_virt_filter_eval(
    release: str = "4.21",
    max_bugs: int = 100,
    days: int = 60,
) -> dict:
    """Fetch virt component bugs from JIRA and run keyword filter."""
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

    _, virt_keywords = get_filter_keywords(VIRT_AGENT)
    skip_keywords, _ = get_filter_keywords(VIRT_AGENT)

    relevant = []
    skipped = []
    keyword_hits = 0

    for bug in bugs:
        if _matches_virt_keyword(bug, virt_keywords):
            keyword_hits += 1
        result = filter_bug(bug, agent_name=VIRT_AGENT)
        if result.chaos_relevant:
            relevant.append(result)
        else:
            skipped.append(result)

    return {
        "release": release,
        "max_bugs": max_bugs,
        "days": days,
        "components": list(agent.components),
        "total_bugs": len(bugs),
        "keyword_hits": keyword_hits,
        "chaos_relevant": len(relevant),
        "skipped": len(skipped),
        "relevant": relevant,
        "skipped_results": skipped,
    }


def _print_report(report: dict) -> None:
    total = report["total_bugs"]
    print(f"\nOCP Virt filter eval — release {report['release']}")
    print(f"Components: {len(report['components'])}")
    print(f"Bugs fetched: {total}")
    print(f"Matched ocp-virt keyword in text: {report['keyword_hits']}")
    print(f"Chaos-relevant (PASS): {report['chaos_relevant']}")
    print(f"Skipped: {report['skipped']}")
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
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Evaluate ocp-virt keyword filter")
    parser.add_argument("--release", default="4.21", help="OCP release (default: 4.21)")
    parser.add_argument("--max-bugs", type=int, default=100, help="Max bugs to fetch")
    parser.add_argument("--days", type=int, default=365, help="Lookback window in days")
    args = parser.parse_args(argv)

    if not os.environ.get("JIRA_API_TOKEN"):
        print("ERROR: JIRA_API_TOKEN required in .env", file=sys.stderr)
        return 1

    report = run_ocp_virt_filter_eval(
        release=args.release,
        max_bugs=args.max_bugs,
        days=args.days,
    )
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
