"""ACT phase — create GitHub issues or draft PRs for identified gaps."""

import logging

from src.apis.github_client import GitHubClient
from src.models import ActionType, GapAnalysis

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


def _infer_failure_mode(gap: GapAnalysis) -> str:
    """Infer a human-readable failure mode from the bug."""
    summary = gap.bug.summary.lower()
    desc = gap.bug.description.lower() if gap.bug.description else ""
    text = f"{summary} {desc}"

    if "node delete" in text or "node replace" in text or "same-name" in text:
        return "Node replacement / same-name recreation causes stale state"
    if "throttl" in text or "load" in text or "timeout" in text:
        return "Component degrades or reports incorrect status under resource pressure"
    if "upgrade" in text or "duplicate member" in text:
        return "Upgrade path causes inconsistent cluster state"
    if "quorum" in text or "leader election" in text:
        return "Cluster consensus / leader election failure"
    if "network" in text or "partition" in text:
        return "Network disruption causes component failure"
    if "crash" in text or "restart" in text or "loop" in text:
        return "Component enters crash/restart loop under failure conditions"
    return "Component failure under adverse conditions"


def _infer_injection_method(gap: GapAnalysis) -> tuple[str, str, str]:
    """Infer the krkn injection method, plugin, and how to configure it.

    Returns (method_description, plugin_name, config_hint).
    """
    summary = gap.bug.summary.lower()
    desc = gap.bug.description.lower() if gap.bug.description else ""
    text = f"{summary} {desc}"

    if "node delete" in text or "node replace" in text:
        return (
            "Delete a control-plane node object via Kubernetes API, wait for Machine API to recreate it with the same name",
            "node_actions (node_scenarios)",
            "Use `node_stop_start_scenario` or `node_terminate_scenario` with `label_selector: node-role.kubernetes.io/master`. "
            "Note: current node_actions plugin terminates cloud instances — deleting the Node API object may require a new scenario "
            "or use of `cluster_shut_down_scenarios` combined with manual `oc delete node`.",
        )
    if "throttl" in text or "api server load" in text or "resource pressure" in text:
        return (
            "Create resource pressure on API server nodes using CPU/memory hog pods, then verify component health reporting",
            "hogs (hog_scenarios)",
            "Deploy CPU/memory hog pods on master nodes using `label_selector: node-role.kubernetes.io/master`. "
            "Set `memory` or `cpu` targets high enough to cause API server throttling. "
            "Combine with a health assertion step that checks the target component's operator status.",
        )
    if "upgrade" in text or "rollback" in text:
        return (
            "Inject failures during an OCP upgrade to test upgrade resilience",
            "pod_disruption (pod_disruption_scenarios)",
            "Run pod kill scenarios targeting the component's pods during an active upgrade. "
            "Combine with the upgrade Prow workflow (`openshift-qe-upgrade` chain).",
        )
    if "network" in text or "partition" in text or "latency" in text:
        return (
            "Inject network latency or partition between component pods",
            "network_chaos (network_chaos_scenarios)",
            "Use `tc netem` based network shaping or iptables-based partition. "
            "Target the component's namespace and pods.",
        )
    if "quorum" in text or "leader" in text or "etcd" in text:
        return (
            "Disrupt etcd members to test quorum loss and recovery",
            "pod_disruption (pod_disruption_scenarios)",
            "Kill etcd pods in `openshift-etcd` namespace. Verify cluster recovers quorum "
            "and the etcd operator reports correct status within expected time.",
        )
    return (
        "Inject component-specific failure and verify recovery",
        "pod_disruption (pod_disruption_scenarios)",
        "Target the component's pods in its namespace using label selectors.",
    )


