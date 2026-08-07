# Interpreting scan results

After a full `/krkn-chaos-scan` (or `python src/main.py …`), Claude presents a **results page** built from the pipeline’s run summary. This guide explains each section, how to read the numbers, and what to do next.

Pipeline phases are documented separately: [Pipeline phases](pipeline.md).

---

## What you should see

A normal interactive full-scan results page has two tables (in this order), then an optional GitHub-issue prompt:

1. **Per-agent results table** (canonical summary)
2. **Gaps by confidence** (only when gaps > 0)
3. **Post gaps?** — Ask which gaps to open as GitHub issues

Do **not** expect a Metric/Count rollup (single column of totals like “Bugs discovered / Passed filter / Already known”). Those aggregates hide per-agent behavior. Prefer the per-agent table.

CLI runs print the same metrics in text form via `format_summary` (Run Summary), then an approval queue when gaps exist.

---

## 1. Per-agent results table

Example shape:

| Agent | Discovered | Passed Filter | Skipped | Gaps | LLM Calls | ANALYZE | Cost |
|-------|------------|---------------|---------|------|-----------|---------|------|
| control_plane | 65 | 16 | 49 | 16 | 40 | 182.3s | $0.78 |
| networking | 42 | 7 | 35 | 7 | 18 | 61.0s | $0.29 |
| **TOTAL** | **297** | **48** | **244** | **48** | **…** | **…** | **~$4.33** |

### Column meanings

| Column | Meaning |
|:-------|:--------|
| **Agent** | Domain agent that ran (`control_plane`, `networking`, …). |
| **Discovered** | Bugs JIRA returned for this agent’s components (includes ones already known in Neo4j). |
| **Passed Filter** | New bugs that passed FILTER **this run** (chaos-relevant, or virt-domain PASS on domain-only scans). |
| **Skipped** | New bugs FILTER rejected **this run**. |
| **Gaps** | Bugs that reached ANALYZE as coverage gaps (PARTIAL / NO_MATCH from MAP). |
| **LLM Calls** | FILTER + MAP + ANALYZE LLM invocations for this agent (0 / `free` path when `--use-llm` was off). |
| **ANALYZE** | Wall time spent in the ANALYZE phase for this agent (seconds). |
| **Cost** | Estimated LLM spend for this agent (`free` when no billed usage). |

A **TOTAL** row sums agents for that scan.

### How the counts relate

```
Discovered = (new bugs filtered this run) + (already known in Neo4j)
new filtered this run = Passed Filter + Skipped
Gaps ⊆ Passed Filter   (only PASS bugs that MAP marked uncovered / partial)
```

Important:

- **Known (Neo4j)** bugs are **not** re-filtered. They inflate **Discovered** but do not appear in Passed / Skipped for this run.
- **Passed ≠ Discovered − Skipped** when Neo4j already knows some bugs.
- Parallel multi-agent runs may overlap the same JIRA bug across agents; the orchestrator **deduplicates gaps by bug key** (keeps the higher confidence) before the Gaps table.

### What “good” looks like

| Signal | Healthy | Investigate |
|:-------|:--------|:------------|
| Passed / Skipped ratio | Roughly in line with past runs for that agent | Sudden 0 PASS or almost all PASS (filter drift / wrong stage) |
| Gaps vs Passed | Gaps ≤ Passed; many FULL_MATCH → fewer gaps | Gaps ≈ Passed always → MAP may be too aggressive |
| LLM Calls | Scales with PASS count when `--use-llm` | Very high calls + few gaps → retries / escalations |
| ANALYZE time | Roughly proportional to gap count | Huge time, few gaps → provider slowness or stuck calls |
| Cost | Matches LLM Calls × model | Spike with keyword-only scan → LLM somehow still on |
| Virtualization / 0 gaps | Possible on short lookbacks | Domain-only vs chaos stage: check filter review if enabled |

