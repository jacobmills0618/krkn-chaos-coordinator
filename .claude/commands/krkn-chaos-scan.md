---
description: Scan JIRA bugs for krkn chaos test coverage gaps — optionally pass a question or filter (e.g. "/krkn-chaos-scan what etcd coverage do we have?")
allowed-tools: Bash, Read, Write, AskUserQuestion, mcp__jira__searchJiraIssuesUsingJql, mcp__github__create_issue
---

# krkn-chaos-scan

You are the AI reasoning engine for krkn-chaos-coordinator. You use ChromaDB (4,089 chunks of krkn + OCP docs) and Neo4j (operational memory) to make intelligent chaos testing decisions.

## User Query

```
$ARGUMENTS
```

## Mode Selection

If the user query above is empty or blank, run in **Interactive Mode** — ask the questions below, then run the full scan.

If the user query is NOT empty, run in **Targeted Query** mode:
- Parse what the user is asking about (component, bug, scenario, coverage area)
- Skip the interactive questions — infer version and agent from context
- Skip to the relevant steps below
- Still be thorough: search scenarios, check docs, reason about gaps

**Example targeted queries:**
- "what etcd bugs and coverage do we have" → Search JIRA for etcd component bugs + search ChromaDB for etcd scenarios + report gaps
- "does krkn cover OVN pod failures" → Search scenarios for OVN, read the YAML files, report what's covered vs missing
- "analyze OCPBUGS-12345" → Pull that specific bug, run FILTER/MAP/ANALYZE on just that bug
- "virtualization domain filter only 100 bugs" → `--agent virtualization --domain-filter-only --max-bugs 100 --days 365`
- "test virt ocp-virt keywords without chaos filter" → `--agent virtualization --domain-filter-only --max-bugs 100 --days 365`
- "what gaps exist for networking" → Query Neo4j for networking gap counts + search ChromaDB for networking scenarios
- "show me all hog scenarios" → Search ChromaDB/krkn docs for hog scenario plugins and list them
- "what components have the most open gaps" → Query Neo4j gap counts

## Interactive Setup (Full Scan only)

Before running the pipeline, ask the user these questions using AskUserQuestion.

**AskUserQuestion limits:**
- **At most 4 options per question** (hard API limit).
- **Batch 1:** Questions 1–4 in **one** `AskUserQuestion` call → **one screen, four tabs** (toggle between questions, submit once).

**Question 1 — OCP Version:**
- Question: "Which OpenShift version(s) to scan?"
- Options:
  - "4.21 (Recommended)" — Current latest stable
  - "4.20" — Previous stable
  - "4.19" — Older supported
  - "All (4.19, 4.20, 4.21, 4.22)" — Scan across all supported versions
- Note: User can also type a custom comma-separated list like "4.20,4.21"

**Question 2 — Agent Scope (Tab 2):**

Discover agents first, then embed the full list in the **question text** (not as button options):

```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.agents.registry import discover_agents,format_agent_prompt_option
for name, cfg in sorted(discover_agents().items()):
    print(f'{name}\t{format_agent_prompt_option(cfg)}')
"
```

Use fixed labels from `config/agents/scan_prompt.yaml` when present; otherwise use `format_agent_prompt_option()` output.

| agent_id | Fixed option label |
|----------|-------------------|
| control_plane | Control plane — etcd, API server, scheduler (control_plane) |
| networking | Networking — OVN, DNS, ingress (networking) |
| node_machine | Node & machine — kubelet, MCO, bare metal (node_machine) |
| operators_platform | Operators & platform — OLM, console, monitoring (operators_platform) |
| storage | Storage — CSI, volumes, registry (storage) |
| upgrade_lifecycle | Upgrade lifecycle — CVO, MCO, installer (upgrade_lifecycle) |
| virtualization | OpenShift Virtualization — VM, migration, KubeVirt (virtualization) |

- Question: "Which domain agent(s) should run?\n\nAvailable agents:\n- control_plane — Control plane (etcd, API server, scheduler)\n- networking — OVN, DNS, ingress\n- … (one bullet per discovered agent, with agent_id)\n\nUse agent_id values when specifying agents."
- Options (only 2 — stays within API limit):
  - "All agents (Recommended)"
  - "Input Agent(s)"
- **If "All agents (Recommended)"** → omit `--agent` (run all discovered agents).
- **If "Input Agent(s)"** → after the user submits the tabbed form, ask in **chat**: "Enter agent ID(s), comma-separated (e.g. `virtualization` or `control_plane,networking`):". Validate IDs against discovery output; then `--agent <comma-separated ids>`.

