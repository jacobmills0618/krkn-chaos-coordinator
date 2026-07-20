"""LLM reasoning for MAP and ANALYZE phases.

ChromaDB retrieves candidate docs/scenarios → LLM reasons over those results.
Same RAG pattern as the FILTER phase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from src.filter.llm_config import LLMBackendConfig, detect_llm_backend
from src.filter.llm_filter import call_llm
from src.models import (
    Bug,
    Confidence,
    ActionType,
    FilterResult,
    GapAnalysis,
    MatchResult,
    ScenarioMatch,
)

logger = logging.getLogger(__name__)

MAP_SYSTEM_PROMPT = """You are a chaos engineering expert for OpenShift/Kubernetes.

You are given:
1. A JIRA bug with its failure mode and injection method
2. Existing krkn chaos scenario configs (from ChromaDB search)
3. Relevant documentation context

Your job: determine if any existing krkn scenario ALREADY covers the exact failure mode in this bug.

Rules:
- FULL_MATCH: A scenario tests this EXACT failure mode. The bug's failure condition is already injected by the scenario.
- PARTIAL_MATCH: A scenario targets the same component or similar failure, but does NOT cover this specific failure mode. Example: scenario kills etcd pods, but the bug is about etcd under CPU stress.
- NO_MATCH: No scenario is related to this failure mode at all.

Be strict about FULL_MATCH — the scenario must inject the same type of disruption that triggers the bug. Same component is not enough.

Respond with ONLY a JSON object:
{
  "match": "FULL_MATCH" | "PARTIAL_MATCH" | "NO_MATCH",
  "matched_scenario": "path/to/scenario.yaml" or null,
  "explanation": "What the closest scenario tests vs what the bug describes"
}"""


def llm_map_match(
    bug: Bug,
    filter_result: FilterResult,
    scenario_hits: list[dict],
    doc_hits: list[dict],
    config: LLMBackendConfig | None = None,
    kb_context: dict | None = None,
) -> ScenarioMatch:
    """Use LLM to determine if existing scenarios cover a bug's failure mode.

    Args:
        bug: The JIRA bug to match.
        filter_result: FILTER output with failure_mode and injection_method.
        scenario_hits: ChromaDB scenario search results.
        doc_hits: ChromaDB doc search results for context.
        config: LLM backend config. Auto-detected if None.
        kb_context: Matching krkn-knowledgebase scenario (if any) showing
            what krkn CAN build for this failure mode.

    Returns:
        ScenarioMatch with LLM-reasoned match result.
    """
    if config is None:
        config = detect_llm_backend(phase="map")

    scenario_context = "\n---\n".join(
        hit["text"][:500] for hit in scenario_hits[:5]
    ) or "No matching scenarios found."

    doc_context = "\n---\n".join(
        hit["text"][:300] for hit in doc_hits[:3]
    ) or "No relevant documentation found."

    if bug.fixed_in_release:
        commit_detail = ""
        if bug.fix_commits:
            commit_lines = "; ".join(bug.fix_commits[:3])
            commit_detail = f" Fix: {commit_lines}"
        fix_info = f"Fixed in {bug.fixed_in_release} ({bug.fix_image or 'unknown'}).{commit_detail}"
    else:
        fix_info = "Not yet fixed in any z-stream."

    if kb_context:
        kb_section = (
            f"\nkrkn-knowledgebase: scenario '{kb_context['scenario_name']}' "
            f"({kb_context['title']}) is available to build. "
            f"Parameters: {', '.join(kb_context['parameters'])}. "
            f"{kb_context['description']}"
        )
    else:
        kb_section = ""

    prompt = f"""Bug: {bug.key}
Component: {bug.component}
Summary: {bug.summary}
Failure Mode: {filter_result.failure_mode or 'unknown'}
Injection Method: {filter_result.injection_method or 'unknown'}
Release Status: {fix_info}
Description: {bug.description[:800] if bug.description else 'No description'}

Existing krkn scenarios (from search):
{scenario_context}

