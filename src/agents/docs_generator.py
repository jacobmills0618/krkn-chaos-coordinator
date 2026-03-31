"""Generate krkn-chaos.dev website documentation for new scenarios."""

import logging

from src.models import GapAnalysis

logger = logging.getLogger(__name__)


def generate_website_docs(
    gap: GapAnalysis,
    scenario_name: str,
    scenario_type: str,
    env_vars: dict[str, str],
) -> dict[str, str]:
    """Generate Hugo website docs for a new scenario.

    Args:
        gap: The gap analysis
        scenario_name: Kebab-case (e.g., "etcd-throttling")
        scenario_type: Snake_case (e.g., "etcd_throttling_scenarios")
        env_vars: Dict of env var name → default value

    Returns:
        Dict of relative_path → file_content
    """
    display_name = scenario_name.replace("-", " ").title()
    bug = gap.bug
    base_path = f"content/en/docs/scenarios/{scenario_name}-scenario"

    files = {}

    # _index.md
    files[f"{base_path}/_index.md"] = f"""---
title: {display_name} Scenarios
description:
date: 2026-03-31
weight: 3
---

<krkn-hub-scenario id="{scenario_name}">

### {display_name} Scenarios

This scenario was identified by krkn-chaos-coordinator based on
[{bug.key}]({bug.url}): {bug.summary}

**Component:** {bug.component}
**Failure mode:** {bug.summary}

</krkn-hub-scenario>

## How to Run {display_name} Scenarios

{{{{< tabpane text=true >}}}}
  {{{{< tab header="**Krkn**" lang="krkn" >}}}}
{{{{< readfile file="_tab-krkn.md" >}}}}
  {{{{< /tab >}}}}
  {{{{< tab header="**Krkn-hub**" lang="krkn-hub" >}}}}
{{{{< readfile file="_tab-krkn-hub.md" >}}}}
  {{{{< /tab >}}}}
  {{{{< tab header="**Krknctl**" lang="krknctl" >}}}}
{{{{< readfile file="_tab-krknctl.md" >}}}}
  {{{{< /tab >}}}}
{{{{< /tabpane >}}}}
"""

    # _tab-krkn.md
    files[f"{base_path}/_tab-krkn.md"] = f"""Example scenario file: [{scenario_name}.yaml](https://github.com/krkn-chaos/krkn/blob/main/scenarios/openshift/{scenario_name}.yaml)

##### Sample scenario config

```yaml
- {scenario_type.rstrip('s')}:
    runs: 1
    # Add scenario-specific configuration here
```

### How to Use Plugin Name
Add the plugin name to the list of chaos_scenarios section in the config/config.yaml file
```yaml
kraken:
    kubeconfig_path: ~/.kube/config
    chaos_scenarios:
        - {scenario_type}:
            - scenarios/{scenario_name}.yaml
```

### Run

```bash
python run_kraken.py --config config/config.yaml
```
"""

    # _tab-krkn-hub.md
    param_rows = []
    for var, default in env_vars.items():
        param_rows.append(f"|{var}| Configuration for {var} |{default}|")
    param_table = "\n".join(param_rows)

    files[f"{base_path}/_tab-krkn-hub.md"] = f"""### {display_name} scenario

{bug.summary}

#### Run

```bash
$ podman run --name={scenario_name} \\
  --net=host \\
  --pull=always \\
  --env-host=true \\
  -v <path-to-kube-config>:/home/krkn/.kube/config:Z \\
  -d containers.krkn-chaos.dev/krkn-chaos/krkn-hub:{scenario_name}

$ podman logs -f <container_name or container_id>

$ podman inspect <container-name or container-id> \\
  --format "{{{{.State.ExitCode}}}}"
```

#### Supported parameters

|Parameter | Description | Default |
|----------|-------------|---------|
{param_table}
"""

    # _tab-krknctl.md
    krknctl_params = []
    for var in env_vars:
        name = var.lower().replace("_", "-")
        krknctl_params.append(f"`--{name}` | Configuration for {var} | string | {env_vars[var]} |")
    krknctl_table = "\n".join(krknctl_params)

    files[f"{base_path}/_tab-krknctl.md"] = f"""
```bash
krknctl run {scenario_name} (optional: --<parameter>:<value> )
```

Scenario specific parameters:
| Parameter | Description | Type | Default |
| --------- | ----------- | ---- | ------- |
{krknctl_table}

To see all available scenario options
```bash
krknctl run {scenario_name} --help
```
"""

    logger.info("Generated %d website doc files for %s", len(files), scenario_name)
    return files