Do **not** list each agent as a separate AskUserQuestion option (exceeds 4-option limit with 7+ agents).

**Question 3 — Lookback Window:**
- Question: "How many days back should we scan for bugs?"
- Options:
  - "7 days" — Last week (Quick scan)
  - "14 days" — Last 2 weeks (Recommended)
  - "30 days" — Full month (More thorough)
  - "365 days" — Full year (Virtualization agent — needed for sample size)

**Question 4 — Scan Settings:**
- Question: "What kind of scan?"
- Options:
  - "Full scan (Recommended)" — All bugs, LLM enabled, complete analysis
  - "Quick scan" — 50 bugs max, LLM enabled (fast validation)
  - "Keyword only" — All bugs, no LLM (fast, free, less accurate)

Map selections to CLI flags (use the days value from Question 3):
- "Full scan" → `--max-bugs 2000 --use-llm`
- "Quick scan" → `--max-bugs 50 --use-llm`
- "Keyword only" → `--max-bugs 2000` (no --use-llm)

**Batch 2 — Filter stages (always ask after agent selection):**

Ask a **second** AskUserQuestion with multiSelect — **regardless of whether the user chose All agents, one agent, or multiple agents**:

**Question 5 — Filter stages:**
- Question: "Select which filter layers should be applied?"
- multiSelect: true
- allow_multiple: true
- Options:
  - "OpenShift Virtualization (Broad Primary Filtering)" — Domain filter: common ocp-virt keywords + virt skip list. Answers: *Is this an OCP Virt bug?* (applies to **every** selected agent — catches virt bugs filed under wrong components)
  - "Krkn Chaos (Specific Secondary Filtering)" — Chaos filter: common chaos keywords + krkn injection matching. Answers: *Is this chaos-testable?* (applies when this stage is enabled and domain-only is not selected)
- Default if user picks nothing: treat as both checked.

Map checkbox combinations to CLI flags:

| OpenShift Virtualization | Krkn Chaos | CLI flags | What runs |
|--------------------------|------------|-----------|-----------|
| ✓ | ✓ | *(none)* | Full keyword filter (`filter_bug`) — domain + chaos gates for virtualization; standard chaos filter for other agents |
| ✓ | | `--domain-filter-only` | Ocp-virt domain filter on **every** selected agent (`filter_domain_bug`) — no chaos/injection gate |
| | ✓ | *(none)* | Standard chaos keyword filter for every selected agent |
| | | — | Invalid — re-ask; at least one stage required |

**Important constraints:**
- `--domain-filter-only` cannot be combined with `--use-llm` (omit `--use-llm` when OpenShift Virtualization is the only stage checked).
- **Do not re-ask** if OpenShift Virtualization is selected without the virtualization agent — apply `--domain-filter-only` to whatever agents were chosen (many virt bugs are filed under networking, HyperShift, etc.).
- When OpenShift Virtualization stage is enabled, pass `--filter-review-json filter_review.json --no-filter-review` so Batch 3 can review results (see below).

**Virt domain-only filter tuning:**

When the user selects **OpenShift Virtualization only** (no Krkn Chaos), run the pipeline with `--domain-filter-only` (Neo4j required). Applies ocp-virt keywords to bugs from **any** selected agent(s):

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && PYTHONPATH=. python3 src/main.py \
  --release <VERSION> --agent <AGENT_LIST_OR_OMIT_FOR_ALL> \
  --max-bugs <MAX> --days <DAYS> --domain-filter-only \
  --filter-review-json filter_review.json --no-filter-review
```

**Important:** When running via this slash command (agent Bash), always pass **`--no-filter-review`** when the OpenShift Virtualization stage is enabled. The terminal `input()` menu in `main.py` does not work in non-interactive Bash — filter review happens in **Batch 3 (AskUserQuestion)** below, not inside `main.py`.

For full keyword filter (domain + chaos), omit `--domain-filter-only` but still use `--filter-review-json` + `--no-filter-review` when reviewing via Batch 3.

## Running the Pipeline

After getting answers, run the pipeline using `main.py`:

```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 src/main.py \
  --release <VERSION_OR_COMMA_LIST> \
  --agent <AGENT_OR_COMMA_LIST_OR_all> \
  --use-llm \
  --max-bugs <MAX_BUGS> \
  --days <DAYS> \
  --parallel
