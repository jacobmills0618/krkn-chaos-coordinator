"""LLM reasoning for MAP and ANALYZE phases.

ChromaDB retrieves candidate docs/scenarios → LLM reasons over those results.
Same RAG pattern as the FILTER phase.
"""

from __future__ import annotations

import json
import logging

from src.filter.llm_config import LLMBackendConfig, detect_llm_backend
from src.filter.llm_filter import call_llm
from src.models import (
    Bug,
    Confidence,
    ActionType,
    FactorConfidence,
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
    failure = filter_result.failure_mode
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

        return ScenarioMatch(
            bug=bug,
            match_result=match_result,
            matched_scenario=matched_scenario,
            matched_repo="krkn-chaos/krkn" if matched_scenario else None,
            similarity_score=1.0 if match_result == MatchResult.FULL_MATCH else 0.5 if match_result == MatchResult.PARTIAL_MATCH else 0.0,
            filter_failure_mode=failure,
            filter_injection_method=injection,
        )

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM MAP failed for %s (bad response), falling back: %s", bug.key, e)
        from dataclasses import replace
        return replace(
            _fallback_match(bug, scenario_hits),
            filter_failure_mode=failure,
            filter_injection_method=injection,
        )
    except Exception as e:
        logger.warning("LLM MAP failed for %s, falling back: %s", bug.key, e)
        from dataclasses import replace
        return replace(
            _fallback_match(bug, scenario_hits),
            filter_failure_mode=failure,
            filter_injection_method=injection,
        )


def _fallback_match(bug: Bug, scenario_hits: list[dict]) -> ScenarioMatch:
    """Threshold-based fallback when LLM is unavailable."""
    if not scenario_hits:
        return ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)

    best_dist = scenario_hits[0].get("distance", 1.0)
    best_text = scenario_hits[0].get("text", "")

    scenario_path = None
    if "Scenario file:" in best_text:
        scenario_path = best_text.split("Scenario file:")[1].split("\n")[0].strip()

    if best_dist < 0.35 and scenario_path:
        return ScenarioMatch(
            bug=bug,
            match_result=MatchResult.FULL_MATCH,
            matched_scenario=scenario_path,
            matched_repo="krkn-chaos/krkn",
            similarity_score=1.0 - best_dist,
        )

    if best_dist < 0.65:
        return ScenarioMatch(
            bug=bug,
            match_result=MatchResult.PARTIAL_MATCH,
            matched_scenario=scenario_path,
            matched_repo="krkn-chaos/krkn",
            similarity_score=1.0 - best_dist,
        )

    return ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH)


