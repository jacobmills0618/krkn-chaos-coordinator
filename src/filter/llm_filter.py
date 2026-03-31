"""LLM-enhanced chaos relevance filter using Ollama."""

import json
import logging

import ollama

from src.models import Bug, FilterResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a chaos engineering expert for OpenShift/Kubernetes clusters.
Your job is to determine if a JIRA bug describes a failure mode that can be tested with chaos engineering tools (krkn).

A bug IS chaos-relevant if:
- A component fails under stress, load, or resource pressure
- A component fails when another component dies or becomes unavailable
- Recovery doesn't work after a disruption (node reboot, pod kill, network partition)
- Race condition during upgrade or rollout
- Data corruption or loss under failure conditions

A bug is NOT chaos-relevant if:
- It's a code logic bug (wrong output from correct inputs, nil pointer, bad parsing)
- It's a CVE or security vulnerability (needs a patch, not a resilience test)
- It's a UI/console rendering issue
- It's a flaky test or test infrastructure problem
- It's a documentation or configuration error
- It's a version-specific migration issue that can't be reproduced via chaos injection

krkn can inject these failure types:
- Pod failures (kill, restart, CPU/memory hog)
- Node failures (drain, reboot, shutdown, network isolate)
- Network chaos (partition, latency, packet loss, DNS failure)
- Resource stress (CPU, memory, disk fill, I/O pressure)
- Time skew (NTP drift, clock jumps)
- Cloud provider chaos (stop VMs, detach volumes, AZ outage)
- Cluster state chaos (delete CRDs, corrupt configmaps, scale to 0)

Respond with ONLY a JSON object, no other text:
{
  "chaos_relevant": true/false,
  "failure_mode": "brief description of the failure mode" or null,
  "injection_method": "which krkn injection type would test this" or null,
  "skip_reason": "why this is not chaos-relevant" or null,
  "confidence": 0.0-1.0
}"""


def llm_filter_bug(bug: Bug, model: str = "llama3") -> FilterResult:
    """Use LLM to determine if a bug is chaos-relevant.

    Falls back to the keyword filter result if LLM fails.
    """
    prompt = f"""Analyze this OpenShift bug for chaos test relevance:

Bug Key: {bug.key}
Component: {bug.component}
Priority: {bug.priority}
Summary: {bug.summary}
Description: {bug.description[:1500] if bug.description else 'No description'}

Is this bug chaos-relevant? Respond with JSON only."""

    try:
        response = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            options={"temperature": 0.1, "num_predict": 300},
        )

        text = response["message"]["content"].strip()

        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        return FilterResult(
            bug=bug,
            chaos_relevant=result.get("chaos_relevant", False),
            failure_mode=result.get("failure_mode"),
            injection_method=result.get("injection_method"),
            skip_reason=result.get("skip_reason"),
        )

    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.warning("LLM filter failed for %s, falling back to keyword: %s", bug.key, e)
        from src.filter.chaos_filter import filter_bug
        return filter_bug(bug)


def llm_filter_bugs(
    bugs: list[Bug], model: str = "llama3"
) -> tuple[list[FilterResult], list[FilterResult]]:
    """Filter bugs using LLM with fallback to keyword filter."""
    relevant = []
    skipped = []

    for i, bug in enumerate(bugs):
        logger.info("LLM filtering %d/%d: %s", i + 1, len(bugs), bug.key)
        result = llm_filter_bug(bug, model)
        if result.chaos_relevant:
            relevant.append(result)
            logger.info("  PASS: %s (%s)", result.failure_mode, result.injection_method)
        else:
            skipped.append(result)
            logger.info("  SKIP: %s", result.skip_reason)

    logger.info("LLM filter: %d relevant, %d skipped", len(relevant), len(skipped))
    return relevant, skipped
