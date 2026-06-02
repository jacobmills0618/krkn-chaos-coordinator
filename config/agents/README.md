# Adding a New Domain Agent

To add a new agent, create a YAML file in this directory. No code changes needed.

## Template

```yaml
name: my_domain
description: "Short description of what this agent covers"
components:
  - "OCPBUGS Component Name 1"
  - "OCPBUGS Component Name 2"
  - "OCPBUGS Component Name / Sub-component"
```

## Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique agent identifier (used in `--agent` flag) |
| `description` | No | Short description shown in help text |
| `components` | Yes | List of OCPBUGS component names this agent monitors |
| `docs` | No | List of GitHub repos to ingest into ChromaDB for domain-specific context |

## Finding Component Names

Component names must match exactly what JIRA uses. To find the correct names:

```bash
# Search JIRA for components in a project
curl -s -u "$JIRA_USERNAME:$JIRA_API_TOKEN" \
  "https://redhat.atlassian.net/rest/api/3/project/OCPBUGS/components" | \
  python -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]"
```

## Example: Adding a Virtualization Agent

```yaml
# config/agents/virtualization.yaml
name: virtualization
description: "OpenShift Virtualization / CNV / KubeVirt"
components:
  - "OpenShift Virtualization"
  - "Container-native Virtualization"
  - "Virtualization / virt-controller"
  - "Virtualization / virt-handler"
  - "Virtualization / virt-operator"
filter:
  chaos_keywords:
    - "vm migration failed"
    - "virt-launcher crash"
    - "live migrate timeout"
  skip_keywords:
    - "cnv-must-gather"
docs:
  # GitHub repo path
  - type: github
    owner: kubevirt
    repo: kubevirt
    path: docs

  # Local files/directory
  - type: local
    path: /home/user/cnv-architecture-docs

  # Web URL (single page, fetched and chunked)
  - type: url
    url: https://kubevirt.io/user-guide/architecture/
```

### Doc source types

| Type | Fields | What it does |
|------|--------|-------------|
| `github` | `owner`, `repo`, `path` | Recursively fetches .md/.adoc/.yaml files from a GitHub repo directory |
| `local` | `path` | Reads .md/.adoc/.txt/.yaml files from a local directory |
| `url` | `url` | Fetches a single web page, strips HTML, chunks into ChromaDB |

If `type` is omitted, defaults to `github` (backward compatible).

Run ingestion after adding docs:
```bash
PYTHONPATH=. python -m src.knowledge.ingest ./chroma_data
```

Then run:
```bash
PYTHONPATH=. python src/main.py --release 4.21 --agent virtualization --use-llm
```

The system auto-discovers the new YAML file — no imports, no registration, no code changes.
