"""GitHub API client for reading krkn repos and creating PRs/issues."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def load_project_env() -> None:
    """Load .env from the project root (safe to call repeatedly)."""
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")


class GitHubClient:
    """Read krkn-chaos repos and create PRs/issues."""

    def __init__(self, token: str = ""):
        if not token:
            load_project_env()
            token = os.environ.get("GITHUB_TOKEN", "")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            }
        )

    @classmethod
    def from_env(cls) -> GitHubClient:
        """Create a client using GITHUB_TOKEN from the project .env file."""
        load_project_env()
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise ValueError("GITHUB_TOKEN is not set in .env or environment")
        return cls(token)

    def list_scenario_files(self, owner: str, repo: str, path: str = "scenarios") -> list[dict]:
        """List scenario YAML files in a krkn repo."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        try:
            response = self._session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            logger.error("GitHub list failed for %s/%s/%s: %s", owner, repo, path, e)
            return []

        items = response.json()
        if not isinstance(items, list):
            return []

        results = []
        for item in items:
            if item["type"] == "dir":
                results.extend(
                    self.list_scenario_files(owner, repo, item["path"])
                )
            elif item["name"].endswith((".yaml", ".yml")):
                results.append(
                    {"name": item["name"], "path": item["path"], "url": item["html_url"]}
                )
        return results

    def get_file_content(self, owner: str, repo: str, path: str) -> str | None:
        """Get the content of a file from a GitHub repo."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        try:
            response = self._session.get(
                url, headers={"Accept": "application/vnd.github.v3.raw"}, timeout=30
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logger.error("GitHub get file failed for %s/%s/%s: %s", owner, repo, path, e)
            return None

    def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None
    ) -> dict | None:
        """Create a GitHub issue. Retries without labels if label validation fails."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues"
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels

        try:
            response = self._session.post(url, json=payload, timeout=30)
            # If labels caused a 422 (label doesn't exist), retry without them
            if response.status_code == 422 and labels:
                logger.warning("Label validation failed, retrying without labels")
                payload.pop("labels", None)
                response = self._session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
            logger.info("Created issue: %s", result.get("html_url"))
            return result
        except requests.RequestException as e:
            error_body = ""
            if hasattr(e, "response") and e.response is not None:
                error_body = e.response.text[:500]
            logger.error("GitHub create issue failed: %s | %s", e, error_body)
            return None
