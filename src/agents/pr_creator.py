"""Create draft PRs on GitHub forks for chaos scenario changes."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from src.agents.act import _infer_injection_method, _scenario_type_from_plugin
from src.apis.github_client import GitHubClient
from src.knowledge.scenario_index import ScenarioInfo, index_scenarios_from_repo
from src.models import GapAnalysis

load_dotenv()

logger = logging.getLogger(__name__)

_krkn_repo_url = os.environ.get("KRKN_REPO_URL", "https://github.com/krkn-chaos/krkn")
_url_parts = urlparse(_krkn_repo_url).path.strip("/").split("/")
UPSTREAM_OWNER = _url_parts[0] if len(_url_parts) >= 1 else "krkn-chaos"
_UPSTREAM_REPO = _url_parts[1] if len(_url_parts) >= 2 else "krkn"

# Local fork paths
FORK_PATHS = {
    "krkn": Path(os.environ.get("KRKN_REPO_PATH", str(Path.home() / "krkn"))),
    "krkn-hub": Path.home() / "krkn-hub",
    "website": Path.home() / "website",
    "release": Path.home() / "release",
}

FORK_OWNER = os.environ.get("GITHUB_FORK_OWNER", "krkn-chaos")

# Fallback example templates when MAP did not set base_scenario (plugin dir → paths).
FALLBACK_SCENARIO_EXAMPLES: dict[str, tuple[str, ...]] = {
    "hog_scenarios": ("scenarios/kube/cpu-hog.yml", "scenarios/kube/memory-hog.yml"),
    "node_scenarios": (
        "scenarios/openshift/aws_node_scenarios.yml",
        "scenarios/openshift/baremetal_node_scenarios.yml",
    ),
    "network_chaos_scenarios": ("scenarios/openshift/network_chaos.yaml",),
    "network_chaos_ng_scenarios": ("scenarios/kube/pod-network-chaos.yml",),
    "application_outages_scenarios": ("scenarios/openshift/app_outage.yaml",),
    "container_scenarios": ("scenarios/openshift/container_etcd.yml",),
    "http_load_scenarios": ("scenarios/kube/http_load_scenario.yml",),
    "kubevirt_vm_outage": ("scenarios/kubevirt/kubevirt-vm-outage.yaml",),
    "managedcluster_scenarios": ("scenarios/kube/managedcluster_scenarios_example.yml",),
    "pvc_scenarios": ("scenarios/openshift/pvc_scenario.yaml",),
    "service_disruption_scenarios": ("scenarios/openshift/ingress_namespace.yaml",),
    "service_hijacking_scenarios": ("scenarios/kube/service_hijacking.yaml",),
    "cluster_shut_down_scenarios": ("scenarios/openshift/cluster_shut_down_scenario.yml",),
    "storage_throttle_scenarios": ("scenarios/openshift/storage_throttle.yaml",),
    "syn_flood_scenarios": ("scenarios/kube/syn_flood.yaml",),
    "time_scenarios": ("scenarios/openshift/time_scenarios_example.yml",),
    "zone_outages_scenarios": ("scenarios/openshift/zone_outage.yaml",),
    "pod_disruption_scenarios": (
        "scenarios/openshift/etcd.yml",
        "scenarios/openshift/container_etcd.yml",
    ),
}

# Backward-compatible alias for tests and external imports.
CANONICAL_SCENARIO_EXAMPLES = FALLBACK_SCENARIO_EXAMPLES

MAX_SCENARIO_CANDIDATES = 5
SCENARIO_OUTPUT_DIR = "scenarios/openshift"


@lru_cache(maxsize=4)
def _load_scenario_index(krkn_path: str) -> tuple[ScenarioInfo, ...]:
    return tuple(index_scenarios_from_repo(Path(krkn_path)))


def _run_git(repo_path: Path, *args: str) -> tuple[int, str]:
    """Run a git command in a repo directory."""
    cmd = ["git", "-C", str(repo_path)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout.strip() + result.stderr.strip()


def _create_branch(repo_path: Path, branch_name: str) -> bool:
    """Create a new branch from upstream main."""
    code, out = _run_git(repo_path, "fetch", "upstream", "main")
    if code != 0:
        logger.error("Failed to fetch upstream: %s", out)
        return False

    code, out = _run_git(repo_path, "checkout", "-b", branch_name, "upstream/main")
    if code != 0:
        code, out = _run_git(repo_path, "checkout", branch_name)
        if code != 0:
            logger.error("Failed to create/checkout branch %s: %s", branch_name, out)
            return False

    return True


def _write_file(repo_path: Path, file_path: str, content: str) -> bool:
    """Write a file in the repo."""
    full_path = repo_path / file_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return True


def _commit_and_push(repo_path: Path, branch_name: str, message: str, files: list[str]) -> bool:
    """Stage files, commit, and push to origin."""
    for f in files:
        _run_git(repo_path, "add", f)

    code, out = _run_git(repo_path, "commit", "-m", message)
    if code != 0:
        logger.error("Failed to commit: %s", out)
        return False

    code, out = _run_git(repo_path, "push", "-u", "origin", branch_name)
    if code != 0:
        logger.error("Failed to push: %s", out)
        return False

    return True


def _cleanup_branch(repo_path: Path) -> None:
    """Switch back to main branch."""
    _run_git(repo_path, "checkout", "main")


def _bug_text(gap: GapAnalysis) -> str:
    """Combined JIRA fields used to rank related scenario templates."""
    bug = gap.bug
    parts = [bug.summary, bug.description or "", bug.component]
    if gap.reasoning:
        parts.append(gap.reasoning)
    return " ".join(parts).lower()


def _score_template_path(rel_path: str, text: str) -> int:
    """Rank templates whose path/filename tokens appear in the bug text."""
    score = 0
    for token in re.split(r"[_/.\s-]+", rel_path.lower()):
        if len(token) > 2 and token in text:
            score += 1
    return score


def _lookup_scenario_info(index: tuple[ScenarioInfo, ...], rel_path: str) -> ScenarioInfo | None:
    for info in index:
        if info.file_path == rel_path:
            return info
    return None


def _sibling_scenario_paths(krkn_path: Path, base_rel: str) -> list[str]:
    """Other scenario YAML files in the same directory as base_scenario."""
    parent = (krkn_path / base_rel).parent
    if not parent.is_dir():
        return []
    siblings: list[str] = []
    for path in sorted(parent.glob("*.y*ml")):
        rel = str(path.relative_to(krkn_path))
        if rel != base_rel:
            siblings.append(rel)
    return siblings


def _related_paths_from_index(
    index: tuple[ScenarioInfo, ...],
    scenario_type: str,
    exclude: set[str],
) -> list[str]:
    return [
        info.file_path
        for info in index
        if info.scenario_type == scenario_type and info.file_path not in exclude
    ]


def _collect_template_paths(gap: GapAnalysis, krkn_path: Path) -> tuple[list[str], str, str | None]:
    """Return ordered template paths, selection source label, and scenario type hint."""
    text = _bug_text(gap)
    index = _load_scenario_index(str(krkn_path))
    rel_paths: list[str] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        if rel not in seen:
            rel_paths.append(rel)
            seen.add(rel)

    if gap.base_scenario:
        add(gap.base_scenario)

        info = _lookup_scenario_info(index, gap.base_scenario)
        if info:
            for rel in _related_paths_from_index(index, info.scenario_type, seen):
                add(rel)

        for rel in _sibling_scenario_paths(krkn_path, gap.base_scenario):
            add(rel)

        rel_paths[1:] = sorted(
            rel_paths[1:],
            key=lambda p: _score_template_path(p, text),
            reverse=True,
        )
        scenario_type = info.scenario_type if info else "unknown"
        return rel_paths, "MAP phase match (gap.base_scenario)", scenario_type

    _, plugin, _ = _infer_injection_method(gap)
    scenario_type = _scenario_type_from_plugin(plugin)
    fallback = list(FALLBACK_SCENARIO_EXAMPLES.get(scenario_type, ()))
    fallback.sort(key=lambda p: _score_template_path(p, text), reverse=True)
    for rel in fallback:
        add(rel)

    return rel_paths, "keyword fallback (no MAP match)", scenario_type


def _candidate_filename(bug_key: str, source_path: Path) -> str:
    stem = source_path.stem.replace(".", "-")
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-")
    return f"{bug_key}-candidate-{stem}.yaml"


def _template_header(
    gap: GapAnalysis,
    scenario_type: str,
    source_rel: str,
    candidate_name: str,
    selection_source: str,
) -> str:
    lines = [
        f"# Draft chaos scenario for {gap.bug.key}: {gap.bug.summary}",
        "# Generated by krkn-chaos-coordinator — pick ONE candidate, edit placeholders, delete the rest.",
        f"# Bug: {gap.bug.url}",
        f"# Selection: {selection_source}",
    ]
    if gap.base_scenario:
        lines.append(f"# MAP base scenario: {gap.base_scenario}")
    lines.extend([
        f"# Krkn scenario type: {scenario_type}",
        f"# Source template: {source_rel}",
        f"# Draft filename: {candidate_name}",
        "#",
    ])
    return "\n".join(lines) + "\n"


def collect_related_scenario_templates(
    gap: GapAnalysis,
    krkn_path: Path | None = None,
    max_candidates: int = MAX_SCENARIO_CANDIDATES,
) -> list[tuple[str, str, str]]:
    """Collect suffix-named drafts copied from krkn example files.

    Selection flow:
      1. **Primary:** ``gap.base_scenario`` from MAP/ANALYZE (Chroma + LLM)
      2. **Related:** same scenario type (scenario index) + sibling YAML in same dir
      3. **Fallback:** keyword inference + ``FALLBACK_SCENARIO_EXAMPLES`` only when
         ``base_scenario`` is unset

    Returns list of (repo_relative_output_path, yaml_content, source_template_path).
    """
    krkn_path = krkn_path or FORK_PATHS["krkn"]
    bug_key = gap.bug.key.upper()

    rel_paths, selection_source, scenario_type = _collect_template_paths(gap, krkn_path)

    ordered_sources: list[Path] = []
    for rel in rel_paths:
        full = krkn_path / rel
        if full.is_file():
            ordered_sources.append(full)

    if not ordered_sources:
        logger.warning(
            "No krkn templates for %s (%s) under %s",
            gap.bug.key,
            selection_source,
            krkn_path,
        )
        return []

    candidates: list[tuple[str, str, str]] = []
    used_names: set[str] = set()

    for source in ordered_sources[:max_candidates]:
        rel_source = str(source.relative_to(krkn_path))
        body = source.read_text()
        out_name = _candidate_filename(bug_key, source)
        while out_name in used_names:
            out_name = out_name.replace(".yaml", "-alt.yaml")
        used_names.add(out_name)

        out_path = f"{SCENARIO_OUTPUT_DIR}/{out_name}"
        content = _template_header(
            gap, scenario_type, rel_source, out_name, selection_source,
        ) + body
        candidates.append((out_path, content, rel_source))

    return candidates


def generate_scenario_candidates(
    gap: GapAnalysis,
    krkn_path: Path | None = None,
    max_candidates: int = MAX_SCENARIO_CANDIDATES,
) -> list[tuple[str, str]]:
    """Generate suffix-named scenario drafts for a PR.

    Returns list of (repo_relative_path, yaml_content).
    """
    return [
        (path, content)
        for path, content, _source in collect_related_scenario_templates(
            gap, krkn_path=krkn_path, max_candidates=max_candidates,
        )
    ]


def generate_scenario_yaml(
    gap: GapAnalysis,
    krkn_path: Path | None = None,
) -> tuple[str, str]:
    """Generate scenario YAML for backward-compatible single-file callers.

    Returns the first candidate, or a minimal stub if no templates exist.
    """
    candidates = generate_scenario_candidates(
        gap, krkn_path=krkn_path, max_candidates=1,
    )
    if candidates:
        path, content = candidates[0]
        return content, Path(path).name

    bug = gap.bug
    component = bug.component.lower().replace(" ", "_").replace("/", "_")
    bug_id = bug.key.lower().replace("-", "_")
    filename = f"{bug.key.upper()}-candidate-stub.yaml"
    _, _, scenario_type = _collect_template_paths(gap, krkn_path or FORK_PATHS["krkn"])
    selection = (
        "MAP phase match (gap.base_scenario)"
        if gap.base_scenario
        else "keyword fallback (no MAP match)"
    )
    header = _template_header(gap, scenario_type or "unknown", "<none>", filename, selection)
    stub = (
        f"{header}# No matching templates found under KRKN_REPO_PATH.\n"
        f"# Clone krkn and re-run, or author a scenario manually for {component}/{bug_id}.\n"
    )
    return stub, filename


def create_scenario_pr(
    github: GitHubClient,
    gap: GapAnalysis,
    scenario_yaml: str | None = None,
    scenario_filename: str | None = None,
    scenario_files: list[tuple[str, str]] | None = None,
    dry_run: bool = True,
) -> dict | None:
    """Create a PR on krkn-chaos/krkn with draft scenario YAML candidate(s).

    Pass ``scenario_files`` as a list of (repo_relative_path, content), or omit to
    auto-generate suffix-named candidates from the local krkn repo.
    """
    repo_path = FORK_PATHS["krkn"]
    branch_name = f"chaos-coordinator/{gap.bug.key.lower()}"

    if scenario_files is None:
        scenario_files = generate_scenario_candidates(gap)
    elif scenario_yaml and scenario_filename:
        scenario_files = [(f"{SCENARIO_OUTPUT_DIR}/{scenario_filename}", scenario_yaml)]

    if not scenario_files:
        logger.error("No scenario candidates to add for %s", gap.bug.key)
        return None

    scenario_paths = [path for path, _ in scenario_files]

    if dry_run:
        logger.info("DRY RUN — would create PR:")
        logger.info("  Repo: %s/%s -> %s/%s", FORK_OWNER, "krkn", UPSTREAM_OWNER, "krkn")
        logger.info("  Branch: %s", branch_name)
        for path in scenario_paths:
            logger.info("  File: %s", path)
        return {"dry_run": True, "branch": branch_name, "files": scenario_paths}

    if not repo_path.exists():
        logger.error("krkn repo not found at %s", repo_path)
        return None

    if not _create_branch(repo_path, branch_name):
        return None

    try:
        for path, content in scenario_files:
            _write_file(repo_path, path, content)

        commit_msg = (
            f"feat: add chaos scenario drafts for {gap.bug.key}\n\n"
            f"Bug: {gap.bug.url}\n"
            f"Failure mode: {gap.bug.summary}\n"
            f"Pick one candidate YAML, edit placeholders, delete the rest.\n"
            f"Generated by krkn-chaos-coordinator"
        )
        if not _commit_and_push(repo_path, branch_name, commit_msg, scenario_paths):
            return None

        file_lines = "\n".join(f"- `{p}`" for p in scenario_paths)
        base_line = (
            f"**MAP match:** `{gap.base_scenario}`\n\n"
            if gap.base_scenario
            else ""
        )
        pr_title = f"[chaos-coordinator] {gap.bug.key}: Add chaos scenario drafts"
        pr_body = (
            f"## Auto-generated Chaos Scenario Drafts\n\n"
            f"**Bug:** [{gap.bug.key}]({gap.bug.url})\n"
            f"**Component:** {gap.bug.component}\n"
            f"**Failure Mode:** {gap.bug.summary}\n\n"
            f"{base_line}"
            f"### What to do\n\n"
            f"1. Review the candidate files copied from existing krkn scenario templates\n"
            f"2. **Keep one** draft, edit placeholders, **delete the others**\n"
            f"3. Register the final scenario in krkn `config/config.yaml` if needed\n\n"
            f"### Candidate files\n\n"
            f"{file_lines}\n\n"
            f"### Confidence\n\n"
            f"{gap.reasoning}\n\n"
            f"---\n"
            f"*Generated by krkn-chaos-coordinator*"
        )

        url = f"https://api.github.com/repos/{UPSTREAM_OWNER}/{_UPSTREAM_REPO}/pulls"
        payload = {
            "title": pr_title,
            "body": pr_body,
            "head": f"{FORK_OWNER}:{branch_name}",
            "base": "main",
            "draft": True,
        }

        try:
            response = github._session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            pr = response.json()
            logger.info("Created PR: %s", pr.get("html_url"))
            return pr
        except Exception as e:
            logger.error("Failed to create PR: %s", e)
            return None

    finally:
        _cleanup_branch(repo_path)