```

**Filter stage flags:**
- Both "OpenShift Virtualization" and "Krkn Chaos" checked → omit `--domain-filter-only`
- Only "OpenShift Virtualization" checked → add `--domain-filter-only` (do **not** add `--use-llm`); add `--filter-review-json filter_review.json --no-filter-review`
- Only "Krkn Chaos" checked → omit `--domain-filter-only`

Note: Always include `--parallel` when running multiple agents. Omit for single-agent runs.

**Examples:**
- Virt domain filter only: `--release 4.21 --agent virtualization --max-bugs 100 --days 365 --domain-filter-only`
- Virt full keyword filter: `--release 4.21 --agent virtualization --max-bugs 100 --days 365`
- Virt full LLM scan: `--release 4.21 --agent virtualization --max-bugs 100 --days 365 --use-llm`
- Single version, single agent: `--release 4.21 --agent control_plane`
- Multiple versions: `--release 4.20,4.21`
- Multiple agents: `--agent control_plane,networking,storage`
- All agents (omit --agent or pass "all"): `--release 4.21`
- Everything: `--release 4.19,4.20,4.21 --agent all`
- Quick scan: `--max-bugs 50 --days 7`
- Deep scan: `--max-bugs 2000 --days 60`

Map the user's interactive selections:
- "All bugs" / full scan → `--max-bugs 2000`
- "Quick scan" → `--max-bugs 50`
- "365 days" → `--days 365`
- "14 days" → `--days 14`
- "All agents" → omit `--agent` flag
- "Input Agent(s)" → use the comma-separated agent IDs from the chat follow-up → `--agent control_plane,networking`
- OpenShift Virtualization stage only → add `--domain-filter-only` (applies ocp-virt keywords to all selected agents)

## After Filter: Review PASS / SKIP (when OpenShift Virtualization stage is enabled)

**Do this only after** `main.py` finishes and `--filter-review-json` wrote the **current run's** file.

**Critical accuracy rules — never invent cross-filing claims:**
1. `filter_review.json` PASS/SKIP lists are **this run only** (new bugs that were filtered). Known Neo4j bugs are **not** re-filtered.
2. If this run shows `0 PASS / N SKIP`, say that plainly. Do **not** treat an older `filter_review.json` or Neo4j historical totals as "control_plane bugs matched virt keywords this run".
3. Metadata in the JSON includes `scope: this_run_new_bugs_only`, `filter_mode` (`domain` or `chaos`), and `neo4j_flag` (`virt_relevant` or `chaos_relevant`) — read it before summarizing.
4. Domain-only PASS means **virt_relevant** (ocp-virt keywords matched) — never call these chaos-relevant. Chaos-stage PASS means **chaos_relevant**.
5. Historical Neo4j matches must use the matching flag (`virt_relevant` after domain-only, `chaos_relevant` after chaos filter), with a component / last_filter_agent breakdown — never attributed to the agent just selected unless that agent's this-run PASS list is non-empty.

**Batch 3 — AskUserQuestion (required before printing any bug lists):**

- Question: "Review filter results?"
- Options (pick one):
  - "Show all PASS bugs (Recommended)"
  - "Show all SKIP bugs"
  - "Show both PASS and SKIP"
  - "Skip review"

**Do not** dump PASS/SKIP lists or write summary code until the user answers Batch 3.

After the user selects an option, load the JSON and print **only what they chose**:

**If PASS (option 1):**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && PYTHONPATH=. python3 -c "
from src.coordinator.filter_review import format_filter_pass_list, load_filter_review_json
import json
from pathlib import Path
meta = json.loads(Path('filter_review.json').read_text()).get('metadata', {})
print('This-run metadata:', meta)
passed, _ = load_filter_review_json('filter_review.json')
print(format_filter_pass_list(passed))
"
```

Then summarize as: "**This run:** N bugs PASSED the ocp-virt **domain** filter (of M new bugs filtered). These are virt-relevant (keyword matches), not chaos-relevant." If N is 0, stop there for "cross-filing" claims.

