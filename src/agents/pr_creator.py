"""Create draft PRs on GitHub forks for chaos scenario changes.

Prerequisites (non-dry-run draft PRs will fail without these):

* **Up-to-date local krkn clone** at ``KRKN_REPO_PATH`` (default ``~/krkn``).
  Templates are copied from this tree; an outdated or missing clone yields empty
  or wrong candidates. Run ``git -C $KRKN_REPO_PATH fetch upstream && git -C
  $KRKN_REPO_PATH checkout main && git -C $KRKN_REPO_PATH merge upstream/main``
  (or equivalent) before creating PRs.
* **Git remotes on that clone:** ``upstream`` → ``krkn-chaos/krkn``, ``origin`` →
  your fork.
* **``GITHUB_FORK_OWNER``** set to the GitHub user/org that owns the fork (the
  PR ``head`` is ``{FORK_OWNER}:{branch}``).
* **Push permission** to ``origin`` and a ``GITHUB_TOKEN`` that can open PRs
  against the upstream repo.
* **MAP match:** ``gap.base_scenario`` must be set; otherwise PR creation is
  skipped (use a GitHub issue instead).

Note: the coordinator's interactive ACT path in ``main.py`` currently creates
GitHub *issues* only. Call ``create_scenario_pr`` explicitly (or wire it into
ACT) to open draft PRs.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

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

MAX_SCENARIO_CANDIDATES = 5
# Used only when a stub filename has no source path to inherit from.
DEFAULT_SCENARIO_OUTPUT_DIR = "scenarios/openshift"


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
    """Return ordered template paths from MAP's ``gap.base_scenario``.

    Requires a MAP match. Returns empty paths when ``base_scenario`` is unset.
    """
    if not gap.base_scenario:
        return [], "no MAP match (gap.base_scenario unset)", None

    text = _bug_text(gap)
    index = _load_scenario_index(str(krkn_path))
    rel_paths: list[str] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        if rel not in seen:
            rel_paths.append(rel)
            seen.add(rel)

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


def _candidate_filename(bug_key: str, source_path: Path) -> str:
    stem = source_path.stem.replace(".", "-")
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-")
    return f"{bug_key}-candidate-{stem}.yaml"


def _output_path_for_source(krkn_path: Path, source: Path, out_name: str) -> str:
    """Keep drafts in the same scenarios/ subdirectory as the source template."""
    rel_parent = source.parent.relative_to(krkn_path).as_posix()
    if not rel_parent.startswith("scenarios"):
        rel_parent = DEFAULT_SCENARIO_OUTPUT_DIR
    return f"{rel_parent}/{out_name}"


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

    Selection flow (MAP-only — no keyword fallback):
      1. Require ``gap.base_scenario`` from MAP/ANALYZE (Chroma + LLM)
      2. Add related files: same scenario type (index) + sibling YAML in same dir
      3. If ``base_scenario`` is unset, return no candidates (skip PR)

    Returns list of (repo_relative_output_path, yaml_content, source_template_path).
    """
    krkn_path = krkn_path or FORK_PATHS["krkn"]
    bug_key = gap.bug.key.upper()

    rel_paths, selection_source, scenario_type = _collect_template_paths(gap, krkn_path)

    if not gap.base_scenario:
        logger.info(
            "Skipping scenario drafts for %s: no MAP match (base_scenario unset)",
            gap.bug.key,
        )
        return []

    ordered_sources: list[Path] = []
    for rel in rel_paths:
        full = krkn_path / rel
        if full.is_file():
            ordered_sources.append(full)

    if not ordered_sources:
        logger.warning(
            "No krkn templates for %s (%s) under %s — MAP path %s not found locally",
            gap.bug.key,
            selection_source,
            krkn_path,
            gap.base_scenario,
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

        out_path = _output_path_for_source(krkn_path, source, out_name)
        content = _template_header(
            gap, scenario_type or "unknown", rel_source, out_name, selection_source,
        ) + body
        candidates.append((out_path, content, rel_source))

    return candidates


def generate_scenario_candidates(
    gap: GapAnalysis,
    krkn_path: Path | None = None,
    max_candidates: int = MAX_SCENARIO_CANDIDATES,
) -> list[tuple[str, str]]:
    """Generate suffix-named scenario drafts for a PR.

    Returns list of (repo_relative_path, yaml_content). Empty if no MAP match.
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

    Returns the first candidate, or a stub explaining why no draft was produced.
    """
    candidates = generate_scenario_candidates(
        gap, krkn_path=krkn_path, max_candidates=1,
    )
    if candidates:
        path, content = candidates[0]
        return content, Path(path).name

    bug = gap.bug
    filename = f"{bug.key.upper()}-candidate-stub.yaml"
    if not gap.base_scenario:
        selection = "no MAP match (gap.base_scenario unset)"
        reason = (
            "# No MAP match — gap.base_scenario is unset.\n"
            "# Draft PRs require a MAP/ANALYZE scenario path; create a GitHub issue instead,\n"
            "# or re-run MAP after ingesting scenarios into ChromaDB.\n"
        )
    else:
        selection = "MAP phase match (gap.base_scenario)"
        reason = (
            f"# MAP matched `{gap.base_scenario}` but the file was not found under KRKN_REPO_PATH.\n"
            "# Clone/update the local krkn repo and re-run.\n"
        )
    header = _template_header(gap, "unknown", "<none>", filename, selection)
    return header + reason, filename


def create_scenario_pr(
    github: GitHubClient,
    gap: GapAnalysis,
    scenario_yaml: str | None = None,
    scenario_filename: str | None = None,
    scenario_files: list[tuple[str, str]] | None = None,
    dry_run: bool = True,
) -> dict | None:
    """Create a PR on krkn-chaos/krkn with draft scenario YAML candidate(s).

    Requires a MAP match (``gap.base_scenario``) unless ``scenario_files`` are passed
    explicitly. Pass ``scenario_files`` as a list of (repo_relative_path, content),
    or omit to auto-generate suffix-named candidates from the local krkn repo.
    """
    repo_path = FORK_PATHS["krkn"]
    branch_name = f"chaos-coordinator/{gap.bug.key.lower()}"

    if scenario_files is None:
        if scenario_yaml and scenario_filename:
            scenario_files = [(f"{DEFAULT_SCENARIO_OUTPUT_DIR}/{scenario_filename}", scenario_yaml)]
        else:
            scenario_files = generate_scenario_candidates(gap)

    if not scenario_files:
        logger.error(
            "No scenario candidates for %s (need gap.base_scenario from MAP)",
            gap.bug.key,
        )
        return None

    scenario_paths = [path for path, _ in scenario_files]

    if dry_run:
        logger.info("DRY RUN — would create PR:")
        logger.info("  Repo: %s/%s -> %s/%s", FORK_OWNER, "krkn", UPSTREAM_OWNER, "krkn")
        logger.info("  Branch: %s", branch_name)
        logger.info(
            "  Requires: up-to-date KRKN_REPO_PATH=%s, remotes upstream+origin, "
            "GITHUB_FORK_OWNER=%s, push + PR permissions",
            repo_path,
            FORK_OWNER,
        )
        for path in scenario_paths:
            logger.info("  File: %s", path)
        return {"dry_run": True, "branch": branch_name, "files": scenario_paths}

    if not repo_path.exists():
        logger.error(
            "krkn repo not found at %s — set KRKN_REPO_PATH to an up-to-date local clone",
            repo_path,
        )
        return None

    if not _create_branch(repo_path, branch_name):
        logger.error(
            "Branch setup failed for %s — ensure remotes exist: "
            "`upstream` → %s/%s and `origin` → your fork (%s), and that you can push",
            branch_name,
            UPSTREAM_OWNER,
            _UPSTREAM_REPO,
            FORK_OWNER,
        )
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
