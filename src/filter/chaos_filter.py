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


def _load_common_filters() -> dict:
    """Load common filter keywords from config/filters/common.yaml, cached."""
    global _common_cache
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

    return skip, chaos


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

def filter_bug(bug: Bug, agent_name: str | None = None) -> FilterResult:
    """Determine if a bug is chaos-relevant using keyword heuristics.

    Part 1: Is this a failure mode? (vs code bug, CVE, UI issue)
    Part 2: Can krkn inject this? (match against capabilities)
    """
    skip_keywords, chaos_keywords = get_filter_keywords(agent_name)
    text = f"{bug.summary} {bug.description}".lower()

    if "clone of issue" in text[:200] or "[stub]" in bug.summary.lower():
        return FilterResult(
            bug=bug,
            chaos_relevant=False,
            skip_reason="Stub/clone ticket — not an original bug report",
            confidence=0.95,
        )

    for keyword in skip_keywords:
        if keyword.lower() in text:
            return FilterResult(
                bug=bug,
                chaos_relevant=False,
                skip_reason=f"Not chaos-relevant: matches skip keyword '{keyword}'",
                confidence=0.95,
            )

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
    has_specific = any(kw in matched_keywords for kw in specific_keywords)

    if has_specific:
        return FilterResult(
            bug=bug,
            chaos_relevant=True,
            failure_mode=failure_mode,
            injection_method=injection_method,
            confidence=0.85,
        )

    return FilterResult(
        bug=bug,
        chaos_relevant=True,
        failure_mode=failure_mode,
        injection_method=injection_method,
        confidence=0.5,
    )


def filter_bugs(bugs: list[Bug], agent_name: str | None = None) -> tuple[list[FilterResult], list[FilterResult]]:
    """Filter a list of bugs into chaos-relevant and non-relevant."""
    relevant = []
    skipped = []

    for bug in bugs:
        result = filter_bug(bug, agent_name)
        if result.chaos_relevant:
            relevant.append(result)
            logger.info(
                "PASS %s: %s (injection: %s)",
                bug.key, result.failure_mode, result.injection_method,
            )
        else:
            skipped.append(result)
            logger.info("SKIP %s: %s", bug.key, result.skip_reason)

    logger.info(
        "Filter result: %d relevant, %d skipped out of %d total",
        len(relevant), len(skipped), len(bugs),
    )
    return relevant, skipped


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
