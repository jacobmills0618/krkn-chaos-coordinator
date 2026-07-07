"""Chaos relevance filter — determines if a bug needs a chaos test.

Core rule: If a bug involves a component behaving incorrectly during, after,
or because of any disruption (restart, failure, load, resource pressure,
upgrade, scaling) — it's chaos-relevant. Even if the symptom appears in a
different component than the root cause.

Keywords are loaded from config/filters/common.yaml (shared) and merged
with agent-specific keywords from config/agents/<name>.yaml.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import yaml

from src.models import Bug, FilterResult

logger = logging.getLogger(__name__)

_FILTERS_DIR = Path(__file__).parent.parent.parent / "config" / "filters"
_AGENTS_DIR = Path(__file__).parent.parent.parent / "config" / "agents"


# ---------------------------------------------------------------------------
# Keyword loading (cached)
# ---------------------------------------------------------------------------

_common_cache: dict | None = None
_ocp_virt_cache: dict | None = None
_cache_lock = threading.Lock()


def _load_ocp_virt_filters() -> dict:
    """Load OpenShift Virtualization keywords from config/filters/ocp-virt.yaml."""
    global _ocp_virt_cache
    with _cache_lock:
        if _ocp_virt_cache is not None:
            return _ocp_virt_cache

        path = _FILTERS_DIR / "ocp-virt.yaml"
        if not path.exists():
            logger.warning("OCP Virt filter config not found at %s", path)
            _ocp_virt_cache = {"skip_keywords": [], "ocp_virt_keywords": []}
            return _ocp_virt_cache

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        _ocp_virt_cache = {
            "skip_keywords": [str(k) for k in data.get("skip_keywords", [])],
            "ocp_virt_keywords": [str(k) for k in data.get("ocp-virt_keywords", [])],
        }
        return _ocp_virt_cache


def _load_common_filters() -> dict:
    """Load common filter keywords from config/filters/common.yaml, cached."""
    global _common_cache
    with _cache_lock:
        if _common_cache is not None:
            return _common_cache

        path = _FILTERS_DIR / "common.yaml"
        if not path.exists():
            logger.warning("Common filter config not found at %s, using empty", path)
            _common_cache = {"skip_keywords": [], "chaos_keywords": []}
            return _common_cache

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        _common_cache = {
            "skip_keywords": [str(k) for k in data.get("skip_keywords", [])],
            "chaos_keywords": [str(k) for k in data.get("chaos_keywords", [])],
        }
        return _common_cache


def _load_agent_filter(agent_name: str) -> dict:
    """Load agent-specific filter keywords from config/agents/<name>.yaml."""
    path = _AGENTS_DIR / f"{agent_name}.yaml"
    if not path.exists():
        return {"skip_keywords": [], "chaos_keywords": []}

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    filt = data.get("filter", {})
    if not filt:
        return {"skip_keywords": [], "chaos_keywords": []}

    return {
        "skip_keywords": [str(k) for k in filt.get("skip_keywords", [])],
        "chaos_keywords": [str(k) for k in filt.get("chaos_keywords", [])],
    }


def get_filter_keywords(agent_name: str | None = None) -> tuple[list[str], list[str]]:
    """Return merged (skip_keywords, chaos_keywords) for an agent.

    Common keywords + agent-specific keywords. Agent keywords are appended,
    not replacing common ones.
    """
    common = _load_common_filters()
    skip = list(common["skip_keywords"])
    chaos = list(common["chaos_keywords"])

    if agent_name:
        agent = _load_agent_filter(agent_name)
        skip.extend(agent["skip_keywords"])
        chaos.extend(agent["chaos_keywords"])
        if agent_name == "virtualization":
            virt = _load_ocp_virt_filters()
            skip.extend(virt["skip_keywords"])
            chaos.extend(virt["ocp_virt_keywords"])

    return skip, chaos


def get_domain_filter_keywords(agent_name: str | None) -> tuple[list[str], list[str]] | None:
    """Return (skip_keywords, domain_keywords) for domain-only filtering.

    Currently supported for the virtualization agent (ocp-virt.yaml).
    Returns None when the agent has no domain filter config.
    """
    if agent_name != "virtualization":
        return None
    virt = _load_ocp_virt_filters()
    return virt["skip_keywords"], virt["ocp_virt_keywords"]


# ---------------------------------------------------------------------------
# krkn injection capabilities
# ---------------------------------------------------------------------------

KRKN_CAPABILITIES = [
    "pod failures (kill, restart, CPU/memory hog)",
    "node failures (drain, reboot, shutdown, network isolate)",
    "network chaos (partition, latency via tc netem, packet loss, DNS failure)",
    "resource stress (CPU, memory, disk fill, I/O pressure)",
    "time skew (NTP drift, clock jumps)",
    "container chaos (kill containers, corrupt mounts)",
    "cloud provider (detach volumes, stop VMs, AZ outage)",
    "cluster state (delete CRDs, corrupt configmaps, scale to 0)",
]


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------

def _is_stub_bug(text: str, summary: str) -> bool:
    """Return True for JIRA stub tickets or clone-of-issue placeholders."""
    return "clone of issue" in text[:200] or "[stub]" in summary.lower()


def _skip_on_keyword(bug: Bug, text: str, skip_keywords: list[str], *, prefix: str) -> FilterResult | None:
    """Return a SKIP FilterResult if *text* matches any skip keyword, else None."""
    for keyword in skip_keywords:
        if keyword.lower() in text:
            return FilterResult(
                bug=bug,
                chaos_relevant=False,
                skip_reason=f"{prefix}: matches skip keyword '{keyword}'",
                confidence=0.95,
            )
    return None


def _partition_bugs(
    bugs: list[Bug],
    filter_fn,
    agent_name: str | None,
    *,
    pass_label: str,
    skip_label: str,
) -> tuple[list[FilterResult], list[FilterResult]]:
    """Apply *filter_fn* to each bug and split into passed vs skipped lists."""
    relevant: list[FilterResult] = []
    skipped: list[FilterResult] = []

    for bug in bugs:
        result = filter_fn(bug, agent_name)
        if result.chaos_relevant:
            relevant.append(result)
            detail = result.failure_mode or result.injection_method
            logger.info("%s %s: %s", pass_label, bug.key, detail)
        else:
            skipped.append(result)
            logger.info("%s %s: %s", skip_label, bug.key, result.skip_reason)

    logger.info(
        "Filter result: %d relevant, %d skipped out of %d total",
        len(relevant), len(skipped), len(bugs),
    )
    return relevant, skipped


def filter_bug(bug: Bug, agent_name: str | None = None) -> FilterResult:
    """Determine if a bug is chaos-relevant using keyword heuristics.

    Applies skip keywords, chaos keywords, and krkn injection-method matching.
    """
    skip_keywords, chaos_keywords = get_filter_keywords(agent_name)
    text = f"{bug.summary} {bug.description}".lower()

    if _is_stub_bug(text, bug.summary):
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            skip_reason="Stub/clone ticket — not an original bug report",
            confidence=0.95,
        )

    skipped = _skip_on_keyword(bug, text, skip_keywords, prefix="Not chaos-relevant")
    if skipped:
        return skipped

    matched_keywords = [kw for kw in chaos_keywords if kw.lower() in text]
    if not matched_keywords:
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            skip_reason="No chaos-relevant failure mode keywords found in bug description",
            confidence=0.7,
        )

    failure_mode = _extract_failure_mode(text, matched_keywords)
    injection_method = _match_injection_method(text)

    if injection_method is None:
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            failure_mode=failure_mode,
            skip_reason="Failure mode identified but no matching krkn injection capability",
            confidence=0.3,
        )

    specific_keywords = [
        "crash", "panic", "oom", "out of memory", "deadlock", "crashloop",
        "node drain", "node reboot", "node delete", "network partition",
        "packet loss", "dns failure", "pod eviction", "pod kill",
        "certificate expired", "clock skew", "data loss", "data corruption",
        "disk full", "memory leak", "quorum", "split brain",
    ]
    confidence = 0.85 if any(kw in matched_keywords for kw in specific_keywords) else 0.5
    return FilterResult(
        bug=bug,
        chaos_relevant=True,
        failure_mode=failure_mode,
        injection_method=injection_method,
        confidence=confidence,
    )


def filter_domain_bug(bug: Bug, agent_name: str | None = None) -> FilterResult:
    """Domain-only filter — ocp-virt keywords without chaos/injection gates.

    Use with ``--domain-filter-only`` to tune virt keyword coverage.
    """
    domain = get_domain_filter_keywords(agent_name)
    if domain is None:
        raise ValueError(
            f"Agent '{agent_name}' has no domain filter config "
            "(currently only 'virtualization' supports --domain-filter-only)"
        )

    skip_keywords, domain_keywords = domain
    text = f"{bug.summary} {bug.description}".lower()

    if _is_stub_bug(text, bug.summary):
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            skip_reason="Stub/clone ticket — not an original bug report",
            confidence=0.95,
        )

    skipped = _skip_on_keyword(bug, text, skip_keywords, prefix="Not domain-relevant")
    if skipped:
        return skipped

    matched = [kw for kw in domain_keywords if kw.lower() in text]
    if not matched:
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            skip_reason="No ocp-virt domain keywords found in bug description",
            confidence=0.7,
        )

    return FilterResult(
        bug=bug,
        chaos_relevant=True,
        failure_mode=f"Domain indicators: {', '.join(matched[:5])}",
        confidence=0.85,
    )


def filter_domain_bugs(
    bugs: list[Bug], agent_name: str | None = None,
) -> tuple[list[FilterResult], list[FilterResult]]:
    """Filter bugs using domain keywords only (no chaos/injection gate)."""
    return _partition_bugs(
        bugs, filter_domain_bug, agent_name, pass_label="DOMAIN PASS", skip_label="DOMAIN SKIP",
    )


def filter_bugs(bugs: list[Bug], agent_name: str | None = None) -> tuple[list[FilterResult], list[FilterResult]]:
    """Filter a list of bugs into chaos-relevant and non-relevant."""
    return _partition_bugs(bugs, filter_bug, agent_name, pass_label="PASS", skip_label="SKIP")


def _extract_failure_mode(text: str, matched_keywords: list[str]) -> str:
    """Build a failure mode description from matched keywords."""
    return f"Failure indicators: {', '.join(matched_keywords[:5])}"


def _match_injection_method(text: str) -> str | None:
    """Match bug description against krkn's injection capabilities."""
    injection_rules: list[tuple[str, list[str]]] = [
        ("node", [
            "node delete", "node replace", "node drain", "node reboot",
            "node shutdown", "node fail", "node not ready", "kubelet",
            "machine api", "node outage", "nodestatuses", "node pressure",
        ]),
        ("network", [
            "network partition", "network chaos", "packet loss",
            "dns fail", "connection refused", "connection reset",
            "connection timeout", "ingress", "ovn",
            "network outage", "network disruption",
            "502", "503", "504",
        ]),
        ("resource_stress", [
            "cpu", "memory pressure", "memory leak", "memory spike",
            "disk full", "disk pressure", "resource exhaustion",
            "throttl", "resource pressure", "api server load",
            "resource stress", "hog", "i/o pressure", "cpu spike",
            "high cpu", "resource quota", "limit reached",
            "slow", "latency increased", "high latency",
            "p99", "p95", "response time", "throughput",
            "performance degradation", "performance regression",
            "under load", "under pressure", "under stress",
            "intermittent",
        ]),
        ("pod", [
            "pod kill", "pod delete", "pod disruption", "pod eviction",
            "container restart", "crashloop", "oom", "out of memory",
            "static pod", "pod fail", "pod outage", "oom kill",
        ]),
        ("cluster_state", [
            "crd", "configmap", "operator", "upgrade fail", "rollback",
            "scale", "quorum", "leader election", "member", "etcd",
            "split brain", "cluster state", "corrupt",
            "cluster operator", "co degraded", "co unavailable",
            "operator degraded", "upgrade from", "upgrade to",
            "doesn't reconcile", "failed to reconcile", "not reconciling",
            "autoscaler", "pending pods", "scheduling failed",
            "scale up failed", "insufficient resources",
            "flapping",
        ]),
        ("time_skew", [
            "clock", "ntp", "time skew", "certificate expired", "cert rotation",
        ]),
        ("cloud_provider", [
            "instance", "volume detach", "stop vm", "az outage",
            "availability zone",
        ]),
    ]

    for capability, keywords in injection_rules:
        for kw in keywords:
            if kw in text:
                return capability

    generic = [
        "fail", "crash", "unavailable", "degraded", "unhealthy",
        "disruption", "outage", "panic", "deadlock", "stuck",
        "doesn't recover", "stale after restart", "data loss",
        "service down", "service unavailable", "endpoint not reachable",
    ]
    for kw in generic:
        if kw in text:
            return "cluster_state"

    return None