ANALYZE_SYSTEM_PROMPT = """You are a chaos engineering expert for OpenShift/Kubernetes using the krkn chaos testing framework.

You are given:
1. A JIRA bug with its failure mode
2. The closest existing krkn scenario from MAP (if any)
3. Relevant OCP architecture documentation
4. Available krkn plugins and their capabilities
5. Previously resolved similar bugs (from Neo4j history)
6. A live catalog of plugin directories and scenario files from the local krkn clone

Your job: analyze the gap and produce a SPECIFIC recommendation for how to fill it.

Prefer causal-chain injection over symptom surrogates (e.g. NetworkPolicy density → OVS
flow pressure, not a CPU hog that only shares CNI timeout symptoms). If the catalog cannot
inject the real cause, prefix injection_method with "SURROGATE:" and set plugin confidence LOW.

Scoring guide (0-100):
- Can you explain exact reproduction steps? (+20) → reproduction
- Is there an existing scenario YAML to extend? (+25) → scenario (Extendable Scenario)
- Do you understand HOW the component fails from the docs? (+20) → understanding
- Is there a krkn plugin that injects this failure (real cause, not unlabeled surrogate)? (+15) → plugin (Injection Capability)
- Does this match the agent's domain? (+10) → domain
- Have we solved a similar bug before? (+10) → history

For each scoring item, set confidence_factors.<key> to
{"level": "high"|"low", "reason": "short why including +/- points"}.

Issue fields below are copied into the GitHub issue verbatim. Do not invent plugin
names or scenario paths outside the live catalog.

- krkn_plugin: prefer full path ``krkn/scenario_plugins/<dir>/`` from the catalog
- starter_scenario: a catalog scenarios/*.yml that uses THAT same plugin (shape reference)
- configuration: one paragraph for implementers (plugin path, starter YAML, and if MAP's
  closest file is a different plugin, say so and warn not to copy it as the starter)
- related_map_note: if MAP matched_scenario is a different plugin than krkn_plugin, say so;
  otherwise null or a short "same plugin / related failure mode" note

Respond with ONLY a JSON object:
{
  "confidence_score": 0-100,
  "confidence_factors": {
    "reproduction": {"level": "high" or "low", "reason": "..."},
    "scenario": {"level": "high" or "low", "reason": "..."},
    "understanding": {"level": "high" or "low", "reason": "..."},
    "plugin": {"level": "high" or "low", "reason": "..."},
    "domain": {"level": "high" or "low", "reason": "..."},
    "history": {"level": "high" or "low", "reason": "..."}
  },
  "reasoning": "Detailed explanation of the score breakdown and analysis",
  "failure_mode": "one-sentence uncovered failure mode",
  "injection_method": "causal injection steps; prefix SURROGATE: if not the real cause",
  "configuration": "Configuration paragraph for the GitHub issue",
  "starter_scenario": "scenarios/.../*.yml from catalog" or null,
  "related_map_note": "note about MAP file vs chosen plugin" or null,
  "modifications": ["specific step 1", "specific step 2", ...],
  "krkn_plugin": "krkn/scenario_plugins/<dir>/" or null,
  "repos_to_update": ["krkn", "krkn-hub", "website"]
}

If confidence_score >= 40, krkn_plugin MUST be set from the catalog (never null).
Only use null when confidence_score < 40."""


def _factor_from_value(value: object) -> FactorConfidence:
    if isinstance(value, FactorConfidence):
        return value
    if isinstance(value, dict):
        return _factor_from_value(value.get("level") or value.get("value"))
    if isinstance(value, str) and value.strip().lower() in ("high", "h"):
        return FactorConfidence.HIGH
    return FactorConfidence.LOW


def _reason_from_value(value: object) -> str | None:
    if isinstance(value, dict):
        reason = value.get("reason") or value.get("why") or value.get("explanation")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


_FACTOR_SHORT_KEYS = (
    ("reproduction", "reproduction_confidence"),
    ("scenario", "scenario_confidence"),
    ("understanding", "understanding_confidence"),
    ("plugin", "plugin_confidence"),
    ("domain", "domain_confidence"),
    ("history", "history_confidence"),
)


