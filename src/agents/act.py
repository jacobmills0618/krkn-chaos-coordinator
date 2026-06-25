"""ACT phase — create GitHub issues or draft PRs for identified gaps."""

import logging
import os
from typing import overload

from src.apis.github_client import GitHubClient, load_project_env
from src.knowledge.scenario_index import scenario_github_url
from src.models import ActionType, Confidence, GapAnalysis

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



PLUGIN_REGISTRY: dict[str, str] = {
    "node_actions": "node_scenarios",
    "hogs": "hog_scenarios",
    "pod_disruption": "pod_disruption_scenarios",
    "network_chaos": "network_chaos_scenarios",
}

def _plugin_path(plugin_dir: str) -> str:
    return f"krkn/scenario_plugins/{plugin_dir}/"

def _scenario_type_from_plugin(plugin: str) -> str:
    """Resolve scenario type from a plugin path or legacy 'name (type)' string."""
    
    if plugin.startswith("krkn/scenario_plugins/"):
        plugin_dir = plugin.removeprefix("krkn/scenario_plugins/").strip("/")
        return PLUGIN_REGISTRY.get(plugin_dir, "pod_disruption_scenarios")
    # legacy fallback during transition
    if " (" in plugin and plugin.endswith(")"):
        return plugin.split(" (", 1)[1].rstrip(")")
    return PLUGIN_REGISTRY.get(plugin, "pod_disruption_scenarios")


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
            _plugin_path("node_actions"),
            "Use `node_stop_start_scenario` or `node_terminate_scenario` with `label_selector: node-role.kubernetes.io/master`. "
            "Note: current node_actions plugin terminates cloud instances — deleting the Node API object may require a new scenario "
            "or use of `cluster_shut_down_scenarios` combined with manual `oc delete node`. "
            "Before writing custom logic, check krkn-lib (`k8s.krkn_kubernetes` for node API operations, "
            "`ocp.krkn_openshift` for Machine/Node readiness checks).",
        )
    if "throttl" in text or "api server load" in text or "resource pressure" in text:
        return (
            "Create resource pressure on API server nodes using CPU/memory hog pods, then verify component health reporting",
            _plugin_path("hogs"),
            "Deploy CPU/memory hog pods on master nodes using `label_selector: node-role.kubernetes.io/master`. "
            "Set `memory` or `cpu` targets high enough to cause API server throttling. "
            "Combine with a health assertion step that checks the target component's operator status. "
            "If extending the hog plugin, use krkn-lib (`k8s.krkn_kubernetes`) for pod deployment and node targeting.",
        )
    if "upgrade" in text or "rollback" in text:
        return (
            "Inject failures during an OCP upgrade to test upgrade resilience",
            _plugin_path("pod_disruption"),
            "Run pod kill scenarios targeting the component's pods during an active upgrade. "
            "Combine with the upgrade Prow workflow (`openshift-qe-upgrade` chain). "
            "Use krkn-lib (`k8s.krkn_kubernetes`) to resolve target pods by label if adding custom kill logic.",
        )
    if "network" in text or "partition" in text or "latency" in text:
        return (
            "Inject network latency or partition between component pods",
            _plugin_path("network_chaos"),
            "Use `tc netem` based network shaping or iptables-based partition. "
            "Target the component's namespace and pods. "
            "Use krkn-lib (`k8s.krkn_kubernetes`) only if you need programmatic pod/namespace discovery.",
        )
    if "quorum" in text or "leader" in text or "etcd" in text:
        return (
            "Disrupt etcd members to test quorum loss and recovery",
            _plugin_path("pod_disruption"),
            "Kill etcd pods in `openshift-etcd` namespace. Verify cluster recovers quorum "
            "and the etcd operator reports correct status within expected time. "
            "For post-chaos checks, use krkn-lib (`ocp.krkn_openshift`) for ClusterOperator/etcd status assertions.",
        )
    return (
        "Inject component-specific failure and verify recovery",
        _plugin_path("pod_disruption"),
        "Target the component's pods in its namespace using label selectors. "
        "Check krkn-lib (`k8s.krkn_kubernetes`, `ocp.krkn_openshift`) before adding custom injection code.",
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
        steps.append("Add the new scenario to `krkn/scenario_plugins/<plugin_dir>/` (under appropriate scenario type)")
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
        steps.append(f"If new plugin code is needed: implement in `{plugin}` and check `krkn-chaos/krkn-lib` for K8s helpers")
        steps.append("If new scenario: follow the plugin creation guide in `CLAUDE.md`")
    else:
        steps.append(f"Design a new chaos scenario for: **{_infer_failure_mode(gap)}**")
        steps.append(f"Suggested plugin: `{plugin}` — {config_hint}")
        steps.append("Check if krkn-lib has the necessary deployment/injection methods")
        steps.append("Create scenario YAML, plugin code (if needed), and tests")

    return steps


def build_issue_body(gap: GapAnalysis, agent_name: str = "coordinator") -> str:
    """Build a detailed GitHub issue body with actionable next steps."""
    effective_agent = gap.agent or agent_name
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
        scenario_url = scenario_github_url(gap.base_scenario)
        if scenario_url:
            lines.append(
                f"The closest existing scenario is [`{gap.base_scenario}`]({scenario_url}). "
            )
        else:
            lines.append(f"The closest existing scenario is `{gap.base_scenario}`. ")
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
    lines.append(
        f"| `krkn-chaos/krkn` | Scenario YAML; register in `krkn/config/config.yaml`; "
        f"plugin code in `{plugin}` if extending injection logic |"
    )
    lines.append(
        "| `krkn-chaos/krkn-lib` | K8s/OpenShift helpers if new API calls are needed |"
    )
    if gap.action_type == ActionType.DRAFT_PR:
        lines.append(f"| `krkn-chaos/krkn-hub` | Container wrapper (Dockerfile, env.sh, run.sh, build_config_file.py) |")
        lines.append(f"| `krkn-chaos/website` | Documentation (Hugo page with krkn/krkn-hub/krknctl tabs) |")
        lines.append(f"| `openshift/release` | Prow CI config (if adding to nightly runs) |")
    lines.append("")

    lines.append("---")
    lines.append(f"*Generated by krkn-chaos-coordinator / {effective_agent} agent*")

    return "\n".join(lines)


def _gap_from_title_args(
    bug_key: str,
    summary: str | None,
    confidence: Confidence | str | int | None,
) -> GapAnalysis:
    """Build a minimal GapAnalysis from legacy title helper arguments."""
    from src.models import Bug

    if isinstance(confidence, Confidence):
        level = confidence
        score = 80 if level == Confidence.HIGH else 50 if level == Confidence.MEDIUM else 20
    elif isinstance(confidence, int):
        score = confidence
        level = (
            Confidence.HIGH if score >= 70
            else Confidence.MEDIUM if score >= 40
            else Confidence.LOW
        )
    elif isinstance(confidence, str):
        level = Confidence(confidence.lower())
        score = 80 if level == Confidence.HIGH else 50 if level == Confidence.MEDIUM else 20
    else:
        level = Confidence.MEDIUM
        score = 50

    bug = Bug(
        key=bug_key,
        summary=summary or "",
        description="",
        component="",
        priority="",
        status="",
        created="",
        url=f"https://issues.redhat.com/browse/{bug_key}",
    )
    return GapAnalysis(bug=bug, confidence_score=score, confidence_level=level)


@overload
def build_issue_title(gap: GapAnalysis) -> str: ...


@overload
def build_issue_title(
    bug_key: str,
    summary: str | None = None,
    confidence: Confidence | str | int | None = None,
) -> str: ...


def build_issue_title(
    gap_or_key: GapAnalysis | str,
    summary: str | None = None,
    confidence: Confidence | str | int | None = None,
) -> str:
    """Build a GitHub issue title from a gap analysis or legacy positional args."""
    if isinstance(gap_or_key, GapAnalysis):
        gap = gap_or_key
    else:
        gap = _gap_from_title_args(gap_or_key, summary, confidence)
    level = gap.confidence_level.value.upper()
    summary = gap.bug.summary[:80]
    return f"[chaos-coordinator] [{level}] {gap.bug.key}: {summary}"


def create_issues_for_gaps(
    github: GitHubClient | None,
    gaps: list[GapAnalysis],
    agent_name: str,
    owner: str | None = None,
    repo: str = "krkn",
    dry_run: bool = True,
) -> list[dict]:
    """Create GitHub issues for each gap.

    Args:
        github: GitHub API client (auto-created from .env when dry_run=False)
        gaps: List of gap analyses to create issues for
        agent_name: Name of the agent that found the gaps
        owner: GitHub repo owner (default: GITHUB_FORK_OWNER or krkn-chaos)
        repo: GitHub repo name
        dry_run: If True, print what would be created without creating

    Returns:
        List of created issue dicts (or dry run previews)
    """
    load_project_env()
    if owner is None:
        owner = os.environ.get("GITHUB_FORK_OWNER", "krkn-chaos")
    if not dry_run and github is None:
        github = GitHubClient.from_env()

    results = []

    for gap in gaps:
        title = build_issue_title(gap)
        body = build_issue_body(gap, agent_name=gap.agent or agent_name)

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
                print(f"  ✓ {gap.bug.key}: {result.get('html_url', 'created')}")
            else:
                logger.error("Failed to create issue for %s", gap.bug.key)
                print(f"  ✗ {gap.bug.key}: failed to create issue")

    return results
