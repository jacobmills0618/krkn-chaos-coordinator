"""Main entry point for krkn-chaos-coordinator."""

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.agents.base_agent import BaseDomainAgent
from src.agents.registry import discover_agents
from src.apis.jira_client import JiraClient, JiraConfig
from src.apis.sippy_client import SippyClient
from src.apis.github_client import GitHubClient
from src.coordinator.orchestrator import deduplicate_gaps, format_approval_queue, format_summary
from src.knowledge.chromadb_store import ChromaStore
from src.knowledge.scenario_index import index_scenarios_from_repo

LOG_FILE = "krkn-chaos-coordinator.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    filename=LOG_FILE,
    filemode="w",
)
logger = logging.getLogger(__name__)


def main():
    load_dotenv()

    registered = discover_agents()
    agent_names_str = ", ".join(sorted(registered.keys()))

    parser = argparse.ArgumentParser(description="krkn-chaos-coordinator")
    parser.add_argument(
        "--release", default="4.21",
        help="OCP release(s) to analyze. Comma-separated for multiple (e.g. '4.20,4.21'). Default: 4.21",
    )
    parser.add_argument(
        "--agent", default=None,
        help=(
            f"Agent(s) to run. Comma-separated for multiple (e.g. 'control_plane,networking'). "
            f"'all' or omit for all agents. Available: {agent_names_str}"
        ),
    )
    parser.add_argument(
        "--max-bugs", type=int, default=2000, help="Max bugs per agent from JIRA (default: 2000)"
    )
    parser.add_argument(
        "--days", type=int, default=14, help="Look back N days for bugs (default: 14)"
    )
    parser.add_argument(
        "--use-llm", action="store_true", default=False,
        help="Enable LLM-enhanced filter/map/analyze (uses tiered model routing)",
    )
    parser.add_argument(
        "--krkn-repo",
        default=os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn")),
        help="Path to local krkn repo (env: KRKN_REPO_PATH)",
    )
    parser.add_argument(
        "--refresh-docs", action="store_true", default=False,
        help="Re-ingest ChromaDB knowledge base before running (pulls latest docs from GitHub)",
    )
    parser.add_argument(
        "--parallel", action="store_true", default=False,
        help="Run agents in parallel (faster, requires stable Neo4j connection)",
    )
    args = parser.parse_args()

    # Initialize API clients
    jira = JiraClient(
        JiraConfig(
            url=os.environ.get("JIRA_URL", "https://redhat.atlassian.net"),
            username=os.environ.get("JIRA_USERNAME", ""),
            api_token=os.environ.get("JIRA_API_TOKEN", ""),
        )
    )
    sippy = SippyClient()
    github = GitHubClient(token=os.environ.get("GITHUB_TOKEN", ""))

    # Refresh docs if requested
    if args.refresh_docs:
        from src.status import status_done
        from src.knowledge.ingest import run_full_ingestion
        status_done("coordinator", "DISCOVER", "refreshing ChromaDB knowledge base...")
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print("ERROR: GITHUB_TOKEN required for --refresh-docs")
            return
        results = run_full_ingestion(token, "./chroma_data")
        status_done("coordinator", "DISCOVER", f"ingested {results['total']} chunks")

    # Initialize knowledge layer
    chroma = ChromaStore(persist_dir="./chroma_data")
    scenarios = index_scenarios_from_repo(Path(args.krkn_repo))

    logger.info("Indexed %d scenarios from %s", len(scenarios), args.krkn_repo)

    # Parse releases and agents
    releases = [r.strip() for r in args.release.split(",") if r.strip()]
    logger.info("Target release(s): %s", ", ".join(releases))

    if args.agent and args.agent.lower() != "all":
        agent_names = [a.strip() for a in args.agent.split(",") if a.strip()]
        unknown = [a for a in agent_names if a not in registered]
        if unknown:
            print(f"Unknown agent(s): {', '.join(unknown)}. Available: {agent_names_str}")
            return
    else:
        agent_names = sorted(registered.keys())

    logger.info("Agent(s): %s", ", ".join(agent_names))

    # Connect Neo4j (required — no JSON fallback)
    from src.knowledge.neo4j_store import Neo4jStore
    neo4j_store = Neo4jStore(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
    )
    if not neo4j_store.connect():
        logger.error(
            "Neo4j is required. Start it with: podman start neo4j-coordinator"
        )
        return
    logger.info("Neo4j connected — REMEMBER phase will use knowledge graph")

    # Run each agent × release combination
    all_results = []

    def _run_agent(agent_name: str, release: str) -> 'AgentResult':
        agent_kwargs = {
            "jira": jira,
            "sippy": sippy,
            "github": github,
            "chroma": chroma,
            "scenarios": scenarios,
            "release": release,
            "neo4j_store": neo4j_store,
            "use_llm": args.use_llm,
        }
        agent = BaseDomainAgent(agent_name=agent_name, **agent_kwargs)
        return agent.run()

    if args.parallel and len(agent_names) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from src.status import status_done
        status_done("coordinator", "DISCOVER", f"running {len(agent_names)} agents in parallel")

        tasks = []
        with ThreadPoolExecutor(max_workers=min(len(agent_names), 4)) as pool:
            for release in releases:
                for agent_name in agent_names:
                    tasks.append(pool.submit(_run_agent, agent_name, release))

            for future in as_completed(tasks):
                try:
                    all_results.append(future.result())
                except Exception as e:
                    logger.error("Agent failed: %s", e)
    else:
        for release in releases:
            for agent_name in agent_names:
                all_results.append(_run_agent(agent_name, release))

    neo4j_store.close()

    # Orchestrator: deduplicate and format
    gaps = deduplicate_gaps(all_results)

    print(format_summary(all_results))
    print()
    if gaps:
        print(format_approval_queue(gaps))
        _prompt_github_issues(gaps, github)
    else:
        print("No chaos test coverage gaps identified.")