def derive_confidence_factors(
    bug: Bug,
    match: ScenarioMatch,
    *,
    krkn_plugin: str | None = None,
    neo4j_history: list[dict] | None = None,
    llm_factors: dict | None = None,
) -> tuple[dict[str, FactorConfidence], tuple[tuple[str, str], ...]]:
    """Compute HIGH/LOW and reasons for each ANALYZE scoring category.

    HIGH means that category contributed points; LOW means zero points.
    When ``llm_factors`` is provided, those values win for keys the model set.
    """
    has_repro = bool(bug.description and len(bug.description) > 200)
    has_scenario = match.match_result == MatchResult.PARTIAL_MATCH or bool(
        match.matched_scenario
    )
    has_understanding = has_repro
    has_plugin = bool(krkn_plugin)
    has_domain = True
    has_history = bool(neo4j_history)

    derived = {
        "reproduction_confidence": (
            FactorConfidence.HIGH if has_repro else FactorConfidence.LOW
        ),
        "scenario_confidence": (
            FactorConfidence.HIGH if has_scenario else FactorConfidence.LOW
        ),
        "understanding_confidence": (
            FactorConfidence.HIGH if has_understanding else FactorConfidence.LOW
        ),
        "plugin_confidence": (
            FactorConfidence.HIGH if has_plugin else FactorConfidence.LOW
        ),
        "domain_confidence": (
            FactorConfidence.HIGH if has_domain else FactorConfidence.LOW
        ),
        "history_confidence": (
            FactorConfidence.HIGH if has_history else FactorConfidence.LOW
        ),
    }
    reasons: dict[str, str] = {
        "reproduction_confidence": (
            "Clear reproduction detail in bug description (+20)"
            if has_repro
            else "Reproduction steps not clear enough (+0)"
        ),
        "scenario_confidence": (
            f"Existing scenario to extend: {match.matched_scenario} (+25)"
            if has_scenario and match.matched_scenario
            else "No existing scenario to extend (+0)"
        ),
        "understanding_confidence": (
            "Failure mechanism clear enough from bug/docs (+20)"
            if has_understanding
            else "Failure mechanism not clear from docs (+0)"
        ),
        "plugin_confidence": (
            f"Plugin identified: {krkn_plugin} (+15)"
            if has_plugin
            else "No matching krkn plugin identified (+0)"
        ),
        "domain_confidence": "Matches agent domain (+10)",
        "history_confidence": (
            "Similar resolved bug found (+10)"
            if has_history
            else "No similar resolved bug found (+0)"
        ),
    }

    if isinstance(llm_factors, dict):
        for raw_key, field_name in _FACTOR_SHORT_KEYS:
            if raw_key not in llm_factors:
                continue
            raw_val = llm_factors[raw_key]
            derived[field_name] = _factor_from_value(raw_val)
            llm_reason = _reason_from_value(raw_val)
            if llm_reason:
                reasons[field_name] = llm_reason

    reason_tuples = tuple(
        (field, reasons[field]) for _, field in _FACTOR_SHORT_KEYS
    )
    return derived, reason_tuples


def _normalize_plugin_path(raw: str | None, plugins: list[str]) -> str | None:
    """Accept catalog dir or full path; return ``krkn/scenario_plugins/<dir>/``.

    Fails closed when ``plugins`` is empty (catalog unavailable) so LLM names
    are never accepted without membership validation.
    """
    if not raw or not isinstance(raw, str):
        return None
    if not plugins:
        return None
    value = raw.strip().strip("`")
    if value.startswith("krkn/scenario_plugins/"):
        value = value.removeprefix("krkn/scenario_plugins/").strip("/")
    value = value.split("/")[0].lower().replace("-", "_")
    if not value:
        return None
    catalog = {d.lower().replace("-", "_"): d for d in plugins}
    if value not in catalog:
        return None
    return f"krkn/scenario_plugins/{catalog[value]}/"


