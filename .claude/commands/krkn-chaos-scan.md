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
- "test virt ocp-virt keywords without chaos filter" → `ocp_virt_filter_eval --domain-only` or `--domain-filter-only`
- "what gaps exist for networking" → Query Neo4j for networking gap counts + search ChromaDB for networking scenarios
- "show me all hog scenarios" → Search ChromaDB/krkn docs for hog scenario plugins and list them
- "what components have the most open gaps" → Query Neo4j gap counts

## Interactive Setup (Full Scan only)

Before running the pipeline, ask the user these questions using AskUserQuestion.

**Batch 1 — ask Questions 1–4 in a single AskUserQuestion call:**

**Question 1 — OCP Version:**
- Question: "Which OpenShift version(s) to scan?"
- Options:
  - "4.21 (Recommended)" — Current latest stable
  - "4.20" — Previous stable
  - "4.19" — Older supported
  - "All (4.19, 4.20, 4.21, 4.22)" — Scan across all supported versions
- Note: User can also type a custom comma-separated list like "4.20,4.21"

**Question 2 — Agent Scope:**

**Step 1 — Discover agents** (run from repo root):

```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.agents.registry import discover_agents,format_agent_prompt_option
for name, cfg in sorted(discover_agents().items()):
    print(f'{name}\t{format_agent_prompt_option(cfg)}')
"
```

**Step 2 — Canonical fixed options** (edit labels/order here; `agent_id` must match YAML `name`):

| agent_id | Fixed option label |
|----------|-------------------|
| control_plane | Control plane — etcd, API server, scheduler (control_plane) |
| networking | Networking — OVN, DNS, ingress (networking) |
| node_machine | Node & machine — kubelet, MCO, bare metal (node_machine) |
| operators_platform | Operators & platform — OLM, console, monitoring (operators_platform) |
| storage | Storage — CSI, volumes, registry (storage) |
| upgrade_lifecycle | Upgrade lifecycle — CVO, MCO, installer (upgrade_lifecycle) |
| virtualization | OpenShift Virtualization — VM, migration, KubeVirt (virtualization) |

**Step 3 — Merge into AskUserQuestion options:**

1. Start with: `"All agents (Recommended)"`
2. Build a map from Step 1 output: `agent_id` → tab-separated dynamic label (second column).
3. For each row in the fixed table **whose `agent_id` appears in discovery output**, use the **fixed label** (ignore the dynamic label for that agent).
4. For each discovered agent **not** in the fixed table, append the dynamic label from Step 1 (respects optional `prompt_label` in agent YAML via `format_agent_prompt_option`).
5. Sort merged agent options alphabetically by `agent_id` (parse from trailing `(agent_id)` in each label).
6. If a fixed-table agent is missing from discovery (YAML removed), **omit** that row — do not offer stale agents.
7. Final options: `"All agents (Recommended)"` first, then sorted merged agent options.

- Question: "Which domain agent(s) should run?"
- multiSelect: true
- Options: result of Step 3

**Step 4 — Map selection → CLI:**

- `"All agents (Recommended)"` → omit `--agent` (run all discovered agents)
- Any other option → extract `agent_id` from the trailing `(agent_id)`; join with commas for `--agent virtualization,storage`

**Question 3 — Lookback Window:**
- Question: "How many days back should we scan for bugs?"
- Options:
  - "14 days (Recommended)" — Last 2 weeks of bugs
  - "7 days" — Last week only (quick scan)
  - "30 days" — Full month (more thorough)
  - "60 days" — Deep scan (catches older unfixed bugs)
  - "365 days" - Full year (needed for virtualization agent sample size)

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

**Batch 2 — Filter stages (only when virtualization is the sole selected agent):**

If the user selected **only** `virtualization` (not "All agents", not multiple agents), ask a **second** AskUserQuestion with multiSelect:

**Question 5 — Filter stages:**
- Question: "Select which filter layers should be applied? (virtualization agent)"
- multiSelect: true
- allow_multiple: true
- Options:
  - "OpenShift Virtualization (Broad Primary Filtering)" — Domain filter: common ocp-virt keywords + virt skip list. Answers: *Is this an OCP Virt bug?*
  - "Krkn Chaos (Specific Secondary Filtering)" — Chaos filter: common chaos keywords + krkn injection matching. Answers: *Is this chaos-testable?*
- Default if user picks nothing: treat as both checked.

Map checkbox combinations to CLI flags:

| OpenShift Virtualization | Krkn Chaos | CLI flags | What runs |
|--------------------------|------------|-----------|-----------|
| ✓ | ✓ | *(none)* | Full keyword filter (`filter_bug`) — domain + chaos gates |
| ✓ | | `--domain-filter-only` | Domain filter only (`filter_domain_bug`) — tune ocp-virt keywords |
| | ✓ | *(none)* | Standard chaos keyword filter (same as both for `--agent virtualization`) |
| | | — | Invalid — re-ask; at least one stage required |