Optionally (only as Neo4j history, clearly labeled), show **virt_relevant** inventory after a domain-only run:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && PYTHONPATH=. python3 -c "
from src.knowledge.neo4j_store import Neo4jStore
from src.coordinator.filter_review import format_neo4j_virt_relevant_inventory
n = Neo4jStore(); n.connect()
print(format_neo4j_virt_relevant_inventory(
    n.count_virt_relevant_bugs(),
    n.get_virt_relevant_bugs_by_filter_agent(),
    n.get_virt_relevant_bugs_by_component(),
))
n.close()
"
```

Phrase historical context like: "**Neo4j inventory:** X bugs stored with `virt_relevant=true` (domain/ocp-virt keywords). By component: … By last filter agent: …" — never call these chaos-relevant, and never say "X bugs from the control_plane domain matched…".

After a **chaos** filter run (Krkn Chaos stage enabled), use `format_neo4j_chaos_relevant_inventory` / `count_chaos_relevant_bugs` instead.

**If SKIP (option 2):**
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && PYTHONPATH=. python3 -c "
from src.coordinator.filter_review import format_filter_skip_list, load_filter_review_json
_, skipped = load_filter_review_json('filter_review.json')
print(format_filter_skip_list(skipped))
"
```

**If both (option 3):** run the PASS command, then the SKIP command.

**If skip review (option 4):** do not run either command.

Present each bug as: `[CONFIDENCE%] OCPBUGS-XXXXX: summary`. Tune keywords in `config/filters/ocp-virt.yaml`.

**Running `main.py` directly in your own terminal (not agent Bash)?** Omit `--no-filter-review` to get the numbered menu via `input()` instead of Batch 3.

## After the Scan: Post Gaps to GitHub

After a full scan, present results as:
1. **Per-agent table** — Agent | Discovered | Passed Filter | Skipped | Gaps | LLM Calls | ANALYZE | Cost (+ TOTAL). Read LLM calls / ANALYZE seconds / Cost from each agent block in `format_summary` (e.g. `LLM calls: 12`, `ANALYZE: 45.2s`). Do **not** use a Metric/Count rollup.
2. **Gaps by confidence** table (keep when gaps > 0).

Then ask which gaps to post as GitHub issues using AskUserQuestion:

- Question: "Which gaps should I create as GitHub issues?"
- multiSelect: true
- Options: one per gap, showing "[CONFIDENCE SCORE] BUG_KEY: summary"
- Plus "None — skip" option

For each approved gap, use the `create_issues_for_gaps` function:
```bash
PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from dotenv import load_dotenv; load_dotenv()
import os
from src.apis.github_client import GitHubClient
from src.agents.act import build_issue_title, build_issue_body, LABEL

github = GitHubClient(token=os.environ.get('GITHUB_TOKEN', ''))
owner = os.environ.get('GITHUB_FORK_OWNER', 'krkn-chaos')

# For each approved gap, create the issue
# Replace BUG_KEY, SUMMARY, COMPONENT, SCORE, REASONING with actual values
title = '[chaos-coordinator] [MEDIUM] OCPBUGS-XXXXX: summary here'
body = '''## Chaos Test Coverage Gap
...full body from build_issue_body...'''

result = github.create_issue(owner=owner, repo='krkn', title=title, body=body, labels=[LABEL])
print(f'Created: {result.get(\"html_url\", \"?\")}'  if result else 'Failed')
"
```

Alternatively, if using main.py directly, the CLI will prompt interactively after the scan.

## Architecture Reference

### Pipeline: DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER

Each agent runs the full pipeline for its component area.

### DISCOVER (JIRA + Sippy + z-stream changelogs)
- 4-tier version query:
  - Tier 1: bugs tagged with target release (>= 4.21, < 4.22)
  - Tier 2: open bugs from older versions (unfixed, likely still present)
  - Tier 3: open bugs from newer versions (if it exists on 5.0, it exists on 4.21 too)
  - Tier 4: bugs with no affectedVersion set
- Z-stream enrichment from OpenShift release controller (fix commits, images)
- Neo4j dedup: already-analyzed bugs get status update only (zero LLM cost)

### FILTER (keyword → optional domain → semantic cache → LLM)

**Virtualization agent — two-stage keyword filter:**

1. **OpenShift Virtualization** (`--domain-filter-only`)
   - Source: `config/filters/ocp-virt.yaml`
   - Applies: virt domain keywords + virt skip keywords
   - Skips: common chaos keywords, krkn injection matching
   - Log prefix: `DOMAIN PASS` / `DOMAIN SKIP`

2. **Krkn Chaos** (default `filter_bug` for `--agent virtualization`)
   - Source: `common.yaml` + agent YAML + `ocp-virt.yaml` merged into chaos keywords
   - Applies: skip keywords, chaos keywords, krkn injection-method matching
   - Log prefix: `PASS` / `SKIP`

