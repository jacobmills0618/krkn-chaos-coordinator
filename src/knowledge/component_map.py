"""Component-to-agent mapping using team_component_map.json from openshift-eng/ai-helpers."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Agent name → list of OCPBUGS component names
# Expanded from 21 → 96 components based on OCP 4.21 analysis (1,406 bugs)
AGENT_COMPONENTS: dict[str, list[str]] = {
    "upgrade_lifecycle": [
        "Cluster Version Operator",
        "Machine Config Operator",
        "Machine Config Operator / platform-vsphere",
        "Installer / openshift-installer",
        "Installer / Agent based installation",
        "Installer / Assisted installer",
        "Installer / Disconnected UI",
        "Installer / OpenShift on OpenStack",
        "Installer / vSphere",
        "Installer / PowerVC",
        "Installer / PowerVS",
        "Installer / Nutanix",
        "LCA operator",
        "TALM Operator",
        "oc-mirror",
    ],
    "control_plane": [
        "kube-apiserver",
        "Etcd",
        "kube-controller-manager",
        "kube-scheduler",
        "openshift-apiserver",
        "apiserver-auth",
        "oauth-apiserver",
        "oauth-proxy",
        "openshift-controller-manager / controller-manager",
        "route-controller-manager",
        "service-ca",
        "config-operator",
        "HyperShift",
        "HyperShift / ROSA",
        "HyperShift / ARO",
        "HyperShift / OCP Virtualization",
        "HyperShift / OpenStack",
        "HyperShift / GCP",
        "HyperShift / Agent",
        "Pod Autoscaler",
    ],
    "node_machine": [
        "Node / Kubelet",
        "Node / CRI-O",
        "Node / CPU manager",
        "Node Tuning Operator",
        "Node Feature Discovery Operator",
        "Cloud Compute",
        "Machine API",
        "Cloud Compute / Machine API Providers",
        "Performance Addon Operator",
        "Bare Metal Hardware Provisioning",
        "Bare Metal Hardware Provisioning / baremetal-operator",
        "Bare Metal Hardware Provisioning / ironic",
        "Bare Metal Hardware Provisioning / OS Image Provider",
        "Bare Metal Hardware Provisioning / cluster-baremetal-operator",
        "Bare Metal Hardware Provisioning / cluster-api-provider",
        "Installer / OpenShift on Bare Metal IPI",
        "Installer / Single Node OpenShift",
        "Two Node Fencing",
        "Cluster Autoscaler",
        "RHCOS",
        "Multi-Arch / IBM P and Z",
        "oc / node-image",
    ],
    "networking": [
        "Networking / ovn-kubernetes",
        "Networking / cluster-network-operator",
        "Networking / DNS",
        "Networking / router",
        "Networking / ptp",
        "Networking / multus",
        "Networking / SR-IOV",
        "Networking / Metal LB",
        "Networking / FRR-K8s",
        "Networking / On-Prem Host Networking",
        "Networking / On-Prem Load Balancer",
        "Networking / networking-console-plugin",
        "Networking / network-tools",
        "Networking / runtime-cfg",
        "Networking / cloud-network-config-controller",
        "Networking / kubernetes-nmstate-operator",
        "Networking / nmstate-console-plugin",
        "Networking / DPU",
        "networking-ingress-commatrix",
        "MicroShift / Networking",
    ],
    "storage": [
        "Storage",
        "Storage / Operators",
        "Storage / Kubernetes",
        "Storage / Kubernetes External Components",
        "Storage / kubernetes-csi-driver-manila",
        "Storage / Local Storage Operator",
        "Image Registry",
        "Logical Volume Manager Storage",
        "Secrets Store CSI driver",
        "kube-storage-version-migrator",
        "OLM / Registry",
    ],
    "operators_platform": [
        "OLM",
        "OLM / OperatorHub",
        "Console",
        "Management Console",
        "Dev Console",
        "Authentication",
        "Monitoring",
        "Logging",
        "Observability UI",
        "Insights Operator",
        "must-gather",
        "Cloud Credential Operator",
        "Cloud Compute / Cloud Controller Manager",
        "Cloud Compute / Cluster API Providers",
        "Cloud Compute / OpenStack Provider",
        "Cloud Compute / IBM Provider",
        "Cloud Compute / KubeVirt Provider",
        "Cloud Compute / Nutanix Provider",
        "Cloud Compute / Libvirt Provider",
        "Cloud Compute / ControlPlaneMachineSet",
        "Cloud Compute / Machine CSR Approver",
        "Cloud Compute / MachineHealthCheck",
        "oc",
        "Build",
        "Samples Operator",
    ],
}


def get_components_for_agent(agent_name: str) -> list[str]:
    """Get the OCPBUGS component names for a given agent."""
    components = AGENT_COMPONENTS.get(agent_name)
    if components is None:
        raise ValueError(f"Unknown agent: {agent_name}. Valid: {list(AGENT_COMPONENTS.keys())}")
    return list(components)


def get_all_agents() -> list[str]:
    """Get all agent names."""
    return list(AGENT_COMPONENTS.keys())


def load_team_component_map(path: Path) -> dict:
    """Load the full team component map from JSON file."""
    with open(path) as f:
        return json.load(f)


def update_agent_components_from_map(map_path: Path) -> None:
    """Update AGENT_COMPONENTS using the authoritative team_component_map.json.

    This reads the team mapping and updates the module-level AGENT_COMPONENTS
    dict with the actual component names from the JSON.
    """
    data = load_team_component_map(map_path)
    teams = data.get("teams", {})

    # Map our agent names to source teams
    agent_to_teams = {
        "upgrade_lifecycle": ["Installer"],
        "control_plane": ["API Server", "etcd"],
        "node_machine": ["Node"],
        "networking": ["Networking"],
        "storage": ["Storage"],
        "operators_platform": ["OLM", "Monitoring", "Console", "Authentication"],
    }

    for agent_name, team_names in agent_to_teams.items():
        components = []
        for team_name in team_names:
            team_data = teams.get(team_name, {})
            if isinstance(team_data, dict):
                components.extend(team_data.get("components", []))
            elif isinstance(team_data, list):
                components.extend(team_data)
        if components:
            AGENT_COMPONENTS[agent_name] = components
            logger.info("Updated %s with %d components from map", agent_name, len(components))
