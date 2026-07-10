# JIRA Label Discovery Categories

Agents primarily discover bugs via **JIRA Component** (`components:` in each agent YAML). Many related bugs are filed under various components,yet carry domain-specific **JIRA labels**.

Label categories add an optional **discovery backfill** that runs on **every selected agent** after component search. Component discovery always runs first; label backfill adds tickets that match label substrings but were not already found.

## Quick Start

```bash
# All agents + OpenShift Virtualization label backfill
PYTHONPATH=. python src/main.py --release 4.21 \
  --discovery-label-categories openshift_virtualization

# Specific agents with label backfill
PYTHONPATH=. python src/main.py --release 4.21 \
  --agent control_plane,networking \
  --discovery-label-categories openshift_virtualization
```

In `/krkn-chaos-scan`, after selecting agents, the wizard asks **"Would you like to filter by Labels?"** and lists available categories from this directory.

## Adding a New Category

Drop a YAML file in this directory. No code changes needed.

```yaml
# config/labels/my_domain.yaml
name: my_domain
description: "Short description shown in the scan wizard"
label_substrings:
  - "substring-one"
  - "substring-two"
prompt_label: "My Domain — custom wizard label (my_domain)"   # optional
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique category ID (used in `--discovery-label-categories`) |
| `description` | No | Blurb for dynamic scan wizard labels |
| `label_substrings` | Yes | Exact JIRA label names used in JQL (`labels = "name"`) |
| `prompt_label` | No | Full wizard label when category is not in `scan_prompt.yaml` (must end with `(category_id)`) |

## Fixed Wizard Labels (`scan_prompt.yaml`)

Curate how categories appear in `/krkn-chaos-scan` by editing `fixed_labels` in `scan_prompt.yaml` — same pattern as `config/agents/scan_prompt.yaml`:

```yaml
fixed_labels:
  openshift_virtualization: "OpenShift Virtualization — CNV, KubeVirt, VM labels (openshift_virtualization)"
  my_domain: "My Domain — custom label (my_domain)"
```

Each label **must** end with `(category_id)` matching the category YAML `name` field.

Categories **not** listed in `fixed_labels` get a dynamic label from `format_label_category_prompt_option()` (uses `prompt_label` or derives from `name` + `description`).

## How Label Matching Works

JIRA JQL only supports **exact** label matches (`labels = "cnv"`). Each value in `label_substrings` is treated as an exact label name and combined with OR:

```jql
project = OCPBUGS AND (labels = "cnv" OR labels = "kubevirt" OR ...)
```

At run start, the coordinator prints the **global exact match count** for the configured window (e.g. `Global exact label matches (365d): 13 OCPBUGS bugs`). That number matches what you see in JIRA for the same JQL.

Per-agent log lines such as `+13 added` count bugs **newly added to that agent's pool** after component search — the same bug may appear in multiple agents' pools. **Do not sum those `+N added` lines across agents**; use the global count in the run header.

Red Hat JIRA blocks `/rest/api/3/label` (401); discovery uses the search API only.

## Built-in Category: OpenShift Virtualization

`openshift_virtualization.yaml` matches bugs with any of these **exact** labels:

- `cnv`
- `kubevirt`
- `openshift-virtualization`
- `virtualmachine`
- `virtual_machine`
- `virtual-machine`

Useful when virt bugs are filed under networking, HyperShift, or other components but tagged with virt-related labels.

## Discovery Order

For each agent run:

1. **Component search** — `component IN (...)` from agent YAML (+ 4-tier version logic when `--release` is set)
2. **Text JQL backfill** — optional `discovery_jql` on agent YAML (virtualization has one)
3. **Label backfill** — when `--discovery-label-categories` is set (applies to all agents in the run)

## CLI Reference

```bash
# Single category
--discovery-label-categories openshift_virtualization

# Multiple categories (substrings are merged)
--discovery-label-categories openshift_virtualization,my_domain

# Omit flag = component search only (default)
```

## Listing Categories

```bash
PYTHONPATH=. python -c "
from src.labels.registry import discover_label_categories, format_label_category_prompt_option
for name, cfg in sorted(discover_label_categories().items()):
    print(f'{name}\t{format_label_category_prompt_option(cfg)}')
"
```
