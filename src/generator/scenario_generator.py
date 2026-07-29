"""Scenario generator — bridges ANALYZE output with krkn-knowledgebase.

Takes gap analysis recommendations (natural language) and produces
validated krknctl commands, krkn-hub Docker commands, and scenario YAML
using the authoritative krkn-knowledgebase schemas.
"""

from __future__ import annotations

import json
import logging
import re

from src.knowledge.scenario_knowledgebase import ScenarioKnowledgeBase
from src.models import GapAnalysis

logger = logging.getLogger(__name__)

# Map common ANALYZE plugin names to knowledge base scenario names
PLUGIN_TO_SCENARIO = {
    "cpu_hog_scenarios": "node-cpu-hog",
    "cpu_hog": "node-cpu-hog",
    "memory_hog_scenarios": "node-memory-hog",
    "memory_hog": "node-memory-hog",
    "io_hog_scenarios": "node-io-hog",
    "io_hog": "node-io-hog",
    "hog_scenarios": "node-cpu-hog",
    "pod_scenarios": "pod-scenarios",
    "pod_kill": "pod-scenarios",
    "container_scenarios": "container-scenarios",
    "node_scenarios": "node-scenarios",
    "node_reboot": "node-scenarios",
    "network_chaos": "network-chaos",
    "network_chaos_scenarios": "network-chaos",
    "pod_network_chaos": "pod-network-chaos",
    "pod_network_outage": "pod-network-chaos",
    "node_network_filter": "node-network-filter",
    "pod_network_filter": "pod-network-filter",
    "syn_flood": "syn-flood",
    "time_scenarios": "time-scenarios",
    "time_skew": "time-scenarios",
    "application_outage": "application-outages",
    "service_disruption": "service-disruption-scenarios",
    "service_hijacking": "service-hijacking",
    "pvc_scenarios": "pvc-scenarios",
    "power_outages": "power-outages",
    "cluster_shut_down": "power-outages",
    "zone_outages": "zone-outages",
    "kubevirt_vm_outage": "kubevirt-outage",
    # FILTER injection_method categories (from chaos_filter.py)
    "resource_stress": "node-cpu-hog",
    "node": "node-scenarios",
    "network": "network-chaos",
    "pod": "pod-scenarios",
    "cluster_state": "service-disruption-scenarios",
    "time_skew": "time-scenarios",
    "cloud_provider": "zone-outages",
}


def _extract_params_from_text(text: str, scenario: dict) -> dict:
    """Extract parameter values from ANALYZE modification text.

    Uses regex patterns to find values that match known parameter names.
    This is the non-LLM fallback — works for common patterns like
    "CPU 90%", "duration 300s", "worker nodes".
    """
    params: dict = {}
    text_lower = text.lower()
    param_defs = scenario.get("parameters", [])

    for pdef in param_defs:
        name = pdef.get("name", "")
        ptype = pdef.get("type", "string")

        if name in ("cpu-percentage", "cpu_percentage"):
            # Match: "CPU 90%", "cpu target 90%", "cpu-percentage 90"
            match = re.search(r"cpu[- _]?(?:target|percentage)?[:\s]+(\d+)\s*%?", text_lower)
            if match:
                params[name] = int(match.group(1))

        elif name in ("chaos-duration", "total_chaos_duration", "duration"):
            # Match: "duration 300s", "300 seconds", "duration: 300"
            match = re.search(r"duration[:\s]+(\d+)\s*s?", text_lower)
            if not match:
                match = re.search(r"(\d+)\s*(?:seconds?|s)\b", text_lower)
            if match:
                params[name] = int(match.group(1))

        elif name in ("cores", "node_cpu_core"):
            match = re.search(r"cores?[:\s]+(\d+)", text_lower)
            if match:
                params[name] = int(match.group(1))

        elif name in ("node-selector", "node_selector", "label_selector"):
            # Match: "worker nodes", "master nodes", "node-selector: key=val"
            if "worker" in text_lower:
                params[name] = "node-role.kubernetes.io/worker="
            elif "master" in text_lower or "control-plane" in text_lower or "control plane" in text_lower:
                params[name] = "node-role.kubernetes.io/master="
            else:
                match = re.search(r"(?:node[_-]?selector|label[_-]?selector)[:\s]+([^\s,]+)", text_lower)
                if match:
                    params[name] = match.group(1)

        elif name in ("namespace", "namespace_pattern"):
            # Match explicit namespace references like "namespace: openshift-etcd"
            # or "in openshift-ovn-kubernetes namespace"
            match = re.search(r"(?:in\s+|namespace[:\s]+)(openshift-[a-z0-9-]+|kube-[a-z0-9-]+|default)", text_lower)
            if match:
                params[name] = match.group(1)

        elif name in ("number-of-nodes", "instance_count"):
            match = re.search(r"(?:nodes?|instances?)[:\s]+(\d+)", text_lower)
            if match:
                params[name] = int(match.group(1))

        elif name in ("memory-percentage", "memory"):
            match = re.search(r"memory[- _]?(?:target|percentage)?[:\s]+(\d+)\s*%?", text_lower)
            if match:
                params[name] = int(match.group(1))

    return params