Relevant OCP/krkn documentation:
{doc_context}
{kb_section}

Does any existing scenario cover this bug's exact failure mode?"""

    injection = filter_result.injection_method
    try:
        text = call_llm(
            messages=[
                {"role": "user", "content": prompt},
            ],
            config=config,
            system_prompt=MAP_SYSTEM_PROMPT,
        )

        # Extract JSON from response
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        match_str = result.get("match", "NO_MATCH")
        matched_scenario = result.get("matched_scenario")

        match_result = {
            "FULL_MATCH": MatchResult.FULL_MATCH,
            "PARTIAL_MATCH": MatchResult.PARTIAL_MATCH,
        }.get(match_str, MatchResult.NO_MATCH)

        match = ScenarioMatch(
            bug=bug,
            match_result=match_result,
            matched_scenario=matched_scenario,
            matched_repo="krkn-chaos/krkn" if matched_scenario else None,
            similarity_score=1.0 if match_result == MatchResult.FULL_MATCH else 0.5 if match_result == MatchResult.PARTIAL_MATCH else 0.0,
            filter_injection_method=injection,
        )
        return _with_soft_map_base(match, scenario_hits)

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM MAP failed for %s (bad response), falling back: %s", bug.key, e)
        return replace(_fallback_match(bug, scenario_hits), filter_injection_method=injection)
    except Exception as e:
        logger.warning("LLM MAP failed for %s, falling back: %s", bug.key, e)
        return replace(_fallback_match(bug, scenario_hits), filter_injection_method=injection)


def _scenario_path_from_hit(hit: dict) -> str | None:
    text = hit.get("text") or ""
    if "Scenario file:" in text:
        return text.split("Scenario file:")[1].split("\n")[0].strip() or None
    meta = hit.get("metadata") or {}
    for key in ("source", "path", "file_path"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _with_soft_map_base(match: ScenarioMatch, scenario_hits: list[dict]) -> ScenarioMatch:
    """If MAP has no scenario, keep the top Chroma hit as a soft PARTIAL base."""
    if match.matched_scenario and match.match_result != MatchResult.NO_MATCH:
        return match
    if not scenario_hits:
        return match
    path = _scenario_path_from_hit(scenario_hits[0])
    dist = float(scenario_hits[0].get("distance", 1.0))
    if not path or dist > 0.85:
        return match
    return ScenarioMatch(
        bug=match.bug,
        match_result=MatchResult.PARTIAL_MATCH,
        matched_scenario=path,
        matched_repo="krkn-chaos/krkn",
        similarity_score=max(0.0, (1.0 - dist) * 0.5),
        filter_injection_method=match.filter_injection_method,
    )


def _fallback_match(bug: Bug, scenario_hits: list[dict]) -> ScenarioMatch:
    """Threshold-based fallback when LLM is unavailable."""
    if not scenario_hits:
        return ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)

    best_dist = scenario_hits[0].get("distance", 1.0)
    scenario_path = _scenario_path_from_hit(scenario_hits[0])

    if best_dist < 0.35 and scenario_path:
        return ScenarioMatch(
            bug=bug,
            match_result=MatchResult.FULL_MATCH,
            matched_scenario=scenario_path,
            matched_repo="krkn-chaos/krkn",
            similarity_score=1.0 - best_dist,
        )

    if best_dist < 0.65 and scenario_path:
        return ScenarioMatch(
            bug=bug,
            match_result=MatchResult.PARTIAL_MATCH,
            matched_scenario=scenario_path,
            matched_repo="krkn-chaos/krkn",
            similarity_score=1.0 - best_dist,
        )

    # Soft base for ACT plugin derivation when distance is weak but a path exists
    return _with_soft_map_base(
        ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH),
        scenario_hits,
    )