When both stages are enabled (default), stage 2 runs as a single `filter_bug()` call — effectively domain keywords plus chaos gates together.

**All agents — standard 3-tier filter when `--use-llm`:**
- Layer 1: Keyword pre-filter (config/filters/common.yaml + agent overrides). Zero tokens.
- Layer 2: Semantic cache in ChromaDB (cosine distance < 0.15). Zero tokens.
- Layer 3: LLM classification via claude_code provider (--bare --system-prompt for minimal token usage ~2,700/call)
- Confidence < 80 auto-escalates from Sonnet to Opus

`--domain-filter-only` is incompatible with `--use-llm`.

### MAP (ChromaDB RAG + LLM reasoning)
- Per-component ChromaDB search (scenarios + krkn docs + OCP docs)
- krkn-knowledgebase lookup for validated scenario patterns
- LLM determines: FULL_MATCH / PARTIAL_MATCH / NO_MATCH
- Fallback: distance-based thresholds (< 0.35 = FULL, < 0.65 = PARTIAL)

### ANALYZE (Opus-level reasoning)
- Context: OCP docs + krkn plugins + Neo4j resolved bug history + z-stream fixes
- Scoring: repro steps (+20), existing scenario (+25), docs understanding (+20), plugin match (+15), domain (+10), prior art (+10)
- Generates SPECIFIC modifications (not vague "extend this scenario")

### Confidence → Action:
- 70-100 HIGH → Draft PRs across krkn + krkn-hub + website
- 40-69 MEDIUM → GitHub issue with recommendation
- 0-39 LOW → GitHub issue describing gap

### LLM Provider: claude_code
- Uses `claude -p --bare --system-prompt --exclude-dynamic-system-prompt-sections`
- ~2,700 tokens per FILTER call (vs 63,000 without --bare)
- Per-call token usage logged: `LLM CALL #N: X in + Y out = Z tokens, $cost`
- Total usage logged at end: `TOKEN USAGE: X input + Y output = Z total, cost=$X, calls=N`

