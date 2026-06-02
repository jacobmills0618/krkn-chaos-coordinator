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
- "what gaps exist for networking" → Query Neo4j for networking gap counts + search ChromaDB for networking scenarios
- "show me all hog scenarios" → Search ChromaDB/krkn docs for hog scenario plugins and list them
- "what components have the most open gaps" → Query Neo4j gap counts

## Interactive Setup (Full Scan only)

Before running the pipeline, ask the user these questions using AskUserQuestion. Ask all 4 in a single AskUserQuestion call:

**Question 1 — OCP Version:**
- Question: "Which OpenShift version(s) to scan?"
- Options:
  - "4.21 (Recommended)" — Current latest stable
  - "4.20" — Previous stable
  - "4.19" — Older supported
  - "All (4.19, 4.20, 4.21, 4.22)" — Scan across all supported versions
- Note: User can also type a custom comma-separated list like "4.20,4.21"

**Question 2 — Agent Scope:**
- First, discover available agents dynamically:
```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 -c "
from src.agents.registry import discover_agents
for name, cfg in sorted(discover_agents().items()):
    print(f'{name}: {cfg.description}')
"
```
- Question: "Which domain agent(s) should run?"
- multiSelect: true
- Options: "All agents (Recommended)" plus one option per discovered agent (use name and description from the output above)
- Note: Agents are auto-discovered from config/agents/*.yaml — new agents appear here automatically

**Question 3 — Lookback Window:**
- Question: "How many days back should we scan for bugs?"
- Options:
  - "14 days (Recommended)" — Last 2 weeks of bugs
  - "7 days" — Last week only (quick scan)
  - "30 days" — Full month (more thorough)
  - "60 days" — Deep scan (catches older unfixed bugs)

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

## Running the Pipeline

After getting answers, run the pipeline using `main.py`:

```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. /opt/homebrew/opt/python@3.11/bin/python3.11 src/main.py \
  --release <VERSION_OR_COMMA_LIST> \
  --agent <AGENT_OR_COMMA_LIST_OR_all> \
  --use-llm \
  --max-bugs <MAX_BUGS> \
  --days <DAYS>
```

**Examples:**
- Single version, single agent: `--release 4.21 --agent control_plane`
- Multiple versions: `--release 4.20,4.21`
- Multiple agents: `--agent control_plane,networking,storage`
- All agents (omit --agent or pass "all"): `--release 4.21`
- Everything: `--release 4.19,4.20,4.21 --agent all`
- Quick scan: `--max-bugs 50 --days 7`
- Deep scan: `--max-bugs 2000 --days 60`

Map the user's interactive selections:
- "All bugs" → `--max-bugs 2000`
- "14 days" → `--days 14`
- "All agents" → omit `--agent` flag

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

### FILTER (3-tier: keyword → semantic cache → LLM)
- Layer 1: Keyword pre-filter (config/filters/common.yaml + agent overrides). Zero tokens.
- Layer 2: Semantic cache in ChromaDB (cosine distance < 0.15). Zero tokens.
- Layer 3: LLM classification via claude_code provider (--bare --system-prompt for minimal token usage ~2,700/call)
- Confidence < 80 auto-escalates from Sonnet to Opus

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
Agents are discovered dynamically. Each YAML defines: name, components, filter keywords, doc sources.

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

When approved, create GitHub issue on `shahsahil264/krkn` with the full analysis.

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