Optional one-liners under the table (known Neo4j totals, token notes) are fine. A second Metric/Count table is not.

---

## 2. Gaps by confidence

Example shape:

| # | Confidence | Bug | Component | Action |
|---|------------|-----|-----------|--------|
| 1 | HIGH 73 | OCPBUGS-99291 — … | Networking / ovn-kubernetes | Draft PR |
| 2 | MEDIUM 65 | OCPBUGS-99845 — … | Monitoring | GitHub Issue |
| 3 | LOW 20 | OCPBUGS-… — … | oauth-apiserver | GitHub Issue |

Sorted by confidence descending (same as the approval queue).

### Confidence bands → action

| Score | Level | Typical action |
|:------|:------|:---------------|
| **70–100** | HIGH | Draft PR candidate (strong repro + scenario/plugin fit) |
| **40–69** | MEDIUM | GitHub issue with concrete recommendation |
| **0–39** | LOW | GitHub issue describing the gap (thin recommendation) |

ANALYZE scores six factors (repro, extendable scenario, docs understanding, plugin fit, domain, history). Details: [ANALYZE](pipeline.md#analyze).

### Notes under the table

Claude may call out caveats, for example:

- **LLM ANALYZE failures / fallback scoring** — compact heuristic scores (often ~20/100); treat as weak signal until re-run.
- **Pre-bootstrap / install-time failures** — correctly LOW if krkn cannot inject that phase.
- **Z-stream already fixed** — may still show as a historical gap; check fixed-in / changelog enrichment.

---

## 3. Post gaps to GitHub

After the tables, you are asked which gaps to open on `krkn-chaos/krkn` (or your configured fork).

- Prefer HIGH first, then MEDIUM with clear injection methods.
- Skip LOW unless you want a tracking issue for a hard-to-automate gap.
- Choosing **None** leaves Neo4j memory intact; nothing is posted.

Issue bodies include confidence, reasoning, plugin/scenario hints, and factor breakdown when available ([ACT](pipeline.md#act)).

---

## CLI vs Claude presentation

| Surface | What you get |
|:--------|:-------------|
| **`format_summary` (CLI stdout)** | Per-agent text: Discovered, Passed, Skipped, Matched, Gaps, LLM calls, ANALYZE seconds, Cost + TOTAL line |
| **Approval queue (CLI)** | Numbered gaps with Approve/Edit/Reject prompts when posting interactively |
| **`/krkn-chaos-scan` results page** | Same metrics as markdown tables (per-agent + Gaps by confidence), then AskUserQuestion for issues |

Status lines during the run (`[agent] ANALYZE — N gaps scored (12.4s)`) are live progress. The **results page / Run Summary** is the durable place to read ANALYZE time and LLM calls after the scan.

---

## Edge cases and modes

| Situation | What to expect |
|:----------|:---------------|
| **0 gaps** | Per-agent table still shown; no Gaps table; message that no coverage gaps were found |
| **Keyword-only** (no `--use-llm`) | LLM Calls ≈ 0, Cost `free`; scoring is thinner |
| **`--domain-filter-only`** | PASS means virt-relevant (ocp-virt keywords), not necessarily chaos-testable; often followed by filter review |
| **Targeted `/krkn-chaos-scan …` query** | May skip the full two-table page and answer the question directly |
| **Neo4j briefly down** | Writes may drop for that window; later agents still persist once reconnect/reboot succeeds |
| **Same bug, multiple agents** | One row in Gaps by confidence after dedup |

---

## Quick checklist

1. Confirm the **per-agent** table (not a Metric/Count rollup).
2. Check TOTAL Passed / Gaps / Cost against your lookback and agent set.
3. Open **HIGH** gaps first; skim MEDIUM; treat LOW + “fallback” notes cautiously.
4. Approve GitHub issues only for gaps you want tracked upstream.
5. Re-run later: Neo4j **known** counts should rise and re-work on the same bugs should shrink.
