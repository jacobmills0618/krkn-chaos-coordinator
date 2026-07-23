"""ACT phase — create GitHub issues or draft PRs for identified gaps."""

import logging
import os
from pathlib import Path

from src.apis.github_client import GitHubClient
from src.knowledge.scenario_index import scenario_github_url
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

# Reverse map: config.yaml scenario_type → plugin directory
_SCENARIO_TYPE_TO_PLUGIN: dict[str, str] = {
    scenario_type: plugin_dir
    for plugin_dir, scenario_type in PLUGIN_REGISTRY.items()
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


def normalize_krkn_plugin(value: str | None) -> str | None:
    """Normalize ANALYZE / free-text plugin names to ``krkn/scenario_plugins/<dir>/``."""
    if not value:
        return None
    raw = value.strip().strip("`").strip()
    if not raw:
        return None

    if raw.startswith("krkn/scenario_plugins/"):
        plugin_dir = raw.removeprefix("krkn/scenario_plugins/").strip("/")
        if plugin_dir in PLUGIN_REGISTRY:
            return _plugin_path(plugin_dir)
        # keep first path segment if nested
        plugin_dir = plugin_dir.split("/")[0]
        if plugin_dir in PLUGIN_REGISTRY:
            return _plugin_path(plugin_dir)
        return None

    # Exact plugin dir or scenario type
    if raw in PLUGIN_REGISTRY:
        return _plugin_path(raw)
    if raw in _SCENARIO_TYPE_TO_PLUGIN:
        return _plugin_path(_SCENARIO_TYPE_TO_PLUGIN[raw])

    # Common LLM variants: "hogs (hog_scenarios)", "cpu_hog_scenarios", "network-chaos"
    lowered = raw.lower().replace("-", "_")
    for plugin_dir, scenario_type in PLUGIN_REGISTRY.items():
        if lowered in {plugin_dir, scenario_type, scenario_type.replace("_scenarios", "")}:
            return _plugin_path(plugin_dir)
        if plugin_dir in lowered or scenario_type in lowered:
            return _plugin_path(plugin_dir)

    return None


def _plugin_from_base_scenario(base_scenario: str) -> str | None:
    """Derive plugin path from a MAP ``base_scenario`` file path."""
    rel = base_scenario.replace("\\", "/")
    stem = Path(rel).stem.lower().replace("-", "_")
    path_l = rel.lower()

    # Prefer scenario index when local krkn is available
    try:
        krkn_path = Path(os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn")))
        if krkn_path.exists():
            from src.knowledge.scenario_index import index_scenarios_from_repo

            for info in index_scenarios_from_repo(krkn_path):
                if info.file_path == rel or info.file_path.endswith(rel):
                    plugin = normalize_krkn_plugin(info.plugin_name) or normalize_krkn_plugin(
                        info.scenario_type
                    )
                    if plugin:
                        return plugin
                    mapped = _SCENARIO_TYPE_TO_PLUGIN.get(info.scenario_type)
                    if mapped:
                        return _plugin_path(mapped)
    except Exception as e:
        logger.debug("base_scenario index lookup failed: %s", e)

    # Path / filename heuristics (no YAML parse)
    for plugin_dir in PLUGIN_REGISTRY:
        token = plugin_dir.replace("_", "")
        if plugin_dir in path_l or plugin_dir.replace("_", "-") in path_l:
            return _plugin_path(plugin_dir)
        if token and token in stem.replace("_", ""):
            return _plugin_path(plugin_dir)

    if "hog" in stem:
        return _plugin_path("hogs")
    if "kubevirt" in path_l or "vm_outage" in stem or "vm-outage" in path_l:
        return _plugin_path("kubevirt_vm_outage")
    if "network" in stem:
        return _plugin_path("network_chaos")
    if any(t in stem for t in ("etcd", "pod", "kill")):
        return _plugin_path("pod_disruption")
    if "node" in stem:
        return _plugin_path("node_actions")
    if "pvc" in stem or "storage" in stem:
        return _plugin_path("pvc" if "pvc" in stem else "storage_throttle")

    return None


def _hint_for_plugin(plugin: str, base_scenario: str | None) -> str:
    """Short configuration hint when plugin came from MAP/ANALYZE.

    Only treat ``base_scenario`` as a starter template when it belongs to the
    same plugin. Otherwise describe the chosen plugin on its own and warn that
    the MAP file is a different scenario type.
    """
    scenario_type = _scenario_type_from_plugin(plugin)
    plugin_dir = plugin.removeprefix("krkn/scenario_plugins/").strip("/")

    if base_scenario and base_scenario_matches_plugin(base_scenario, plugin):
        return (
            f"Start from `{base_scenario}` (scenario type `{scenario_type}`). "
            "Adapt selectors/namespace for this bug's component, then register in "
            "`config/config.yaml` if adding a new file."
        )

    example = _PLUGIN_EXAMPLE_SCENARIOS.get(plugin_dir)
    example_bit = (
        f" Use `{example}` as a shape reference for `{scenario_type}`."
        if example
        else ""
    )
    if base_scenario:
        base_plugin = _plugin_from_base_scenario(base_scenario)
        base_type = (
            _scenario_type_from_plugin(base_plugin) if base_plugin else "unknown"
        )
        base_dir = (
            base_plugin.removeprefix("krkn/scenario_plugins/").strip("/")
            if base_plugin
            else "unknown"
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
        f"{example_bit} "
        "Match label selectors to the bug's component namespace."
    )


def base_scenario_matches_plugin(
    base_scenario: str | None, plugin: str | None,
) -> bool:
    """True when MAP ``base_scenario`` is the same krkn plugin as ``plugin``."""
    if not base_scenario or not plugin:
        return False
    mapped = _plugin_from_base_scenario(base_scenario)
    if not mapped:
        return False
    return (
        mapped.rstrip("/") == plugin.rstrip("/")
        or normalize_krkn_plugin(mapped) == normalize_krkn_plugin(plugin)
    )


# Example YAML shapes when MAP base_scenario is the wrong plugin type
_PLUGIN_EXAMPLE_SCENARIOS: dict[str, str] = {
    "hogs": "scenarios/kube/cpu-hog.yml",
    "container": "scenarios/openshift/container.yml",
    "network_chaos": "scenarios/openshift/network-chaos.yml",
    "pod_disruption": "scenarios/openshift/etcd.yml",
    "node_actions": "scenarios/openshift/node_scenarios_example.yml",
}


def _method_for_plugin(plugin: str) -> str:
    plugin_dir = plugin.removeprefix("krkn/scenario_plugins/").strip("/")
    labels = {
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
    }
    return labels.get(plugin_dir, f"Use the `{plugin_dir}` krkn plugin to inject the failure mode")


def resolve_injection_method(
    gap: GapAnalysis,
) -> tuple[str, str, str, str]:
    """Pick injection method for issue/PR text.

    Preference order:
      1. ``gap.krkn_plugin`` from ANALYZE
      2. Plugin derived from MAP ``gap.base_scenario``
      3. Keyword inference guided by FILTER ``filter_injection_method``
      4. Keyword ``_infer_injection_method`` (labeled fallback / default)

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
        # Prefer FILTER hint only when it moves off the generic pod_disruption default
        # or when the filter text itself clearly names a disruption style.
        intentional = any(
            kw in gap.filter_injection_method.lower()
            for kw in (
                "network", "hog", "cpu", "memory", "node", "kubevirt", "vm",
                "pvc", "storage", "time", "zone", "flood", "hijack", "container",
            )
        )
        if intentional or not plugin.rstrip("/").endswith("pod_disruption"):
            return (
                method_desc,
                plugin,
                config_hint,
                "FILTER injection_method",
            )

    method_desc, plugin, config_hint = _infer_injection_method(gap)
    summary = gap.bug.summary.lower()
    desc = (gap.bug.description or "").lower()
    text = f"{summary} {desc}"
    source = "keyword heuristic"
    if plugin.rstrip("/").endswith("pod_disruption"):
        intentional = any(
            kw in text
            for kw in ("etcd", "quorum", "leader", "upgrade", "rollback", "duplicate member")
        )
        if not intentional:
            source = "keyword default (low confidence — review before implementing)"
            config_hint = (
                f"{config_hint} "
                "**Note:** No MAP/ANALYZE plugin was available; pod_disruption is the "
                "generic fallback, not a high-confidence match for this bug."
            )
        else:
            source = "keyword heuristic (pod_disruption for etcd/upgrade-style failure)"

    return method_desc, plugin, config_hint, source


def _infer_injection_method(gap: GapAnalysis) -> tuple[str, str, str]:
    """Infer the krkn injection method, plugin, and how to configure it.

    Returns (method_description, plugin_name, config_hint).
    """
    summary = gap.bug.summary.lower()
    desc = gap.bug.description.lower() if gap.bug.description else ""
    text = f"{summary} {desc}"

    if "kubevirt" in text or "virt-launcher" in text or "vm migration" in text:
        return (
            "Disrupt a KubeVirt virtual machine and verify recovery",
            _plugin_path("kubevirt_vm_outage"),
            "Use `scenarios/kubevirt/kubevirt-vm-outage.yaml` with `vm_name` or `label_selector` "
            "in the target namespace.",
        )
    if "managedcluster" in text or "klusterlet" in text or "multicluster" in text:
        return (
            "Disrupt a ManagedCluster or klusterlet and verify OCM detection/recovery",
            _plugin_path("managed_cluster"),
            "Use `managedcluster_stop_start_scenario` with `managedcluster_name` or `label_selector`.",
        )
    if "zone outage" in text or "availability zone" in text:
        return (
            "Simulate an availability zone outage via network ACL changes",
            _plugin_path("zone_outage"),
            "Configure `zone_outage` with `cloud_type`, `duration`, and `subnet_id`.",
        )
    if "cluster shut down" in text or "cluster shutdown" in text:
        return (
            "Shut down all cluster nodes for a duration and verify recovery on restart",
            _plugin_path("shut_down"),
            "Use `cluster_shut_down_scenario` with `shut_down_duration` and `cloud_type`.",
        )
    if "clock skew" in text or "time skew" in text or "ntp drift" in text:
        return (
            "Inject clock skew on pods or nodes to test time-sensitive component behavior",
            _plugin_path("time_actions"),
            "Use `time_scenarios` with `action: skew_time` or `skew_date` and `label_selector`.",
        )
    if "storage throttle" in text or "iops limit" in text or "disk throttle" in text:
        return (
            "Throttle storage I/O on a PVC mount to simulate degraded disk performance",
            _plugin_path("storage_throttle"),
            "Use `storage_throttle_scenario` with `pvc_name`, `throttle_type`, and `duration`.",
        )
    if "disk full" in text or "out of space" in text or "pvc full" in text:
        return (
            "Fill a PVC to capacity to test out-of-disk handling",
            _plugin_path("pvc"),
            "Use `pvc_scenario` with `pvc_name`, `namespace`, and `fill_percentage`.",
        )
    if "syn flood" in text or "syn-flood" in text or "hping" in text:
        return (
            "Launch a SYN flood against a target service",
            _plugin_path("syn_flood"),
            "Configure `target-service`, `target-port`, and `duration`.",
        )
    if "service hijack" in text or "service hijacking" in text:
        return (
            "Hijack a Kubernetes service to return error responses to callers",
            _plugin_path("service_hijacking"),
            "Set `service_name`, `service_namespace`, and a `plan` with HTTP status steps.",
        )
    if "service disruption" in text:
        return (
            "Disrupt services in a namespace to test application resilience",
            _plugin_path("service_disruption"),
            "Use `scenarios` with `namespace` regex, `runs`, and `wait_time`.",
        )
    if "application outage" in text or "route inaccessible" in text:
        return (
            "Block ingress/egress traffic to application routes",
            _plugin_path("application_outage"),
            "Use `application_outage` with `namespace`, `pod_selector`, and `block`.",
        )
    if "http load" in text or "load test" in text or "request rate" in text:
        return (
            "Generate HTTP load against target endpoints to test saturation behavior",
            _plugin_path("http_load"),
            "Use `- http_load_scenario:` with `targets.endpoints`, `rate`, and `duration`.",
        )
    if "container kill" in text or "kill container" in text:
        return (
            "Kill or disrupt a specific container within a pod",
            _plugin_path("container"),
            "Use `scenarios` with `namespace`, `label_selector`, `container_name`, and `action: 1`.",
        )
    if "pod network" in text or "network filter" in text or "interface down" in text:
        return (
            "Apply pod- or node-level network filters (latency, loss, bandwidth)",
            _plugin_path("network_chaos_ng"),
            "Use `- id: pod_network_chaos` with `label_selector`, `latency`, and `loss`.",
        )
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
    method_desc, plugin, config_hint, _source = resolve_injection_method(gap)

    if gap.base_scenario and gap.action_type == ActionType.DRAFT_PR:
        steps.append(f"Review the existing scenario at `{gap.base_scenario}` and understand its current configuration")
        steps.append(f"Create a new scenario YAML (or add a variant) that targets: **{_infer_failure_mode(gap)}**")
        steps.append(f"Use the `{plugin}` plugin — {config_hint}")
        steps.append("Add assertions to verify the component reports correct status during/after chaos")
        steps.append("Add the new scenario to `krkn/scenario_plugins/<plugin_dir>/` (under appropriate scenario type)")
        steps.append("Write a unit test in `tests/` if adding new plugin logic")
        steps.append("Create krkn-hub wrapper (Dockerfile, env.sh, run.sh, build_config_file.py, krknctl-input.json) following the standard pattern")
        steps.append("Update krkn-chaos.dev documentation with the new scenario")
        steps.append("Add to Prow CI config in `openshift/release` if needed")
    elif gap.base_scenario:
        steps.append(f"Evaluate whether `{gap.base_scenario}` can be extended or if a new scenario is needed")
        steps.append(f"The failure mode is: **{_infer_failure_mode(gap)}**")
        steps.append(f"Suggested plugin: `{plugin}` — {config_hint}")
        steps.append("Determine if existing krkn-lib methods support this injection, or if new code is needed")
        steps.append("If extending: modify the existing YAML to add a new variant")
        steps.append(
            f"If new plugin code is needed: implement in `{plugin}` and check "
            "`krkn-chaos/krkn-lib` for K8s helpers"
        )
        steps.append(
            "If new scenario: follow the plugin creation guide in "
            "[`krkn-chaos/krkn` CLAUDE.md](https://github.com/krkn-chaos/krkn/blob/main/CLAUDE.md)"
        )
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
        scenario_url = scenario_github_url(gap.base_scenario)
        if scenario_url:
            lines.append(
                f"The closest existing scenario is [`{gap.base_scenario}`]({scenario_url}). "
            )
        else:
            lines.append(f"The closest existing scenario is `{gap.base_scenario}`. ")
        if not base_scenario_matches_plugin(gap.base_scenario, plugin):
            base_plugin = _plugin_from_base_scenario(gap.base_scenario)
            base_dir = (
                base_plugin.removeprefix("krkn/scenario_plugins/").strip("/")
                if base_plugin
                else "unknown"
            )
            chosen_dir = plugin.removeprefix("krkn/scenario_plugins/").strip("/")
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
        f"| `krkn-chaos/krkn` | Scenario YAML under `{plugin}`; register in "
        "`krkn/config/config.yaml`; extend plugin code there if needed |"
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