def _normalize_starter_scenario(
    raw: str | None,
    scenarios: list[str],
) -> str | None:
    """Accept a catalog scenario path; reject unknowns (fail closed if empty)."""
    if not raw or not isinstance(raw, str):
        return None
    if not scenarios:
        return None
    value = raw.strip().strip("`").lstrip("./").replace("\\", "/")
    if not value.startswith("scenarios/"):
        return None

    by_norm: dict[str, str] = {}
    for path in scenarios:
        key = path.replace("\\", "/")
        by_norm[key] = path
        if key.endswith(".yaml"):
            by_norm[key[:-5] + ".yml"] = path
        elif key.endswith(".yml"):
            by_norm[key[:-4] + ".yaml"] = path

    return by_norm.get(value)


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def llm_analyze_gap(
    bug: Bug,
    match: ScenarioMatch,
    ocp_docs: list[dict],
    krkn_docs: list[dict],
    neo4j_history: list[dict],
    config: LLMBackendConfig | None = None,
    krkn_catalog: dict | None = None,
) -> GapAnalysis:
    """Use LLM to analyze a coverage gap and produce specific recommendations.

    Args:
        bug: The JIRA bug.
        match: ScenarioMatch from MAP phase (PARTIAL or NO_MATCH).
        ocp_docs: ChromaDB OCP doc search results for component context.
        krkn_docs: ChromaDB krkn doc search results for available plugins.
        neo4j_history: Similar resolved bugs from Neo4j.
        config: LLM backend config. Auto-detected if None.
        krkn_catalog: Optional prebuilt catalog from ``build_krkn_catalog``
            (preferred once per ANALYZE run). Built on demand if omitted.

    Returns:
        GapAnalysis with LLM-generated confidence score, reasoning, and modifications.
    """
    if config is None:
        config = detect_llm_backend(phase="analyze")

    from src.knowledge.scenario_index import (
        build_krkn_catalog,
        format_krkn_catalog_for_prompt,
    )

    catalog = krkn_catalog if krkn_catalog is not None else build_krkn_catalog()
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

    filter_bits = []
    if match.filter_failure_mode:
        filter_bits.append(f"FILTER failure_mode: {match.filter_failure_mode}")
    if match.filter_injection_method:
        filter_bits.append(f"FILTER injection_method: {match.filter_injection_method}")
    filter_section = ("\n".join(filter_bits) + "\n") if filter_bits else ""

    prompt = f"""Bug: {bug.key}
Component: {bug.component}
Summary: {bug.summary}
Release Status: {fix_info}
Description: {bug.description[:1000] if bug.description else 'No description'}
{filter_section}
Match result: {match.match_result.value}
{scenario_context}

OCP Architecture Documentation:
{ocp_context}

Available krkn Plugins:
{krkn_context}

Previously Resolved Similar Bugs:
{history_context}

Live krkn catalog (prefer these paths; do not invent names):
{catalog_block}

Analyze this gap. Score confidence and provide SPECIFIC modifications."""

    try:
        text = call_llm(
            messages=[
                {"role": "user", "content": prompt},
            ],
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
        krkn_plugin = _normalize_plugin_path(result.get("krkn_plugin"), plugins)
        # Scores >= 40 require a catalog-validated plugin; otherwise downgrade.
        if score >= 40 and not krkn_plugin:
            score = 39

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

        failure_mode = _optional_str(result.get("failure_mode")) or match.filter_failure_mode
        injection_method = (
            _optional_str(result.get("injection_method"))
            or match.filter_injection_method
        )

        if score >= 70:
            confidence = Confidence.HIGH
            action = ActionType.DRAFT_PR
        elif score >= 40:
            confidence = Confidence.MEDIUM
            action = ActionType.GITHUB_ISSUE
        else:
            confidence = Confidence.LOW
            action = ActionType.GITHUB_ISSUE

        factors, factor_reasons = derive_confidence_factors(
            bug,
            match,
            krkn_plugin=krkn_plugin,
            neo4j_history=neo4j_history,
            llm_factors=result.get("confidence_factors"),
        )

        return GapAnalysis(
            bug=bug,
            confidence_score=score,
            confidence_level=confidence,
            action_type=action,
            reasoning=reasoning,
            failure_mode=failure_mode,
            injection_method=injection_method,
            configuration=_optional_str(result.get("configuration")),
            related_map_note=_optional_str(result.get("related_map_note")),
            starter_scenario=_normalize_starter_scenario(
                result.get("starter_scenario"),
                catalog.get("scenarios") or [],
            ),
            base_scenario=match.matched_scenario,
            krkn_plugin=krkn_plugin,
            filter_injection_method=match.filter_injection_method,
            modifications=modifications,
            confidence_factor_reasons=factor_reasons,
            **factors,
        )

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("LLM ANALYZE failed for %s (bad response), falling back: %s", bug.key, e)
        return _fallback_analyze(bug, match)
    except Exception as e:
        logger.warning("LLM ANALYZE failed for %s, falling back: %s", bug.key, e)
        return _fallback_analyze(bug, match)


def _fallback_analyze(bug: Bug, match: ScenarioMatch) -> GapAnalysis:
    """Keyword-based fallback when LLM is unavailable."""
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

    factors, factor_reasons = derive_confidence_factors(bug, match)

    return GapAnalysis(
        bug=bug,
        confidence_score=score,
        confidence_level=confidence,
        action_type=action,
        reasoning="; ".join(reasoning_parts),
        failure_mode=match.filter_failure_mode,
        injection_method=match.filter_injection_method,
        base_scenario=match.matched_scenario,
        filter_injection_method=match.filter_injection_method,
        modifications=modifications,
        confidence_factor_reasons=factor_reasons,
        **factors,
    )