### Pluggable Agents (auto-discovered from config/agents/*.yaml):
Each YAML defines name, components, filter keywords, docs. Scan wizard: discover agents, show the list in Question 2 text, offer only "All agents" / "Input Agent(s)" (stays within AskUserQuestion's 4-option limit). Fixed labels: `config/agents/scan_prompt.yaml` when present; else `format_agent_prompt_option()`.

### Knowledge Layer:
- **ChromaDB**: Vector search over krkn scenarios, krkn docs, OCP docs, agent-specific docs, filter cache
- **Neo4j**: Operational memory — 3,000+ bugs, 484+ gaps, component relationships, run metrics

## Targeted Query Pipeline Steps

### Step 1: DISCOVER

Pull recent bugs from JIRA (skip or narrow if in Targeted Query mode):

```
mcp__jira__searchJiraIssuesUsingJql with:
  cloudId: https://redhat.atlassian.net
  jql: project = OCPBUGS AND issuetype = Bug AND created >= -14d ORDER BY created DESC
  maxResults: 50
  fields: ["summary", "description", "status", "priority", "components", "created"]
  responseContentFormat: markdown
```

### Step 2: FILTER (Claude reasoning + ChromaDB)

For EACH bug, do these steps — don't batch, actually reason per bug:

**2a. Read the bug** — understand the summary and description.

**2b. Search OCP docs** for component context:
```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.knowledge.chromadb_store import ChromaStore
c = ChromaStore(persist_dir='./chroma_data')
for r in c.search_all('PUT_COMPONENT_AND_SUMMARY_HERE', n_results=3):
    print(r['text'][:300])
    print('---')
"
```

**2c. Decide** using this rule:
> If the bug involves a component behaving incorrectly during, after, or because of any disruption — it's chaos-relevant. Even if the symptom is in a different component.

**Chaos-relevant:** performance degradation, crash/restart, operator degraded, node failure, network disruption, resource exhaustion, service down, upgrade/rollback failure, recovery failure, scaling issues, intermittent failures, data corruption, certificate issues.

**NOT chaos-relevant:** CVEs, test infra, docs, backports, dependency bumps, stubs/clones.

Output:
```
PASS: OCPBUGS-XXXXX — [failure mode] (injection: [method])
SKIP: OCPBUGS-XXXXX — [reason]
```

### Step 3: MAP (Claude reads actual scenarios)

For each PASS bug, find existing krkn scenarios:

```bash
PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.knowledge.chromadb_store import ChromaStore
c = ChromaStore(persist_dir='./chroma_data')
print('=== Matching scenarios ===')
for r in c.search_scenarios('PUT_COMPONENT_AND_SUMMARY_HERE', n_results=5):
    print(f'[dist={r[\"distance\"]:.3f}] {r[\"text\"][:200]}')
    print()
"
```

Then **READ the actual matched scenario YAML** if one exists:
```bash
cat /Users/sahil/krkn/scenarios/openshift/SCENARIO_FILE.yaml
```
(Adjust path if your krkn clone lives elsewhere.)

Now reason:
- What does this scenario actually inject? (pod kill? node drain? network latency?)
- Does it cover the EXACT failure mode in the bug?
- Or does it test the same component but a different failure?

Decision:
- **FULL MATCH**: Scenario tests this exact failure → no action needed
- **PARTIAL MATCH**: Same component, different failure → extend it
- **NO MATCH**: Nothing covers this → new scenario needed

### Step 4: ANALYZE (Claude reasons about each gap)

For each gap (PARTIAL or NO MATCH), reason deeply:

**4a. What krkn plugins are available?**
```bash
PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.knowledge.chromadb_store import ChromaStore
c = ChromaStore(persist_dir='./chroma_data')
for r in c.search_krkn_docs('PUT_FAILURE_MODE_HERE', n_results=3):
    print(r['text'][:300])
    print('---')
"
```

**4b. Check Neo4j for similar resolved bugs:**
```bash
PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.knowledge.neo4j_store import Neo4jStore
n = Neo4jStore(); n.connect()
for s in n.get_similar_resolved_bugs('PUT_COMPONENT_NAME'):
    print(f'{s[\"bug_key\"]}: {s[\"summary\"][:60]} → {s[\"issue_url\"]}')
for g in n.get_component_gap_counts()[:5]:
    print(f'{g[\"component\"]}: {g[\"gaps\"]} gaps ({g[\"open_gaps\"]} open)')
n.close()
"
```

**4c. Score confidence** by actually reasoning:

| Question | If YES | If NO |
|----------|--------|-------|
| Can I explain the exact reproduction steps? | +20 | +0 |
| Is there an existing scenario to extend? | +25 | +0 |
| Do I understand HOW this fails from the OCP docs? | +20 | +0 |
| Is there a krkn plugin that injects this exact failure? | +15 | +0 |
| Does this match the agent's domain? | +10 | +0 |
| Have we solved a similar bug before? (Neo4j) | +10 | +0 |

**4d. For HIGH confidence gaps, generate SPECIFIC modifications:**

Don't say "extend pod_etcd.yml". Instead say:
```
Extend scenarios/openshift/etcd.yml:
- Add a new test case that deploys CPU hog pods on master nodes
  (use hog_scenarios plugin with cpu target 80%, duration 300s)
- While hog is running, check etcd operator status:
  oc get co/etcd -o jsonpath='{.status.conditions}'
- Assert: etcd should NOT report Degraded=True while members
  are actually healthy (etcdctl endpoint health shows true)
```

### Step 5: ACT

Present each gap to the user:

```
Gap #1: [HIGH 85/100]
Bug: OCPBUGS-XXXXX — summary
Component: Etcd

What I found:
- OCP docs say: [relevant architecture context]
- Closest krkn scenario: [what it tests]
- This bug is different because: [what's NOT covered]

Recommendation:
- [specific changes needed]
- krkn plugin: [exact plugin name]
- Repos to update: krkn, krkn-hub, website

→ [Approve] [Reject]
```

When approved, create GitHub issue on `krkn-chaos/krkn` with the full analysis.

### Step 6: REMEMBER

Store results in Neo4j:
```bash
PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.knowledge.neo4j_store import Neo4jStore
from src.models import *
n = Neo4jStore(); n.connect()
# Results get stored via the pipeline
n.close()
"
```

## Key Principles

1. **READ before deciding** — don't pattern match, actually understand the bug
2. **SEARCH before recommending** — check what krkn already has, what OCP docs say
3. **BE SPECIFIC** — don't say "extend this scenario", say exactly what to change
4. **BE HONEST** — if you don't understand the component, say LOW confidence
5. **CHECK HISTORY** — Neo4j tells you what was solved before
