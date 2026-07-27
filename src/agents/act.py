"""ACT phase — create GitHub issues or draft PRs for identified gaps."""

import logging
import os
from pathlib import Path

from src.apis.github_client import GitHubClient
from src.models import ActionType, CONFIDENCE_FACTOR_LABELS, CONFIDENCE_FACTOR_LOW_DEFAULTS, GapAnalysis

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

    if "kubevirt" in text or "virt-launcher" in text or "vm migration" in text:
        return "KubeVirt VM disruption causes workload failure"
    if "managedcluster" in text or "klusterlet" in text or "multicluster" in text:
        return "Managed cluster disconnect causes hub visibility loss"
    if "zone outage" in text or "availability zone" in text:
        return "Availability zone outage causes regional infrastructure failure"
    if "cluster shut down" in text or "cluster shutdown" in text:
        return "Full cluster shutdown causes extended unavailability"
    if "clock skew" in text or "time skew" in text or "ntp drift" in text:
        return "Clock skew causes time-sensitive component misbehavior"
    if "storage throttle" in text or "iops limit" in text or "disk throttle" in text:
        return "Storage I/O throttling causes degraded disk performance"
    if "disk full" in text or "out of space" in text or "pvc full" in text:
        return "PVC exhaustion causes out-of-disk failure"
    if "syn flood" in text or "syn-flood" in text or "hping" in text:
        return "SYN flood attack overwhelms target service"
    if "service hijack" in text or "service hijacking" in text:
        return "Service hijacking causes incorrect responses to clients"
    if "service disruption" in text:
        return "Service disruption causes application unavailability"
    if "application outage" in text or "route inaccessible" in text:
        return "Application route outage blocks ingress/egress traffic"
    if "http load" in text or "load test" in text or "request rate" in text:
        return "HTTP load saturation causes endpoint degradation"
    if "container kill" in text or "kill container" in text:
        return "Container kill disrupts pod workload"
    if "pod network" in text or "network filter" in text or "interface down" in text:
        return "Pod-level network filtering causes connectivity degradation"
    if "node delete" in text or "node replace" in text or "same-name" in text:
        return "Node replacement / same-name recreation causes stale state"
    if "throttl" in text or "api server load" in text or "resource pressure" in text:
        return "Component degrades or reports incorrect status under resource pressure"
    if "upgrade" in text or "rollback" in text or "duplicate member" in text:
        return "Upgrade path causes inconsistent cluster state"
    if "network" in text or "partition" in text or "latency" in text:
        return "Network disruption causes component failure"
    if "quorum" in text or "leader" in text or "etcd" in text:
        return "Cluster consensus / leader election failure"
    if "crash" in text or "restart" in text or "loop" in text:
        return "Component enters crash/restart loop under failure conditions"
    return "Component failure under adverse conditions"


# Plugin directory -> config.yaml scenario_type (krkn/scenario_plugins/<dir>/)
# See https://krkn-chaos.dev/docs/scenarios/
PLUGIN_REGISTRY: dict[str, str] = {
    "application_outage": "application_outages_scenarios",
    "container": "container_scenarios",
    "hogs": "hog_scenarios",
    "http_load": "http_load_scenarios",
    "kubevirt_vm_outage": "kubevirt_vm_outage",
    "managed_cluster": "managedcluster_scenarios",
    "network_chaos": "network_chaos_scenarios",
    "network_chaos_ng": "network_chaos_ng_scenarios",
    "node_actions": "node_scenarios",
    "pod_disruption": "pod_disruption_scenarios",
    "pvc": "pvc_scenarios",
    "service_disruption": "service_disruption_scenarios",
    "service_hijacking": "service_hijacking_scenarios",
    "shut_down": "cluster_shut_down_scenarios",
    "storage_throttle": "storage_throttle_scenarios",
    "syn_flood": "syn_flood_scenarios",
    "time_actions": "time_scenarios",
    "zone_outage": "zone_outages_scenarios",
}

_SCENARIO_TYPE_TO_PLUGIN: dict[str, str] = {
    scenario_type: plugin_dir
    for plugin_dir, scenario_type in PLUGIN_REGISTRY.items()
}

