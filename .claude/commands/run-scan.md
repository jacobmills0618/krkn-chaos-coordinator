---
description: Run krkn-chaos-coordinator scan with Claude Code as the reasoning engine
allowed-tools: Bash, Read, Write, mcp__jira__searchJiraIssuesUsingJql, mcp__github__create_issue
---

# krkn-chaos-coordinator — Full Scan with LLM Reasoning

You are the AI reasoning engine for krkn-chaos-coordinator. You have access to:
- **JIRA MCP** for live bug data
- **ChromaDB** (4,089 chunks) for krkn scenario docs and OpenShift architecture docs
- **Neo4j** for operational memory (already-analyzed bugs, gaps, trends)
- **Your own reasoning** for chaos relevance decisions

Run the full pipeline: DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER

## Step 1: DISCOVER

Query JIRA for recent bugs. Pull 100 bugs across all major components:

```
mcp__jira__searchJiraIssuesUsingJql with:
  cloudId: https://redhat.atlassian.net
  jql: project = OCPBUGS AND issuetype = Bug AND created >= -14d ORDER BY created DESC
  maxResults: 100
  fields: ["summary", "description", "status", "priority", "components", "created"]
  responseContentFormat: markdown
```

Save results to `tests/fixtures/latest_scan.json`.

## Step 2: FILTER (Claude Code reasoning + ChromaDB context)

For each bug, do NOT just check keywords. Actually reason about it:

1. **Read the bug description** carefully
2. **Search ChromaDB** for relevant OCP architecture docs:
   ```bash
   cd /Users/sahil/krkn-chaos-coordinator
   PYTHONPATH=. ./venv/bin/python3 -c "
   from src.knowledge.chromadb_store import ChromaStore
   chroma = ChromaStore(persist_dir='./chroma_data')
   results = chroma.search_all('QUERY_HERE', n_results=3)
   for r in results:
       print(r['text'][:200])
       print()
   "
   ```
3. **Use the OCP docs context** to understand:
   - How does this component normally behave?
   - Is this failure expected under disruption?
   - Can krkn inject the condition that triggers this bug?

4. **Apply the chaos relevance rule:**
   > If the bug involves a component behaving incorrectly during, after, or because of any disruption — it's chaos-relevant. Even if the symptom appears in a different component.

   Chaos-relevant categories:
   - Performance degradation (slow, p99, latency, throughput)
   - Component crash/restart (panic, OOM, crashloop)
   - Cluster health (operator degraded/unavailable)
   - Node failures (drain, reboot, not ready)
   - Network disruption (partition, DNS, connection reset, 502/503)
   - Resource exhaustion (memory leak, disk full, CPU spike)
   - Service disruption (service down, endpoint unreachable)
   - Upgrade/rollback failures
   - Recovery failures (doesn't reconcile after restart)
   - Scaling issues (autoscaler, pending pods)
   - Intermittent failures (under load/pressure)
   - Data integrity (stale, corrupt, lost)

   NOT chaos-relevant: CVEs, test infra, docs, backports, dependency bumps, stub/clone tickets.

5. **Output decisions:**
   ```
   PASS: OCPBUGS-XXXXX — [failure mode] (injection: [method])
     Context: [what you learned from OCP docs about this component]
   SKIP: OCPBUGS-XXXXX — [reason]
   ```

## Step 3: MAP (ChromaDB + Claude reasoning)

For each chaos-relevant bug, search for existing krkn scenarios:

```bash
PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.chromadb_store import ChromaStore
chroma = ChromaStore(persist_dir='./chroma_data')

# Search scenario configs
print('=== Scenario matches ===')
for r in chroma.search_scenarios('COMPONENT SUMMARY HERE', n_results=5):
    print(f'[{r[\"distance\"]:.3f}] {r[\"text\"][:150]}')
    print()

# Search krkn docs for how to test this
print('=== krkn docs ===')
for r in chroma.search_krkn_docs('COMPONENT SUMMARY HERE', n_results=3):
    print(f'[{r[\"distance\"]:.3f}] {r[\"text\"][:150]}')
    print()

# Search OCP docs for component architecture
print('=== OCP docs ===')
ocp = chroma._ocp_docs.query(query_texts=['COMPONENT SUMMARY HERE'], n_results=3)
if ocp and ocp['documents']:
    for i, doc in enumerate(ocp['documents'][0]):
        dist = ocp['distances'][0][i]
        print(f'[{dist:.3f}] {doc[:150]}')
        print()
"
```

Then reason:
- **FULL MATCH** (distance < 0.35): Existing scenario covers this — no action needed
- **PARTIAL MATCH** (distance < 0.65): Similar scenario exists — can extend
- **NO MATCH**: New gap — need new scenario

For each gap, explain:
- What the existing scenario tests
- What the bug describes that ISN'T covered
- How to extend the scenario (or why a new one is needed)

## Step 4: ANALYZE (Claude reasoning over all context)

For each gap, score confidence by reasoning (not just keyword points):

Consider:
- How clear is the reproduction path?
- How well do you understand the component behavior (from OCP docs)?
- Is there a krkn plugin that can inject this exact failure?
- Have similar bugs been resolved before? (check Neo4j)

```bash
# Check Neo4j for similar resolved bugs
PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.neo4j_store import Neo4jStore
neo4j = Neo4jStore()
neo4j.connect()
similar = neo4j.get_similar_resolved_bugs('COMPONENT_NAME')
for s in similar:
    print(f'{s[\"bug_key\"]}: {s[\"summary\"][:60]} → {s[\"issue_url\"]}')
gaps = neo4j.get_component_gap_counts()
for g in gaps[:10]:
    print(f'{g[\"component\"]}: {g[\"gaps\"]} gaps ({g[\"open_gaps\"]} open)')
neo4j.close()
"
```

Score:
- **HIGH (70-100)**: Clear repro, existing scenario to extend, you understand the component well, krkn plugin available → Draft PR
- **MEDIUM (40-69)**: Moderate clarity, plugin might work → GitHub issue with recommendation
- **LOW (0-39)**: Unclear, may need new plugin → GitHub issue describing gap

## Step 5: ACT

Present the approval queue to the user with your reasoning for each gap:

For each gap show:
1. Bug key and summary
2. Your confidence score and WHY
3. What you learned from OCP docs about this component
4. What krkn scenarios exist nearby (from ChromaDB)
5. What specific changes are needed
6. Which repos need PRs (krkn, krkn-hub, website, openshift/release)

Ask: **Approve** (create GitHub issue) or **Reject**?

When approved, create the issue on `shahsahil264/krkn` using GitHub MCP with the full detailed body.

## Step 6: REMEMBER

After the scan, store results in Neo4j:
```bash
PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.neo4j_store import Neo4jStore
neo4j = Neo4jStore()
neo4j.connect()
# Store will happen via the pipeline
neo4j.close()
"
```

## Key Difference from Keyword Mode

In keyword mode: `"crash" found → chaos-relevant`
In Claude Code mode: You READ the bug, SEARCH the docs, UNDERSTAND the component, then DECIDE.

Example:
- **Keyword mode**: "Clusters born in 4.9 may have duplicate members upgrading to 4.21" → PASS (word "member" matches) ← WRONG
- **Claude Code mode**: You read it, understand it's a version migration issue that can't be injected with krkn → SKIP ← CORRECT
