# krkn-chaos-coordinator

AI-driven multi-agent system that autonomously expands [krkn](https://github.com/krkn-chaos/krkn) chaos test coverage for OpenShift clusters by monitoring JIRA bugs, identifying coverage gaps, and creating PRs/issues.

**Current stats (June 2026):** 3,000+ bugs analyzed, 465+ gaps identified, 200 tests passing, $0.18/run with claude_code provider.

## How It Works

```
DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER

1. DISCOVER   Query JIRA (three-tier version matching) + z-stream changelogs
2. FILTER     3-tier: keyword pre-filter → semantic cache → LLM classification
3. MAP        ChromaDB RAG + LLM reasoning over existing krkn scenarios
4. ANALYZE    Score confidence (0-100), generate specific krkn modifications
5. ACT        Create GitHub issues (MEDIUM) or draft PRs (HIGH confidence)
6. REMEMBER   Store in Neo4j graph — never re-analyze the same bug
```

## Architecture

```
Orchestrator (dedup, approval queue)
├── Control Plane          (Etcd, kube-apiserver, HyperShift)
├── Networking             (OVN-K, DNS, router, SR-IOV, MetalLB)
├── Node & Machine         (Kubelet, CRI-O, Machine API, Bare Metal)
├── Storage                (CSI, Image Registry, LVMS)
├── Operators & Platform   (OLM, Console, Auth, Monitoring, Cloud Compute)
└── Upgrade & Lifecycle    (CVO, MCO, Installer variants)
```

Pluggable agents — auto-discovered from `config/agents/*.yaml`. 6 built-in agents covering 96 OCPBUGS components. Drop a YAML file to add a new domain.

## Knowledge Layer

| Store | Purpose | Data |
|-------|---------|------|
| **ChromaDB** | Vector search (RAG context for LLM) | 4,089 chunks: krkn scenarios, krkn docs, OCP docs, filter cache |
| **Neo4j** | Operational memory (dedup, history) | 3,000+ bugs, 465+ gaps, component relationships, run metrics |

## LLM Providers

5 pluggable backends, configurable per-phase:

| Provider | Description |
|----------|-------------|
| `claude_code` | Claude Code CLI — no API key needed (default when `claude` is on PATH) |
| `anthropic` | Direct API with prompt caching + batch API |
| `ollama` | Local models, free, private |
| `openai` | GPT-4o compatible |
| `google` | Gemini compatible |

Per-phase model routing: `LLM_FILTER_MODEL=claude-sonnet-4-6`, `LLM_ANALYZE_MODEL=claude-opus-4-6`

## Token Optimization

6-layer stack reduces cost by 91%:

1. **Keyword pre-filter** — 167 chaos keywords, catches ~55% of bugs (zero tokens)
2. **Semantic cache** — ChromaDB cosine similarity on past decisions (zero tokens)
3. **Model routing** — Sonnet for FILTER/MAP, Opus for ANALYZE
4. **Confidence escalation** — Sonnet → Opus only when uncertain (<80)
5. **Prompt caching** — `cache_control` on system prompts (90% off)
6. **Batch API** — 50% off, stacks with caching

With `claude_code` provider: `--bare --system-prompt` strips 62K system prompt overhead → ~2,700 tokens per call.

## Quick Start

### Prerequisites

- Python 3.11+
- Podman (for Neo4j)
- krkn repo cloned locally (required for scenario indexing)
- JIRA API token + GitHub PAT

### Setup

```bash
# Clone
git clone https://github.com/shahsahil264/krkn-chaos-coordinator.git
cd krkn-chaos-coordinator

# Clone krkn repo (required for MAP phase)
git clone https://github.com/krkn-chaos/krkn ~/krkn

# Virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Environment variables
cp .env.example .env
# Edit .env with: JIRA_API_TOKEN, JIRA_USERNAME, GITHUB_TOKEN, NEO4J_PASSWORD

# Start Neo4j (required)
podman run -d --name neo4j-coordinator \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password \
  neo4j:5-community

# Ingest knowledge base (one-time, ~6 min)
PYTHONPATH=. python -m src.knowledge.ingest ./chroma_data
```

### Run

#### Option 1: Claude Code (recommended)

```bash
cd ~/krkn-chaos-coordinator
claude
# Then type: /run-scan
# Interactive: asks for OCP version + agent selection
```

#### Option 2: CLI

```bash
# Single agent, single version
PYTHONPATH=. python src/main.py --release 4.21 --agent control_plane --use-llm

# Multiple agents
PYTHONPATH=. python src/main.py --release 4.21 --agent control_plane,networking --use-llm

# Multiple versions
PYTHONPATH=. python src/main.py --release 4.20,4.21 --use-llm

# All agents, all defaults
PYTHONPATH=. python src/main.py --release 4.21 --use-llm
```

#### Option 3: Streamlit Dashboard

```bash
PYTHONPATH=. streamlit run src/ui/web_dashboard.py --server.port 8501
```

### Adding a New Agent

Create a YAML file in `config/agents/`. No code changes needed.

```yaml
# config/agents/virtualization.yaml
name: virtualization
description: "OpenShift Virtualization / CNV / KubeVirt"
components:
  - "OpenShift Virtualization"
  - "Virtualization / virt-controller"
  - "Virtualization / virt-handler"
```

Then run:
```bash
PYTHONPATH=. python src/main.py --release 4.21 --agent virtualization --use-llm
```

See [config/agents/README.md](config/agents/README.md) for details and how to find OCPBUGS component names.

### Run Tests

```bash
# Unit tests (no external deps, ~0.2s)
PYTHONPATH=. pytest tests/unit/ -v                    # 187 tests

# Integration tests (requires Neo4j)
PYTHONPATH=. pytest tests/integration/ -v             # 13 tests

# All tests
PYTHONPATH=. pytest tests/ -v                         # 200 total

# Run filter eval (Sonnet vs Haiku comparison)
PYTHONPATH=. python -m src.evals.filter_eval --sample-size 20
```

## Project Structure

```
config/
└── agents/                        # Drop a YAML file here to add a new agent
    ├── control_plane.yaml
    ├── networking.yaml
    ├── node_machine.yaml
    ├── storage.yaml
    ├── operators_platform.yaml
    └── upgrade_lifecycle.yaml

src/
├── main.py                        # CLI entry point (multi-version, multi-agent)
├── models.py                      # Domain models (Bug, Gap, Observation, RunMetrics)
├── reasoning.py                   # LLM reasoning for MAP + ANALYZE phases
├── logging_util.py                # Structured JSON logging
├── coordinator/
│   └── orchestrator.py            # Dedup, approval queue, run summary
├── agents/
│   ├── base_agent.py              # Pipeline: DISCOVER→FILTER→MAP→ANALYZE→ACT→REMEMBER
│   ├── registry.py                # Auto-discovers agents from config/agents/*.yaml
│   ├── pr_creator.py              # Draft PR creation
│   ├── hub_generator.py           # krkn-hub boilerplate
│   └── docs_generator.py          # Website docs
├── apis/
│   ├── jira_client.py             # JIRA REST API (three-tier version query)
│   ├── sippy_client.py            # Sippy regressions + health
│   ├── github_client.py           # GitHub API
│   └── release_client.py          # Z-stream changelog enrichment
├── knowledge/
│   ├── chromadb_store.py          # Vector search (4 collections)
│   ├── neo4j_store.py             # Graph memory (single backend)
│   ├── component_map.py           # Delegates to registry for agent → component mapping
│   ├── filter_cache.py            # Semantic cache (Cache-Aside pattern)
│   ├── scenario_index.py          # Index krkn scenario YAMLs
│   └── scenario_knowledgebase.py  # krkn-knowledgebase integration
├── filter/
│   ├── chaos_filter.py            # Keyword filter (167 keywords, confidence scoring)
│   ├── llm_filter.py              # LLM filter (5 providers, token tracking)
│   ├── llm_config.py              # Per-phase model routing + auto-detection
│   ├── llm_tools.py               # Typed tool functions with Observation returns
│   └── llm_batch.py               # Anthropic Batch API support
├── evals/
│   ├── filter_eval.py             # Model comparison eval
│   ├── sampler.py                 # Stratified bug sampler
│   └── eval_report.py             # Eval metrics + pass criteria
└── ui/
    ├── terminal_ui.py             # Rich terminal dashboard
    └── web_dashboard.py           # Streamlit web dashboard
```

## ADR

Design document: [Confluence](https://redhat.atlassian.net/wiki/x/x4rTFg)

## License

Apache-2.0