# One-line injection descriptions (shared by MAP/ANALYZE and keyword fallback)
_PLUGIN_METHODS: dict[str, str] = {
    "hogs": "Create resource pressure (CPU/memory/IO hog) and verify component health",
    "network_chaos": "Inject network latency, loss, or partition against target pods",
    "network_chaos_ng": "Apply pod-level network filters (latency, loss, bandwidth)",
    "node_actions": "Disrupt or recreate a node and verify cluster recovery",
    "pod_disruption": "Disrupt component pods and verify recovery",
    "container": "Kill or disrupt a specific container within a pod",
    "kubevirt_vm_outage": "Disrupt a KubeVirt VM and verify recovery",
    "application_outage": "Block ingress/egress to application routes",
    "pvc": "Fill or stress a PVC to test out-of-disk handling",
    "storage_throttle": "Throttle storage I/O to simulate degraded disk performance",
    "managed_cluster": "Disrupt a ManagedCluster or klusterlet and verify recovery",
    "zone_outage": "Simulate an availability zone outage",
    "shut_down": "Shut down cluster nodes and verify recovery on restart",
    "time_actions": "Inject clock skew on pods or nodes",
    "syn_flood": "Launch a SYN flood against a target service",
    "service_hijacking": "Hijack a Kubernetes service to return error responses",
    "service_disruption": "Disrupt services in a namespace",
    "http_load": "Generate HTTP load against target endpoints",
}

# Example YAML when MAP base_scenario is a different plugin type
_PLUGIN_EXAMPLES: dict[str, str] = {
    "hogs": "scenarios/kube/cpu-hog.yml",
    "container": "scenarios/openshift/container.yml",
    "network_chaos": "scenarios/openshift/network-chaos.yml",
    "pod_disruption": "scenarios/openshift/etcd.yml",
    "node_actions": "scenarios/openshift/node_scenarios_example.yml",
    "kubevirt_vm_outage": "scenarios/kubevirt/kubevirt-vm-outage.yaml",
}

# First match wins (keyword substring → plugin dir)
_KEYWORD_TO_PLUGIN: tuple[tuple[tuple[str, ...], str], ...] = (
    (("kubevirt", "virt-launcher", "vm migration"), "kubevirt_vm_outage"),
    (("managedcluster", "klusterlet", "multicluster"), "managed_cluster"),
    (("zone outage", "availability zone"), "zone_outage"),
    (("cluster shut down", "cluster shutdown"), "shut_down"),
    (("clock skew", "time skew", "ntp drift"), "time_actions"),
    (("storage throttle", "iops limit", "disk throttle"), "storage_throttle"),
    (("disk full", "out of space", "pvc full"), "pvc"),
    (("syn flood", "syn-flood", "hping"), "syn_flood"),
    (("service hijack", "service hijacking"), "service_hijacking"),
    (("service disruption",), "service_disruption"),
    (("application outage", "route inaccessible"), "application_outage"),
    (("http load", "load test", "request rate"), "http_load"),
    (("container kill", "kill container"), "container"),
    (("pod network", "network filter", "interface down"), "network_chaos_ng"),
    (("node delete", "node replace"), "node_actions"),
    (("throttl", "api server load", "resource pressure"), "hogs"),
    (("upgrade", "rollback"), "pod_disruption"),
    (("network", "partition", "latency"), "network_chaos"),
    (("quorum", "leader", "etcd"), "pod_disruption"),
)

_FILTER_STYLE_KEYWORDS = (
    "network", "hog", "cpu", "memory", "node", "kubevirt", "vm",
    "pvc", "storage", "time", "zone", "flood", "hijack", "container",
)
_POD_DISRUPTION_INTENTIONAL = (
    "etcd", "quorum", "leader", "upgrade", "rollback", "duplicate member",
)


def _plugin_path(plugin_dir: str) -> str:
    return f"krkn/scenario_plugins/{plugin_dir}/"


def _plugin_dir(plugin: str) -> str:
    return plugin.removeprefix("krkn/scenario_plugins/").strip("/")


def _scenario_type_from_plugin(plugin: str) -> str:
    """Resolve scenario type from a plugin path or legacy 'name (type)' string."""
    if plugin.startswith("krkn/scenario_plugins/"):
        return PLUGIN_REGISTRY.get(_plugin_dir(plugin), "pod_disruption_scenarios")
    if " (" in plugin and plugin.endswith(")"):
        return plugin.split(" (", 1)[1].rstrip(")")
    return PLUGIN_REGISTRY.get(plugin, "pod_disruption_scenarios")