def _build_next_steps(gap: GapAnalysis) -> list[str]:
    """Build concrete next steps for the issue."""
    steps = []
    method_desc, plugin, config_hint = _infer_injection_method(gap)

    if gap.base_scenario and gap.action_type == ActionType.DRAFT_PR:
        steps.append(f"Review the existing scenario at `{gap.base_scenario}` and understand its current configuration")
        steps.append(f"Create a new scenario YAML (or add a variant) that targets: **{_infer_failure_mode(gap)}**")
        steps.append(f"Use the `{plugin}` plugin — {config_hint}")
        steps.append("Add assertions to verify the component reports correct status during/after chaos")
        steps.append("Add the new scenario to `config/config.yaml` under the appropriate scenario type")
        steps.append("Write a unit test in `tests/` if adding new plugin logic")
        steps.append("Create krkn-hub wrapper (Dockerfile, env.sh, run.sh, build_config_file.py) following the standard pattern")
        steps.append("Update krkn-chaos.dev documentation with the new scenario")
        steps.append("Add to Prow CI config in `openshift/release` if needed")
    elif gap.base_scenario:
        steps.append(f"Evaluate whether `{gap.base_scenario}` can be extended or if a new scenario is needed")
        steps.append(f"The failure mode is: **{_infer_failure_mode(gap)}**")
        steps.append(f"Suggested plugin: `{plugin}` — {config_hint}")
        steps.append("Determine if existing krkn-lib methods support this injection, or if new code is needed")
        steps.append("If extending: modify the existing YAML to add a new variant")
        steps.append("If new scenario: follow the plugin creation guide in `CLAUDE.md`")
    else:
        steps.append(f"Design a new chaos scenario for: **{_infer_failure_mode(gap)}**")
        steps.append(f"Suggested plugin: `{plugin}` — {config_hint}")
        steps.append("Check if krkn-lib has the necessary deployment/injection methods")
        steps.append("Create scenario YAML, plugin code (if needed), and tests")

    return steps


def build_issue_body(gap: GapAnalysis, agent_name: str) -> str:
    """Build a detailed GitHub issue body with actionable next steps."""
    lines = []
    failure_mode = _infer_failure_mode(gap)
    method_desc, plugin, config_hint = _infer_injection_method(gap)

    # Header
    lines.append("## Chaos Test Coverage Gap")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| **Bug** | [{gap.bug.key}]({gap.bug.url}) |")
    lines.append(f"| **Component** | {gap.bug.component} |")
    lines.append(f"| **Priority** | {gap.bug.priority} |")
    lines.append(f"| **Confidence** | {gap.confidence_level.value.upper()} ({gap.confidence_score}/100) |")
    lines.append(f"| **Action** | {'Draft PR recommended' if gap.action_type == ActionType.DRAFT_PR else 'Human review needed'} |")
    lines.append("")

    # What happened
    lines.append("### What Happened")
    lines.append("")
    lines.append(gap.bug.summary)
    lines.append("")
    lines.append(f"**JIRA:** [{gap.bug.key}]({gap.bug.url})")
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
    lines.append("This failure mode is not covered by any existing krkn chaos scenario.")
    lines.append("")

    # How to test
    lines.append("### How to Chaos Test This")
    lines.append("")
    lines.append(f"**Injection method:** {method_desc}")
    lines.append("")
    lines.append(f"**krkn plugin:** `{plugin}`")
    lines.append("")
    lines.append(f"**Configuration:** {config_hint}")
    lines.append("")

    # Base scenario
    if gap.base_scenario:
        lines.append("### Related Existing Scenario")
        lines.append("")
        lines.append(f"The closest existing scenario is [`{gap.base_scenario}`](https://github.com/krkn-chaos/krkn/blob/main/{gap.base_scenario}). ")
        lines.append("This scenario tests a related failure mode but does not cover the specific condition described in this bug.")
        lines.append("")

    # Confidence breakdown
    lines.append("### Confidence Breakdown")
    lines.append("")
    lines.append(f"Score: **{gap.confidence_score}/100** ({gap.confidence_level.value.upper()})")
    lines.append("")
    for reason in gap.reasoning.split("; "):
        lines.append(f"- {reason}")
    lines.append("")

    # Generated commands from knowledge base
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
    next_steps = _build_next_steps(gap)
    for i, step in enumerate(next_steps, 1):
        lines.append(f"{i}. {step}")
    lines.append("")

    # Repos to change
    lines.append("### Repos to Update")
    lines.append("")
    lines.append("| Repo | Change |")
    lines.append("|---|---|")
    lines.append(f"| `krkn-chaos/krkn` | New/modified scenario YAML + config registration |")
    if gap.action_type == ActionType.DRAFT_PR:
        lines.append(f"| `krkn-chaos/krkn-hub` | Container wrapper (Dockerfile, env.sh, run.sh, build_config_file.py) |")
        lines.append(f"| `krkn-chaos/website` | Documentation (Hugo page with krkn/krkn-hub/krknctl tabs) |")
        lines.append(f"| `openshift/release` | Prow CI config (if adding to nightly runs) |")
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
