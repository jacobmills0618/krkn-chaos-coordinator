# Filter Keywords

## Common Keywords (`common.yaml`)

Shared across all agents. Edit this file to add/remove keywords that apply universally.

- `skip_keywords` — bugs matching these are NOT chaos-relevant (CVEs, backports, test infra)
- `chaos_keywords` — bugs matching these ARE chaos-relevant (crash, timeout, OOM, etc.)

## Agent-Specific Keywords

Add a `filter` section to any agent's YAML in `config/agents/`:

```yaml
# config/agents/virtualization.yaml
name: virtualization
components:
  - "OpenShift Virtualization"
filter:
  chaos_keywords:
    - "vm migration failed"
    - "virt-launcher crash"
    - "live migrate timeout"
    - "guest agent unresponsive"
  skip_keywords:
    - "cnv-must-gather"
    - "virtctl console"
```

Agent keywords are **merged** with common keywords (not replacing them).