def match_scenario(
    gap: GapAnalysis,
    kb: ScenarioKnowledgeBase,
) -> dict | None:
    """Match a gap's plugin recommendation to a knowledge base scenario.

    Tries multiple strategies:
    1. Direct mapping from PLUGIN_TO_SCENARIO
    2. Knowledge base find_scenario (fuzzy)
    3. Search from gap reasoning text
    """
    # Prefer structured field from ANALYZE (full path or bare dir)
    plugin_name = None
    if getattr(gap, "krkn_plugin", None):
        raw = gap.krkn_plugin.strip().strip("`")
        if raw.startswith("krkn/scenario_plugins/"):
            raw = raw.removeprefix("krkn/scenario_plugins/").strip("/")
        plugin_name = raw.split("/")[0].lower().replace("-", "_") or None

    # Check reasoning for "krkn plugin: xxx" (dir or full path)
    if not plugin_name and gap.reasoning:
        match = re.search(
            r"krkn plugin:\s*(?:krkn/scenario_plugins/)?([a-z0-9_-]+)",
            gap.reasoning.lower(),
        )
        if match:
            plugin_name = match.group(1).replace("-", "_")

    # Check if reasoning itself is a known plugin/category name
    if not plugin_name and gap.reasoning:
        reasoning_lower = gap.reasoning.lower().strip()
        if reasoning_lower in PLUGIN_TO_SCENARIO:
            plugin_name = reasoning_lower

    # Check modifications for plugin names
    if not plugin_name and gap.modifications:
        mods_text = " ".join(gap.modifications).lower()
        for known_plugin in PLUGIN_TO_SCENARIO:
            if known_plugin.replace("_", " ") in mods_text or known_plugin in mods_text:
                plugin_name = known_plugin
                break

    # Try direct mapping first
    if plugin_name and plugin_name in PLUGIN_TO_SCENARIO:
        scenario_name = PLUGIN_TO_SCENARIO[plugin_name]
        scenario = kb.get_scenario(scenario_name)
        if scenario:
            return scenario

    # Try knowledge base fuzzy search
    if plugin_name:
        scenario = kb.find_scenario(plugin_name)
        if scenario:
            return scenario

    # Last resort: search with bug summary keywords
    query = gap.bug.summary.lower()
    for keyword, scenario_name in [
        ("cpu", "node-cpu-hog"),
        ("memory", "node-memory-hog"),
        ("io", "node-io-hog"),
        ("network", "network-chaos"),
        ("pod kill", "pod-scenarios"),
        ("service disruption", "service-disruption-scenarios"),
        ("pvc", "pvc-scenarios"),
    ]:
        if keyword in query:
            return kb.get_scenario(scenario_name)

    return None


def extract_parameters(
    gap: GapAnalysis,
    scenario: dict,
) -> dict:
    """Extract parameter values from gap's modifications and reasoning."""
    # Combine all text sources
    text_parts = [gap.bug.summary]
    if gap.modifications:
        text_parts.extend(gap.modifications)
    if gap.reasoning:
        text_parts.append(gap.reasoning)

    combined_text = " ".join(text_parts)
    return _extract_params_from_text(combined_text, scenario)


def generate_issue_section(
    gap: GapAnalysis,
    kb: ScenarioKnowledgeBase,
) -> str | None:
    """Generate the validated commands section for a GitHub issue body.

    Returns a markdown string with krknctl command, krkn-hub command,
    scenario YAML, and edge cases. Returns None if no scenario matches.
    """
    scenario = match_scenario(gap, kb)
    if not scenario:
        return None

    params = extract_parameters(gap, scenario)
    if not params:
        return None

    # Validate
    errors = []
    for pdef in kb.get_parameters(scenario):
        if pdef["name"] in params:
            err = kb.validate_parameter(pdef, params[pdef["name"]])
            if err:
                errors.append(err)

    # Build section
    lines = []
    lines.append("### Generated Commands (validated against krkn-knowledgebase)")
    lines.append("")

    if errors:
        lines.append(f"> **Validation warnings:** {'; '.join(errors)}")
        lines.append("")

    scenario_name = scenario.get("scenario_name", "unknown")
    lines.append(f"**Scenario:** {scenario.get('title', '?')} (`{scenario_name}`)")
    lines.append(f"**Container image:** `{scenario.get('container_image', '?')}`")
    lines.append("")

    # Parameters table
    lines.append("**Parameters:**")
    lines.append("")
    lines.append("| Parameter | Value | Default |")
    lines.append("|-----------|-------|---------|")
    for pdef in kb.get_parameters(scenario):
        name = pdef["name"]
        if name in params:
            lines.append(f"| `{name}` | **{params[name]}** | {pdef.get('default', '-')} |")
    lines.append("")

    # krknctl
    lines.append("**krknctl:**")
    lines.append("```bash")
    lines.append(kb.generate_krknctl_command(scenario, params))
    lines.append("```")
    lines.append("")

    # krkn-hub
    lines.append("**krkn-hub (Docker):**")
    lines.append("```bash")
    lines.append(kb.generate_krknhub_command(scenario, params))
    lines.append("```")
    lines.append("")

    # Scenario YAML
    lines.append("**Scenario YAML:**")
    lines.append("```yaml")
    lines.append(kb.generate_scenario_yaml(scenario, params))
    lines.append("```")
    lines.append("")

    # Edge cases
    edge_cases = kb.get_edge_cases(scenario)
    if edge_cases:
        lines.append("**Edge cases:**")
        for ec in edge_cases[:4]:
            lines.append(f"- {ec}")
        lines.append("")

    return "\n".join(lines)