ANALYZE_SYSTEM_PROMPT = """You are a chaos engineering expert for OpenShift/Kubernetes using the krkn chaos testing framework.

You are given:
1. A JIRA bug with its failure mode
2. The closest existing krkn scenario (if any — partial match or no match)
3. Relevant OCP architecture documentation
4. Available krkn plugins and their capabilities
5. Previously resolved similar bugs (from Neo4j history)
6. A live catalog of plugin dirs / scenario files from the local krkn clone

Your job: analyze the gap and produce a SPECIFIC recommendation for how to fill it.

Scoring guide (0-100):
- Can you explain exact reproduction steps? (+20)
- Is there an existing scenario to extend? (+25)
- Do you understand HOW the component fails from the docs? (+20)
- Is there a krkn plugin that injects this exact failure? (+15)
- Does this match the agent's domain? (+10)
- Have we solved a similar bug before? (+10)

For modifications, be SPECIFIC:
- BAD: "extend the etcd scenario"
- GOOD: "Add a test case to scenarios/openshift/etcd.yml that deploys CPU hog pods on master nodes (use hog_scenarios plugin with cpu target 80%, duration 300s). While hog is running, check etcd operator status with: oc get co/etcd -o jsonpath='{.status.conditions}'. Assert: etcd should NOT report Degraded=True while members are actually healthy."

Respond with ONLY a JSON object:
{
  "confidence_score": 0-100,
  "reasoning": "Detailed explanation of the score breakdown and analysis",
  "modifications": ["specific step 1", "specific step 2", ...],
  "krkn_plugin": "plugin directory under krkn/scenario_plugins/ (e.g. network_chaos, hogs, node_actions)" or null,
  "repos_to_update": ["krkn", "krkn-hub", "website"]
}

If confidence_score >= 40, krkn_plugin MUST be a catalog plugin directory (never null).
Only use null when confidence_score < 40."""


def _validate_plugin_choice(raw: str | None, plugins: list[str]) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    value = raw.strip().strip("`")
    if value.startswith("krkn/scenario_plugins/"):
        value = value.removeprefix("krkn/scenario_plugins/").strip("/")
    value = value.split("/")[0]
    if not plugins:
        return value or None
    if value in plugins:
        return value
    lowered = value.lower().replace("-", "_")
    for d in plugins:
        if d == lowered or d in lowered or lowered in d:
            return d
    return None


def compact_pick_plugin(
    bug: Bug,
    match: ScenarioMatch,
    config: LLMBackendConfig | None = None,
) -> str | None:
    """One-shot plugin pick from the local krkn catalog (ANALYZE follow-up)."""
    if config is None:
        config = detect_llm_backend(phase="analyze")
    from src.knowledge.scenario_index import (
        build_krkn_catalog,
        format_krkn_catalog_for_prompt,
    )

    catalog = build_krkn_catalog()
    plugins = list(catalog.get("plugins") or [])
    if not plugins:
        return None
    catalog_block = format_krkn_catalog_for_prompt(catalog)
    prompt = (
        f"Bug: {bug.key}\nSummary: {bug.summary}\n"
        f"Closest scenario: {match.matched_scenario or 'none'}\n\n"
        f"{catalog_block}\n\n"
        'Respond with ONLY JSON: {"krkn_plugin": "<catalog plugin dir>", "reasoning": "..."}'
    )
    try:
        text = call_llm(
            messages=[{"role": "user", "content": prompt}],
            config=config,
            system_prompt="Pick one plugin from the catalog. Do not invent names.",
        )
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return _validate_plugin_choice(json.loads(text).get("krkn_plugin"), plugins)
    except Exception as e:
        logger.warning("compact_pick_plugin failed for %s: %s", bug.key, e)
        return None


def finalize_gap_evidence(gap: GapAnalysis) -> GapAnalysis:
    """Cap at 39/LOW when there is no plugin and no MAP base scenario."""
    if gap.confidence_score < 40 or gap.krkn_plugin or gap.base_scenario:
        return gap
    note = "Score capped at 39: no krkn_plugin and no MAP base_scenario"
    return replace(
        gap,
        confidence_score=39,
        confidence_level=Confidence.LOW,
        action_type=ActionType.GITHUB_ISSUE,
        reasoning=f"{gap.reasoning}; {note}" if gap.reasoning else note,
    )