def normalize_krkn_plugin(value: str | None) -> str | None:
    """Normalize ANALYZE / free-text plugin names to ``krkn/scenario_plugins/<dir>/``."""
    if not value:
        return None
    raw = value.strip().strip("`").strip()
    if not raw:
        return None

    if raw.startswith("krkn/scenario_plugins/"):
        plugin_dir = raw.removeprefix("krkn/scenario_plugins/").strip("/").split("/")[0]
        return _plugin_path(plugin_dir) if plugin_dir in PLUGIN_REGISTRY else None

    if raw in PLUGIN_REGISTRY:
        return _plugin_path(raw)
    if raw in _SCENARIO_TYPE_TO_PLUGIN:
        return _plugin_path(_SCENARIO_TYPE_TO_PLUGIN[raw])

    lowered = raw.lower().replace("-", "_")
    for plugin_dir, scenario_type in PLUGIN_REGISTRY.items():
        aliases = {plugin_dir, scenario_type, scenario_type.replace("_scenarios", "")}
        if lowered in aliases or plugin_dir in lowered or scenario_type in lowered:
            return _plugin_path(plugin_dir)
    return None


def _plugin_from_base_scenario(base_scenario: str) -> str | None:
    """Derive plugin path from a MAP ``base_scenario`` file path."""
    rel = base_scenario.replace("\\", "/")
    stem = Path(rel).stem.lower().replace("-", "_")
    path_l = rel.lower()

    try:
        krkn_path = Path(os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn")))
        if krkn_path.exists():
            from src.knowledge.scenario_index import index_scenarios_from_repo

            for info in index_scenarios_from_repo(krkn_path):
                if info.file_path == rel or info.file_path.endswith(rel):
                    return (
                        normalize_krkn_plugin(info.plugin_name)
                        or normalize_krkn_plugin(info.scenario_type)
                        or (
                            _plugin_path(_SCENARIO_TYPE_TO_PLUGIN[info.scenario_type])
                            if info.scenario_type in _SCENARIO_TYPE_TO_PLUGIN
                            else None
                        )
                    )
    except Exception as e:
        logger.debug("base_scenario index lookup failed: %s", e)

    for plugin_dir in PLUGIN_REGISTRY:
        if plugin_dir in path_l or plugin_dir.replace("_", "-") in path_l:
            return _plugin_path(plugin_dir)

    # Filename heuristics when index is unavailable
    if "hog" in stem:
        return _plugin_path("hogs")
    if "kubevirt" in path_l or "vm_outage" in stem:
        return _plugin_path("kubevirt_vm_outage")
    if "network" in stem:
        return _plugin_path("network_chaos")
    if any(t in stem for t in ("etcd", "pod", "kill")):
        return _plugin_path("pod_disruption")
    if "node" in stem:
        return _plugin_path("node_actions")
    if "pvc" in stem:
        return _plugin_path("pvc")
    if "storage" in stem:
        return _plugin_path("storage_throttle")
    return None


def base_scenario_matches_plugin(
    base_scenario: str | None, plugin: str | None,
) -> bool:
    """True when MAP ``base_scenario`` is the same krkn plugin as ``plugin``."""
    if not base_scenario or not plugin:
        return False
    mapped = _plugin_from_base_scenario(base_scenario)
    if not mapped:
        return False
    return normalize_krkn_plugin(mapped) == normalize_krkn_plugin(plugin)


def _method_for_plugin(plugin: str) -> str:
    plugin_dir = _plugin_dir(plugin)
    return _PLUGIN_METHODS.get(
        plugin_dir, f"Use the `{plugin_dir}` krkn plugin to inject the failure mode",
    )


def _hint_for_plugin(plugin: str, base_scenario: str | None) -> str:
    """Config hint for MAP/ANALYZE plugins; warn when MAP file is a different plugin."""
    scenario_type = _scenario_type_from_plugin(plugin)
    example = _PLUGIN_EXAMPLES.get(_plugin_dir(plugin))
    example_bit = f" Use `{example}` as a shape reference." if example else ""

    if base_scenario and base_scenario_matches_plugin(base_scenario, plugin):
        return (
            f"Start from `{base_scenario}` (scenario type `{scenario_type}`). "
            "Adapt selectors/namespace for this bug's component, then register in "
            "`config/config.yaml` if adding a new file."
        )

    if base_scenario:
        base_plugin = _plugin_from_base_scenario(base_scenario)
        base_dir = _plugin_dir(base_plugin) if base_plugin else "unknown"
        base_type = (
            _scenario_type_from_plugin(base_plugin) if base_plugin else "unknown"
        )
        return (
            f"Author or extend a scenario under `{scenario_type}` using plugin "
            f"`{plugin}`.{example_bit} "
            f"MAP closest file `{base_scenario}` is a **different** plugin "
            f"(`{base_dir}` / `{base_type}`) — reuse namespace/selectors only; "
            "do not copy it as the starter template for this plugin."
        )

    return (
        f"Author or extend a scenario under `{scenario_type}` using plugin `{plugin}`."
        f"{example_bit} Match label selectors to the bug's component namespace."
    )


def _infer_injection_method(gap: GapAnalysis) -> tuple[str, str, str]:
    """Keyword fallback: (method_description, plugin_path, config_hint)."""
    text = f"{gap.bug.summary} {gap.bug.description or ''}".lower()
    plugin_dir = "pod_disruption"
    for keywords, mapped in _KEYWORD_TO_PLUGIN:
        if any(kw in text for kw in keywords):
            plugin_dir = mapped
            break

    plugin = _plugin_path(plugin_dir)
    hint = (
        f"Configure a `{_scenario_type_from_plugin(plugin)}` scenario with "
        f"plugin `{plugin}`; match selectors to the bug's component."
    )
    return _method_for_plugin(plugin), plugin, hint


def resolve_injection_method(
    gap: GapAnalysis,
) -> tuple[str, str, str, str]:
    """Pick injection method for issue/PR text.

    Preference: ANALYZE plugin → MAP base_scenario → FILTER hint → keyword fallback.
    Returns (method_description, plugin_path, config_hint, source_label).
    """
    analyzed = normalize_krkn_plugin(gap.krkn_plugin)
    if analyzed:
        return (
            _method_for_plugin(analyzed),
            analyzed,
            _hint_for_plugin(analyzed, gap.base_scenario),
            "ANALYZE (gap.krkn_plugin)",
        )

    if gap.base_scenario:
        mapped = _plugin_from_base_scenario(gap.base_scenario)
        if mapped:
            return (
                _method_for_plugin(mapped),
                mapped,
                _hint_for_plugin(mapped, gap.base_scenario),
                f"MAP match ({gap.base_scenario})",
            )

    if gap.filter_injection_method:
        from dataclasses import replace

        filter_gap = replace(
            gap,
            bug=replace(
                gap.bug,
                summary=f"{gap.filter_injection_method}: {gap.bug.summary}",
            ),
        )
        method_desc, plugin, config_hint = _infer_injection_method(filter_gap)
        intentional = any(
            kw in gap.filter_injection_method.lower()
            for kw in _FILTER_STYLE_KEYWORDS
        )
        if intentional or not plugin.rstrip("/").endswith("pod_disruption"):
            return method_desc, plugin, config_hint, "FILTER injection_method"

    method_desc, plugin, config_hint = _infer_injection_method(gap)
    text = f"{gap.bug.summary} {gap.bug.description or ''}".lower()
    if plugin.rstrip("/").endswith("pod_disruption"):
        if any(kw in text for kw in _POD_DISRUPTION_INTENTIONAL):
            return method_desc, plugin, config_hint, "keyword heuristic"
        return (
            method_desc,
            plugin,
            f"{config_hint} Generic pod_disruption fallback — review before implementing.",
            "keyword default (low confidence — review before implementing)",
        )
    return method_desc, plugin, config_hint, "keyword heuristic"


def _build_next_steps(gap: GapAnalysis) -> list[str]:
    """Build concrete next steps for the issue."""
    steps = []
    method_desc, plugin, config_hint, _source = resolve_injection_method(gap)

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
    method_desc, plugin, config_hint, plugin_source = resolve_injection_method(gap)

    # Header
    lines.append("## Chaos Test Coverage Gap")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| **Bug** | [{gap.bug.key}]({gap.bug.url}) |")
    lines.append(f"| **Component** | {gap.bug.component} |")
    lines.append(f"| **Priority** | {gap.bug.priority} |")
    lines.append(f"| **Confidence** | {gap.confidence_level.value.upper()} ({gap.confidence_score}/100) |")
    for field_name, label in CONFIDENCE_FACTOR_LABELS:
        level = getattr(gap, field_name).value.upper()
        lines.append(f"| **{label}** | {level} |")
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
    lines.append(f"**Plugin source:** {plugin_source}")
    lines.append("")
    lines.append(f"**Configuration:** {config_hint}")
    lines.append("")

    # Base scenario
    if gap.base_scenario:
        lines.append("### Related Existing Scenario")
        lines.append("")
        lines.append(
            f"The closest existing scenario is "
            f"[`{gap.base_scenario}`](https://github.com/krkn-chaos/krkn/blob/main/{gap.base_scenario}). "
        )
        if not base_scenario_matches_plugin(gap.base_scenario, plugin):
            base_plugin = _plugin_from_base_scenario(gap.base_scenario)
            base_dir = _plugin_dir(base_plugin) if base_plugin else "unknown"
            chosen_dir = _plugin_dir(plugin)
            lines.append(
                f"This MAP match uses the **`{base_dir}`** plugin, which differs from the "
                f"recommended **`{chosen_dir}`** plugin above. Treat it as component context "
                "(namespace/selectors) only — not as the YAML template to copy."
            )
        else:
            lines.append(
                "This scenario tests a related failure mode but does not cover the "
                "specific condition described in this bug."
            )
        lines.append("")

    # Confidence breakdown — one line per factor: label, HIGH/LOW, and why
    lines.append("### Confidence Breakdown")
    lines.append("")
    lines.append(f"Score: **{gap.confidence_score}/100** ({gap.confidence_level.value.upper()})")
    lines.append("")
    reason_map = dict(gap.confidence_factor_reasons)
    for field_name, label in CONFIDENCE_FACTOR_LABELS:
        level = getattr(gap, field_name).value.upper()
        why = reason_map.get(field_name) or CONFIDENCE_FACTOR_LOW_DEFAULTS.get(
            field_name, ""
        )
        if why:
            lines.append(f"- **{label}:** {level} — {why}")
        else:
            lines.append(f"- **{label}:** {level}")
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
    try_draft_pr: bool = False,
) -> list[dict]:
    """Create GitHub issues for each gap.

    When ``try_draft_pr`` is True, HIGH-confidence gaps with a MAP
    ``base_scenario`` attempt ``create_scenario_pr`` first and fall back to an
    issue if the PR cannot be created.

    Args:
        github: GitHub API client
        gaps: List of gap analyses to create issues for
        agent_name: Name of the agent that found the gaps
        owner: GitHub repo owner
        repo: GitHub repo name
        dry_run: If True, print what would be created without creating
        try_draft_pr: If True, prefer draft scenario PRs when eligible

    Returns:
        List of created issue/PR dicts (or dry run previews)
    """
    results = []

    for gap in gaps:
        if (
            try_draft_pr
            and gap.action_type == ActionType.DRAFT_PR
            and gap.base_scenario
        ):
            analyzed = normalize_krkn_plugin(gap.krkn_plugin)
            if analyzed and not base_scenario_matches_plugin(gap.base_scenario, analyzed):
                logger.warning(
                    "Skipping draft PR for %s: MAP base_scenario %s does not match "
                    "ANALYZE plugin %s — creating issue instead",
                    gap.bug.key,
                    gap.base_scenario,
                    analyzed,
                )
            else:
                from src.agents.pr_creator import create_scenario_pr

                try:
                    pr_result = create_scenario_pr(github, gap, dry_run=dry_run)
                except Exception as e:
                    logger.error(
                        "Draft PR failed for %s (%s); falling back to issue",
                        gap.bug.key,
                        e,
                    )
                    pr_result = None

                if pr_result:
                    results.append({**pr_result, "bug_key": gap.bug.key})
                    continue
                logger.warning(
                    "Draft PR unavailable for %s — creating GitHub issue instead",
                    gap.bug.key,
                )

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
