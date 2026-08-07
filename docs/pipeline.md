# Pipeline phases

Every domain agent runs the same six stages:

**[DISCOVER](#discover) → [FILTER](#filter) → [MAP](#map) → [ANALYZE](#analyze) → [ACT](#act) → [REMEMBER](#remember)**

This page explains each phase in depth. The [README](../README.md) keeps a one-line summary.

---

## DISCOVER

**Question:** Which bugs touch this agent’s OpenShift components?

**What happens**

- Queries JIRA (`OCPBUGS`) for the agent’s configured components.
- Prefer Critical / Major / Blocker when prioritizing.
- Supports **4-tier version matching** when a target release is set (for example `4.21`):
  1. Bugs tagged with that release
  2. Open bugs from older versions (still unfixed)
  3. Open bugs from newer versions (likely present on the target too)
  4. Bugs with no `affectedVersion`
- Enriches with z-stream / changelog context when available.
- Optional **JQL text backfill** when component hits are sparse (used heavily for virtualization / CNV bugs filed under varied components).

**Outputs:** A list of candidate bugs for FILTER.

---

## FILTER

**Question:** Is this chaos-testable? Can krkn inject something like it?

**What happens** — three tiers, cheapest first:

1. **Keyword pre-filter** — Instant. Merges `config/filters/common.yaml` with per-agent keywords from `config/agents/<name>.yaml`. Optional **ocp-virt** domain keywords (`config/filters/ocp-virt.yaml`) can apply for virtualization-focused runs.
2. **Semantic cache** — Reuses past LLM decisions for similar bug summaries (ChromaDB).
3. **LLM classification** — Decides chaos-relevant vs skip; may escalate to a stronger model when uncertain.

Optional **filter review** (especially virtualization): interactive or JSON export of PASS/SKIP lists for human checking.

**Keep** resilience failure modes (crash, partition, OOM, timeout under stress, …).  
**Skip** docs typos, flaky CI, UI polish, and similar.

**Outputs:** Chaos-relevant bugs with a failure mode and injection hint for MAP.

---

## MAP

**Question:** Do we already have a krkn scenario for this?

**What happens**

- Searches ChromaDB (scenario + docs embeddings) for related coverage.
- LLM reasons over closest hits and classifies:

| Result | Meaning |
|:-------|:--------|
| **FULL_MATCH** | Already covered — no gap |
| **PARTIAL_MATCH** | Related scenario exists, not exact |
| **NO_MATCH** | No useful coverage — gap |

**Outputs:** Match result + closest scenario path (if any). PARTIAL / NO_MATCH go to ANALYZE.

---

## ANALYZE

**Question:** How strong is this gap, and how should we fill it?

**What happens**

- Builds a **live catalog** of krkn plugins and scenario files from the local krkn clone (once per run).
- Scores confidence **0–100** from six factors:

| Factor | Max | Asks |
|:-------|:----|:-----|
| Reproduction | +20 | Clear repro steps? |
| Extendable scenario | +25 | Existing YAML to extend? |
| Understanding | +20 | Do we know how it fails? |
| Injection capability | +15 | Plugin can inject the *real* cause? |
| Domain | +10 | Belongs to this agent? |
| History | +10 | Similar gap solved before? |

- Prefers **causal-chain injection** over symptom surrogates. Surrogates should be labeled (`SURROGATE:`) with low injection confidence.
- Emits recommendation fields: failure mode, injection method, `krkn_plugin` path, starter scenario, configuration note, next steps.

**Score → action**

| Score | Level | Next |
|:------|:------|:-----|
| 70–100 | HIGH | Draft PRs in `krkn`, `krkn-hub`, and `website` |
| 40–69 | MEDIUM | GitHub issue **with** a concrete recommendation |
| 0–39 | LOW | GitHub issue **describing the gap only** (no recommendation) |

**MAP vs ANALYZE:** MAP finds the closest existing file; ANALYZE decides the recommendation. If they disagree on plugin, the issue should say so.

**Outputs:** `GapAnalysis` for ACT.

---

## ACT

**Question:** What should humans review?

**What happens**

- **HIGH (70–100):** Draft PRs across `krkn`, `krkn-hub`, and `website`.
- **MEDIUM (40–69):** GitHub issue with passthrough ANALYZE fields and a concrete recommendation (plugin, injection, next steps).
- **LOW (0–39):** GitHub issue that describes the detected gap/problem only — no implementation recommendation.

**Typical issue contents**

- Header: bug link, component, confidence + factor HIGH/LOW labels
- Failure mode (and honest MAP coverage wording when a related scenario exists)
- Analysis narrative
- For **MEDIUM/HIGH:** how to chaos test (injection method, plugin path, configuration), related scenario, next steps, repos to update
- For **LOW:** gap description only — omit actionable “build this scenario” recommendations
- Confidence breakdown with reasons

Humans still approve; nothing merges itself.

**Outputs:** Issue/PR URLs (or dry-run previews).

---

## REMEMBER

**Question:** Don’t repeat the same work.

**What happens**

- Writes bugs, gaps, runs, and actions into **Neo4j**.
- Next runs skip already-analyzed bug keys.
- Enables history lookup for ANALYZE (“have we solved something similar?”).

**Outputs:** Durable memory for the next coordinator run.

---

## Related

- [Interpreting scan results](scan-results.md)
- [README pipeline summary](../README.md#pipeline)
- [Project overview (visual)](project-overview.html)
- [Agent config](../config/agents/README.md)
- [Filter keywords](../config/filters/README.md)