def llm_analyze_gap(
    bug: Bug,
    match: ScenarioMatch,
    ocp_docs: list[dict],
    krkn_docs: list[dict],
    neo4j_history: list[dict],
    config: LLMBackendConfig | None = None,
) -> GapAnalysis:
    """Use LLM to analyze a coverage gap and produce specific recommendations.

    Args:
        bug: The JIRA bug.
        match: ScenarioMatch from MAP phase (PARTIAL or NO_MATCH).
        ocp_docs: ChromaDB OCP doc search results for component context.
        krkn_docs: ChromaDB krkn doc search results for available plugins.
        neo4j_history: Similar resolved bugs from Neo4j.
        config: LLM backend config. Auto-detected if None.

    Returns:
        GapAnalysis with LLM-generated confidence score, reasoning, and modifications.
    """
    if config is None:
        config = detect_llm_backend(phase="analyze")

    from src.knowledge.scenario_index import (
        build_krkn_catalog,
        format_krkn_catalog_for_prompt,
    )

    catalog = build_krkn_catalog()
    plugins = list(catalog.get("plugins") or [])
    catalog_block = format_krkn_catalog_for_prompt(catalog)

    ocp_context = "\n---\n".join(
        hit["text"][:400] for hit in ocp_docs[:3]
    ) or "No OCP documentation found."

    krkn_context = "\n---\n".join(
        hit["text"][:400] for hit in krkn_docs[:3]
    ) or "No krkn plugin documentation found."

    history_context = "\n".join(
        f"- {h.get('bug_key', '?')}: {h.get('summary', '?')[:60]} → {h.get('issue_url', 'N/A')}"
        for h in neo4j_history[:5]
    ) or "No similar resolved bugs found."

    scenario_context = f"Closest scenario: {match.matched_scenario}" if match.matched_scenario else "No matching scenario found."

    if bug.fixed_in_release:
        commit_detail = ""
        if bug.fix_commits:
            commit_lines = "\n".join(f"  - {c}" for c in bug.fix_commits[:5])
            commit_detail = f"\nFix commits ({bug.fix_image or 'unknown'}):\n{commit_lines}"
        fix_info = f"Fixed in {bug.fixed_in_release}.{commit_detail}\nChaos test is still valuable for regression prevention — the fix could regress in future z-streams."
    else:
        fix_info = "Not yet fixed in any z-stream. Active gap — high priority."

    prompt = f"""Bug: {bug.key}
Component: {bug.component}
Summary: {bug.summary}
Release Status: {fix_info}
Description: {bug.description[:1000] if bug.description else 'No description'}

Match result: {match.match_result.value}
{scenario_context}

Live krkn repository catalog (do not invent plugins):
{catalog_block}

OCP Architecture Documentation:
{ocp_context}

Available krkn Plugins:
{krkn_context}

Previously Resolved Similar Bugs:
{history_context}

Analyze this gap. Score confidence and provide SPECIFIC modifications.
Pick krkn_plugin from the live catalog above."""

    try:
        try:
            text = call_llm(
                messages=[{"role": "user", "content": prompt}],
                config=config,
                system_prompt=ANALYZE_SYSTEM_PROMPT,
            )
        except Exception as e:
            if "timed out" not in str(e).lower():
                raise
            logger.warning("ANALYZE timed out for %s — retrying once", bug.key)
            text = call_llm(
                messages=[{"role": "user", "content": prompt}],
                config=config,
                system_prompt=ANALYZE_SYSTEM_PROMPT,
            )

        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)

        score = min(100, max(0, int(result.get("confidence_score", 0))))

        krkn_plugin = _validate_plugin_choice(result.get("krkn_plugin"), plugins)
        if score >= 40 and not krkn_plugin:
            krkn_plugin = compact_pick_plugin(bug, match, config=config)

        reasoning_parts = []
        if result.get("reasoning"):
            reasoning_parts.append(result["reasoning"])
        if krkn_plugin:
            reasoning_parts.append(f"krkn plugin: {krkn_plugin}")
        if result.get("repos_to_update"):
            reasoning_parts.append(f"Repos: {', '.join(result['repos_to_update'])}")
        reasoning = "; ".join(reasoning_parts)

        modifications = result.get("modifications", [])
        if not isinstance(modifications, list):
            modifications = [str(modifications)]

        if score >= 70:
            confidence = Confidence.HIGH
            action = ActionType.DRAFT_PR
        elif score >= 40:
            confidence = Confidence.MEDIUM
            action = ActionType.GITHUB_ISSUE
        else:
            confidence = Confidence.LOW
            action = ActionType.GITHUB_ISSUE

        return finalize_gap_evidence(GapAnalysis(
            bug=bug,
            confidence_score=score,
            confidence_level=confidence,
            action_type=action,
            reasoning=reasoning,
            base_scenario=match.matched_scenario,
            krkn_plugin=krkn_plugin,
            filter_injection_method=match.filter_injection_method,
            modifications=modifications,
        ))

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM ANALYZE failed for %s (bad response), keeping LOW: %s", bug.key, e)
        return _analyze_failure_gap(bug, match, config, f"bad LLM response: {e}")
    except Exception as e:
        logger.warning("LLM ANALYZE failed for %s, keeping LOW: %s", bug.key, e)
        return _analyze_failure_gap(bug, match, config, str(e))