def _prompt_github_issues(gaps: list, github: GitHubClient) -> None:
    """Prompt the user to select which gaps to act on (draft PR or issue)."""
    from src.agents.act import create_issues_for_gaps
    from src.models import ActionType

    print("\n" + "=" * 60)
    print("Post gaps as GitHub issues / draft PRs?")
    print("  (HIGH + MAP match → draft PR when enabled; else issue)")
    print("=" * 60)
    print()
    for i, gap in enumerate(gaps, 1):
        level = gap.confidence_level.value.upper()
        kind = (
            "draft PR"
            if gap.action_type == ActionType.DRAFT_PR and gap.base_scenario
            else "issue"
        )
        print(
            f"  {i}. [{level} {gap.confidence_score}/100] "
            f"{gap.bug.key}: {gap.bug.summary[:60]}  ({kind})"
        )
    print()
    print("  Enter numbers to post (e.g., '1,3'), 'all', or 'none':")

    try:
        choice = input("  → ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped.")
        return

    if choice in ("none", "n", ""):
        print("  Skipped.")
        return

    if choice == "all":
        selected = list(range(len(gaps)))
    else:
        try:
            selected = [int(x.strip()) - 1 for x in choice.split(",")]
            selected = [i for i in selected if 0 <= i < len(gaps)]
        except ValueError:
            print("  Invalid input. Skipped.")
            return

    if not selected:
        print("  No valid selections. Skipped.")
        return

    selected_gaps = [gaps[i] for i in selected]
    owner = os.environ.get("GITHUB_FORK_OWNER", "krkn-chaos")

    # Default: try draft PRs for eligible gaps, but dry-run until confirmed.
    dry_run = True
    try_draft_pr = True
    if any(
        g.action_type == ActionType.DRAFT_PR and g.base_scenario
        for g in selected_gaps
    ):
        print()
        print("  Draft PR mode: [d]ry-run (default) / [y]es create / [i]ssues only")
        try:
            pr_choice = input("  → ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipped.")
            return
        if pr_choice in ("y", "yes"):
            dry_run = False
        elif pr_choice in ("i", "issue", "issues"):
            try_draft_pr = False
            dry_run = False
    else:
        print()
        print("  Create issues for real? [y/N]")
        try:
            if input("  → ").strip().lower() in ("y", "yes"):
                dry_run = False
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipped.")
            return

    try:
        print(f"\n  Creating action(s) on {owner}/krkn...")
        results = create_issues_for_gaps(
            github,
            selected_gaps,
            agent_name="coordinator",
            owner=owner,
            dry_run=dry_run,
            try_draft_pr=try_draft_pr,
        )
        for result in results:
            bug_key = result.get("bug_key", "?")
            url = result.get("html_url")
            if result.get("dry_run"):
                files = result.get("files")
                print(f"  ○ {bug_key}: dry-run" + (f" files={files}" if files else ""))
            elif url:
                print(f"  ✓ {bug_key}: {url}")
            else:
                print(f"  ✓ {bug_key}: created")
    except KeyboardInterrupt:
        print("\n  Issue creation interrupted.")


if __name__ == "__main__":
    main()