**Important constraints:**
- `--domain-filter-only` requires `--agent virtualization` alone (no `--use-llm`).
- If user selected "All agents" or multiple agents including virtualization, **skip Question 5** and use the default full chaos filter for every agent.
- For non-virtualization agents, only the Krkn Chaos filter applies (Question 5 is not shown).

**Virt filter eval (no Neo4j, filter tuning only):**

When the user selects **OpenShift Virtualization only** (no Krkn Chaos), prefer the eval script instead of the full pipeline if Neo4j is unavailable:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && PYTHONPATH=. python3 -m src.evals.ocp_virt_filter_eval \
  --release <VERSION> --max-bugs <MAX> --days <DAYS> --domain-only
```

When both stages are checked but user wants keyword-only validation (no MAP/ANALYZE/Neo4j), use the eval script without `--domain-only`:

```bash
PYTHONPATH=. python3 -m src.evals.ocp_virt_filter_eval \
  --release <VERSION> --max-bugs <MAX> --days <DAYS>
```

## Running the Pipeline

After getting answers, run the pipeline using `main.py`:

```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 src/main.py \
  --release <VERSION_OR_COMMA_LIST> \
  --agent <AGENT_OR_COMMA_LIST_OR_all> \
  --use-llm \
  --max-bugs <MAX_BUGS> \
  --days <DAYS> \
  [--use-llm] \
  [--domain-filter-only] \
  [--parallel]
```

**Filter stage flags (virtualization agent only):**
- Both "OpenShift Virtualization" and "Krkn Chaos" checked → omit `--domain-filter-only`
- Only "OpenShift Virtualization" checked → add `--domain-filter-only` (do **not** add `--use-llm`)
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
- Specific agent(s) → extract `agent_id` from `(agent_id)` suffix in Question 2 labels → `--agent control_plane,networking`
- "virtualization" only + OCP Virt checkbox only → `--agent virtualization --domain-filter-only`

## After Filter: Review virt PASS / SKIP (virtualization scans)

After a **virtualization** scan or `ocp_virt_filter_eval` completes, **always** offer filter review using AskUserQuestion before gap/issue steps:

**Batch 3 — Filter review (virtualization agent only):**

- Question: "Review filter results?"
- Options:
  - "Show all PASS bugs (Recommended)" — full list with filter confidence, failure mode, injection method, JIRA URL
  - "Show all SKIP bugs" — full list with skip reason and confidence
  - "Show both PASS and SKIP"
  - "Skip review"

**How to get the data:**

1. **Preferred — eval script** (no Neo4j, keyword filter only):
```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" && PYTHONPATH=. python3 -m src.evals.ocp_virt_filter_eval \
  --release <VERSION> --max-bugs <MAX> --days <DAYS> \
  [--domain-only] \
  --filter-review-json filter_review.json
```
The script prints a summary, saves JSON, and prompts `1/2/3/4` in the terminal.

2. **Full pipeline** (after FILTER phase):
```bash
PYTHONPATH=. python3 src/main.py --release <VERSION> --agent virtualization \
  --max-bugs <MAX> --days <DAYS> \
  [--domain-filter-only] \
  --filter-review-json filter_review.json
```

3. **Read saved JSON** if the user chose review in Claude but the terminal already ran:
```bash
PYTHONPATH=. python3 -c "
import json
from pathlib import Path
from src.coordinator.filter_review import format_filter_pass_list, format_filter_skip_list
from src.models import Bug, FilterResult

data = json.loads(Path('filter_review.json').read_text())
def row_to_fr(r):
    return FilterResult(
        bug=Bug(key=r['key'], summary=r['summary'], description='', component=r['component'],
                priority='', status='', created='', url=r['url']),
        chaos_relevant=r['outcome']=='pass',
        failure_mode=r.get('failure_mode'),
        injection_method=r.get('injection_method'),
        skip_reason=r.get('skip_reason'),
        confidence=r['confidence'],
    )
passed = [row_to_fr(r) for r in data['passed']]
skipped = [row_to_fr(r) for r in data['skipped']]
print(format_filter_pass_list(passed))
print(format_filter_skip_list(skipped))
"
```

**When presenting lists to the user in Claude**, format each bug as:
`[CONFIDENCE%] OCPBUGS-XXXXX: summary (skip reason or injection method)`

Use this review to tune `config/filters/ocp-virt.yaml` — false PASS → add skip keywords; false SKIP → add domain/chaos keywords.

## After the Scan: Post Gaps to GitHub

If the pipeline found gaps, present each one to the user and ask which to post as GitHub issues using AskUserQuestion:

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

1. **OpenShift Virtualization** (`--domain-filter-only` or `ocp_virt_filter_eval --domain-only`)
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
Agents are discovered dynamically. Each YAML defines: name, description, components, optional `prompt_label`, filter keywords, doc sources. `/krkn-chaos-scan` merges a fixed label table with discovery output; new agents get dynamic labels via `format_agent_prompt_option()` unless listed in the fixed table.

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
