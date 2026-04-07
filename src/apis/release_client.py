"""OpenShift release controller client for z-stream changelogs."""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

RELEASE_CONTROLLER_URL = "https://amd64.ocp.releases.ci.openshift.org/api/v1/releasestream"
CINCINNATI_URL = "https://api.openshift.com/api/upgrades_info/v1/graph"


@dataclass(frozen=True)
class BugFix:
    """A bug fix from a z-stream changelog."""
    bug_key: str                     # "OCPBUGS-78393"
    fixed_in: str                    # "4.21.8"
    image: str                       # "machine-config-operator"
    commits: tuple[str, ...] = ()    # ("Fix race condition in logger", ...)


def get_release_versions(release: str, channel: str = "stable") -> list[dict]:
    """Get all z-stream versions for a release from Cincinnati.

    Returns list of dicts with version, errata_url for each z-stream.
    """
    url = f"{CINCINNATI_URL}?channel={channel}-{release}&arch=amd64"
    logger.info("Fetching release graph from: %s", url)

    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.error("Cincinnati query failed: %s", e)
        return []

    versions = []
    for node in data.get("nodes", []):
        version = node.get("version", "")
        if version.startswith(release):
            metadata = node.get("metadata", {})
            versions.append({
                "version": version,
                "errata_url": metadata.get("url", ""),
            })

    versions.sort(key=lambda v: v["version"])
    logger.info("Found %d z-stream versions for %s", len(versions), release)
    return versions


def _fetch_release_data(version: str) -> dict | None:
    """Fetch raw release data from the release controller."""
    url = f"{RELEASE_CONTROLLER_URL}/4-stable/release/{version}"

    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        logger.error("Release controller query failed for %s: %s", version, e)
        return None


def get_changelog_bugs(release: str, version: str) -> set[str]:
    """Get OCPBUGS keys fixed in a specific z-stream version."""
    data = _fetch_release_data(version)
    if not data:
        return set()

    changelog_b64 = data.get("changeLog", "")
    if not changelog_b64:
        return set()

    try:
        changelog_html = base64.b64decode(changelog_b64).decode("utf-8")
    except Exception as e:
        logger.error("Failed to decode changelog for %s: %s", version, e)
        return set()

    bugs = set(re.findall(r"OCPBUGS-\d+", changelog_html))
    logger.info("%s: %d bugs fixed", version, len(bugs))
    return bugs


def get_changelog_details(version: str) -> list[BugFix]:
    """Get detailed bug fix info from a z-stream changelog.

    Parses changeLogJson to extract: which image was updated,
    what commits were made, and which bugs they reference.
    """
    data = _fetch_release_data(version)
    if not data:
        return []

    changelog_json = data.get("changeLogJson", {})
    if isinstance(changelog_json, str):
        try:
            changelog_json = json.loads(changelog_json)
        except json.JSONDecodeError:
            return []

    updated_images = changelog_json.get("updatedImages", [])
    if not updated_images:
        return []

    fixes: list[BugFix] = []
    seen_bugs: set[str] = set()

    for img in updated_images:
        image_name = img.get("name", "unknown")
        commits = img.get("commits", [])

        for commit in commits:
            subject = commit.get("subject", "")
            # issues is a dict: {"OCPBUGS-78393": "https://...", ...}
            issues = commit.get("issues", {})
            if isinstance(issues, dict):
                bug_keys = [k for k in issues.keys() if k.startswith("OCPBUGS-")]
            else:
                bug_keys = []

            for bug_key in bug_keys:
                if bug_key not in seen_bugs:
                    # Collect all commit messages for this bug in this image
                    bug_commits = tuple(
                        c.get("subject", "")[:200]
                        for c in commits
                        if bug_key in (c.get("issues", {}) if isinstance(c.get("issues"), dict) else {})
                    )
                    fixes.append(BugFix(
                        bug_key=bug_key,
                        fixed_in=version,
                        image=image_name,
                        commits=bug_commits,
                    ))
                    seen_bugs.add(bug_key)

    # Also catch bugs from HTML changelog that aren't in structured JSON
    changelog_b64 = data.get("changeLog", "")
    if changelog_b64:
        try:
            html = base64.b64decode(changelog_b64).decode("utf-8")
            html_bugs = set(re.findall(r"OCPBUGS-\d+", html))
            for bug_key in html_bugs - seen_bugs:
                fixes.append(BugFix(
                    bug_key=bug_key,
                    fixed_in=version,
                    image="unknown",
                    commits=(),
                ))
        except Exception:
            pass

    logger.info("%s: %d bug fixes with details", version, len(fixes))
    return fixes


def get_all_fixed_bugs(release: str) -> dict[str, BugFix]:
    """Get all bugs fixed across all z-streams with full details.

    Returns: {"OCPBUGS-81323": BugFix(...), ...}
    First fix wins — if a bug appears in multiple changelogs,
    the earliest version is kept.
    """
    versions = get_release_versions(release)
    fixed: dict[str, BugFix] = {}

    for v in versions:
        version = v["version"]
        if version == f"{release}.0":
            continue  # Skip GA release, only z-streams

        details = get_changelog_details(version)
        for fix in details:
            if fix.bug_key not in fixed:
                fixed[fix.bug_key] = fix

    logger.info("Total bugs fixed across %s z-streams: %d", release, len(fixed))
    return fixed