def _analyze_failure_gap(
    bug: Bug,
    match: ScenarioMatch,
    config: LLMBackendConfig | None,
    reason: str,
) -> GapAnalysis:
    """On LLM ANALYZE failure: stay LOW (no fake MEDIUM from keyword fallback)."""
    plugin = compact_pick_plugin(bug, match, config=config)
    reasoning = f"LLM ANALYZE failed ({reason})"
    if plugin:
        reasoning = f"{reasoning}; compact plugin pick: {plugin}"
    return finalize_gap_evidence(GapAnalysis(
        bug=bug,
        confidence_score=20 if plugin or match.matched_scenario else 0,
        confidence_level=Confidence.LOW,
        action_type=ActionType.GITHUB_ISSUE,
        reasoning=reasoning,
        base_scenario=match.matched_scenario,
        krkn_plugin=plugin,
        filter_injection_method=match.filter_injection_method,
    ))


def _fallback_analyze(bug: Bug, match: ScenarioMatch) -> GapAnalysis:
    """Keyword-based analysis when LLM is disabled (not used as LLM-failure substitute)."""
    score = 0
    reasoning_parts = []

    if bug.description and len(bug.description) > 200:
        score += 20
        reasoning_parts.append("Clear repro steps (+20)")

    if match.match_result == MatchResult.PARTIAL_MATCH:
        score += 25
        reasoning_parts.append(f"Partial match: {match.matched_scenario} (+25)")

    failure_keywords = [
        "timeout", "crash", "unavailable", "degraded", "unhealthy",
        "not cleared", "failure", "failed", "outage", "disruption",
        "quorum", "leader election", "not ready", "eviction",
    ]
    if any(kw in bug.summary.lower() for kw in failure_keywords):
        score += 20
        reasoning_parts.append("Known failure mode (+20)")

    if score >= 70:
        confidence = Confidence.HIGH
        action = ActionType.DRAFT_PR
    elif score >= 40:
        confidence = Confidence.MEDIUM
        action = ActionType.GITHUB_ISSUE
    else:
        confidence = Confidence.LOW
        action = ActionType.GITHUB_ISSUE

    modifications = []
    if match.matched_scenario:
        modifications.append(f"Extend {match.matched_scenario}")

    return finalize_gap_evidence(GapAnalysis(
        bug=bug,
        confidence_score=score,
        confidence_level=confidence,
        action_type=action,
        reasoning="; ".join(reasoning_parts),
        base_scenario=match.matched_scenario,
        filter_injection_method=match.filter_injection_method,
        modifications=modifications,
    ))
