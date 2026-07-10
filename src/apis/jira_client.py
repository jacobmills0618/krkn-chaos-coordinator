"""JIRA REST API client for querying OCPBUGS."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from src.models import Bug

logger = logging.getLogger(__name__)

# Jira labels are exact-match in JQL; batch to stay within query size limits.
_LABEL_JQL_BATCH_SIZE = 100


def filter_labels_by_substrings(
    labels: list[str],
    substrings: tuple[str, ...] | list[str],
) -> list[str]:
    """Return labels whose text contains any of the given substrings (case-insensitive)."""
    if not substrings:
        return []
    subs = [s.lower() for s in substrings]
    return sorted(
        label for label in labels
        if any(sub in label.lower() for sub in subs)
    )


def issue_labels_match_substrings(
    labels: list[str],
    substrings: tuple[str, ...] | list[str],
) -> bool:
    """True if any issue label contains any configured substring (case-insensitive)."""
    if not labels or not substrings:
        return False
    subs = [s.lower() for s in substrings]
    return any(any(sub in label.lower() for sub in subs) for label in labels)


def build_exact_label_substrings_jql(substrings: tuple[str, ...] | list[str]) -> str:
    """JQL for exact label names (same style as JIRA AI / labels in (...))."""
    terms = " OR ".join(f'labels = "{s}"' for s in substrings)
    return f"project = OCPBUGS AND ({terms})"


def build_label_discovery_jql(
    labels: list[str],
    batch_size: int = _LABEL_JQL_BATCH_SIZE,
) -> list[str]:
    """Build one JQL query per batch of matching labels."""
    if not labels:
        return []
    clauses: list[str] = []
    for i in range(0, len(labels), batch_size):
        batch = labels[i : i + batch_size]
        quoted = ", ".join(f'"{label}"' for label in batch)
        clauses.append(f"project = OCPBUGS AND labels in ({quoted})")
    return clauses


def _next_version(release: str) -> str:
    """Return the next minor version string. '4.21' → '4.22', '5.0' → '5.1'."""
    parts = release.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _extract_text_from_adf(doc: dict) -> str:
    """Extract plain text from Atlassian Document Format (ADF).

    JIRA REST API v3 returns descriptions as ADF (nested JSON) instead of
    plain strings. This recursively extracts text nodes.
    """
    parts: list[str] = []

    def _walk(node: dict | list) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []):
            _walk(child)

    _walk(doc)
    return " ".join(parts)


@dataclass(frozen=True)
class JiraConfig:
    url: str
    username: str
    api_token: str


class JiraClient:
    """Query JIRA REST API for OpenShift bugs by component."""

    def __init__(self, config: JiraConfig):
        self._config = config
        self._session = requests.Session()
        self._session.auth = (config.username, config.api_token)
        self._session.headers.update({"Accept": "application/json"})

    def get_bugs_by_components(
        self,
        components: list[str],
        days: int = 14,
        max_results: int = 2000,
        priority_filter: bool = True,
        release: str | None = None,
    ) -> list[Bug]:
        """Query OCPBUGS for recent bugs in the given components.

        When priority_filter is True, fetches Critical/Major/Blocker bugs first,
        then backfills with remaining bugs up to max_results.

        4-tier version query when release is set (e.g. "4.21"):
          Tier 1: bugs tagged with target release (>= 4.21, < 4.22)
          Tier 2: open bugs from older versions (unfixed, likely still present)
          Tier 3: open bugs from newer versions (if reported on 5.0, likely affects 4.21)
          Tier 4: bugs with no affectedVersion set
        """
        component_list = ", ".join(f'"{c}"' for c in components)

        if release:
            next_minor = _next_version(release)
            bugs = self._four_tier_version_query(
                component_list, release, next_minor, days, max_results, priority_filter,
            )
            return bugs

        if not priority_filter:
            jql = (
                f"project = OCPBUGS AND component IN ({component_list})"
                f" AND created >= -{days}d ORDER BY created DESC"
            )
            return self._search(jql, max_results)

        return self._priority_then_backfill(component_list, "", days, max_results)

    def discover_bugs(
        self,
        components: list[str],
        days: int = 14,
        max_results: int = 2000,
        release: str | None = None,
        discovery_jql: str | None = None,
        discovery_label_substrings: tuple[str, ...] | None = None,
    ) -> list[Bug]:
        """Fetch bugs by component, optionally backfilling with JQL and label searches."""
        bugs = self.get_bugs_by_components(
            components, days=days, max_results=max_results, release=release,
        )
        bugs = self._apply_jql_backfill(
            bugs, discovery_jql, days, max_results, backfill_name="JQL text",
        )
        if discovery_label_substrings:
            exact_jql = build_exact_label_substrings_jql(discovery_label_substrings)
            logger.info("Label discovery JQL (exact): %s", exact_jql)
            bugs = self._apply_jql_backfill(
                bugs, exact_jql, days, max_results, backfill_name="label-exact",
            )
        return bugs[:max_results]

    def _apply_jql_backfill(
        self,
        bugs: list[Bug],
        jql: str | None,
        days: int,
        max_results: int,
        backfill_name: str,
    ) -> list[Bug]:
        """Append bugs from a JQL backfill query, deduplicating by issue key."""
        if not jql:
            return bugs

        remaining = max_results - len(bugs)
        if remaining <= 0:
            return bugs[:max_results]

        seen_keys = {b.key for b in bugs}
        full_jql = f"({jql}) AND created >= -{days}d ORDER BY created DESC"
        extra = self._search(full_jql, remaining)
        jira_returned = len(extra)
        backfill_count = 0
        for bug in extra:
            if bug.key in seen_keys:
                continue
            bugs.append(bug)
            seen_keys.add(bug.key)
            backfill_count += 1
            if len(bugs) >= max_results:
                break

        already_in_pool = jira_returned - backfill_count
        if backfill_name.startswith("label"):
            logger.info(
                "Discovery %s backfill: JIRA returned %d, %d already in agent pool, "
                "+%d added (agent pool now %d). "
                "Do not sum '+N added' across agents — use global label count in run header.",
                backfill_name,
                jira_returned,
                already_in_pool,
                backfill_count,
                len(bugs),
            )
        elif backfill_count:
            logger.info(
                "Discovery %s backfill: %d total bugs (+%d new)",
                backfill_name, len(bugs), backfill_count,
            )
        return bugs[:max_results]

    def count_jql_results(self, jql: str, max_count: int = 10000) -> int:
        """Count issues matching JQL (paginated, for global label census)."""
        url = f"{self._config.url}/rest/api/3/search/jql"
        total = 0
        next_token = None
        page_size = min(max_count, 100)

        while total < max_count:
            params = {"jql": jql, "maxResults": page_size, "fields": "key"}
            if next_token:
                params["nextPageToken"] = next_token
            try:
                response = self._session.get(url, params=params, timeout=30)
                response.raise_for_status()
            except requests.RequestException as e:
                logger.error("JIRA count query failed: %s", e)
                break

            data = response.json()
            issues = data.get("issues", [])
            if not issues:
                break
            total += len(issues)
            if data.get("isLast", True) or not data.get("nextPageToken"):
                break
            next_token = data["nextPageToken"]

        return total

    def _fetch_matching_labels(self, substrings: tuple[str, ...]) -> list[str]:
        """Fetch all Jira site labels and return those matching any substring."""
        url = f"{self._config.url}/rest/api/3/label"
        all_labels: list[str] = []
        start_at = 0
        page_size = 1000

        while True:
            try:
                response = self._session.get(
                    url,
                    params={"startAt": start_at, "maxResults": page_size},
                    timeout=30,
                )
                response.raise_for_status()
            except requests.RequestException as e:
                logger.error("JIRA label fetch failed: %s", e)
                break

            data = response.json()
            values = data.get("values", [])
            if not values:
                break

            all_labels.extend(values)
            if data.get("isLast", True):
                break

            start_at += len(values)
            total = data.get("total")
            if total is not None and start_at >= total:
                break

        return filter_labels_by_substrings(all_labels, substrings)

    def _backfill_bugs_by_label_substrings(
        self,
        bugs: list[Bug],
        substrings: tuple[str, ...],
        days: int,
        max_results: int,
    ) -> list[Bug]:
        """Fallback when /rest/api/3/label is blocked: scan issues that have labels set."""
        remaining = max_results - len(bugs)
        if remaining <= 0:
            return bugs[:max_results]

        logger.warning(
            "Label API unavailable or returned no matches; scanning OCPBUGS issues "
            "with labels for substrings %s",
            list(substrings),
        )
        seen_keys = {b.key for b in bugs}
        jql = (
            "project = OCPBUGS AND labels IS NOT EMPTY "
            f"AND created >= -{days}d ORDER BY created DESC"
        )
        url = f"{self._config.url}/rest/api/3/search/jql"
        page_size = min(remaining * 3, 100)
        next_token = None
        backfill_count = 0

        while len(bugs) < max_results:
            params = {
                "jql": jql,
                "maxResults": page_size,
                "fields": "summary,description,status,priority,components,created,labels",
            }
            if next_token:
                params["nextPageToken"] = next_token

            try:
                response = self._session.get(url, params=params, timeout=30)
                response.raise_for_status()
            except requests.RequestException as e:
                logger.error("JIRA label issue-scan failed: %s", e)
                break

            data = response.json()
            issues = data.get("issues", [])
            if not issues:
                break

            for issue in issues:
                fields = issue["fields"]
                labels = fields.get("labels") or []
                if not issue_labels_match_substrings(labels, substrings):
                    continue
                key = issue["key"]
                if key in seen_keys:
                    continue

                components = fields.get("components", [])
                component_names = (
                    tuple(c["name"] for c in components) if components else ("Unknown",)
                )
                description = fields.get("description", "") or ""
                if isinstance(description, dict):
                    description = _extract_text_from_adf(description)

                bugs.append(
                    Bug(
                        key=key,
                        summary=fields.get("summary", ""),
                        description=description,
                        component=", ".join(component_names),
                        priority=fields.get("priority", {}).get("name", "Unknown"),
                        status=fields.get("status", {}).get("name", "Unknown"),
                        created=fields.get("created", ""),
                        url=f"{self._config.url}/browse/{key}",
                        all_components=component_names,
                    )
                )
                seen_keys.add(key)
                backfill_count += 1
                if len(bugs) >= max_results:
                    break

            next_token = data.get("nextPageToken")
            if data.get("isLast", True) or not next_token or len(bugs) >= max_results:
                break

        if backfill_count:
            logger.info(
                "Discovery label issue-scan backfill: %d total bugs (+%d new)",
                len(bugs), backfill_count,
            )
        return bugs[:max_results]

    def _four_tier_version_query(
        self,
        component_list: str,
        release: str,
        next_minor: str,
        days: int,
        max_results: int,
        priority_filter: bool,
    ) -> list[Bug]:
        """Fetch bugs using 4-tier version matching."""
        seen_keys: set[str] = set()
        all_bugs: list[Bug] = []

        # Tier 1: Bugs explicitly tagged with the target release
        tier1_clause = f' AND affectedVersion >= "{release}" AND affectedVersion < "{next_minor}"'
        tier1 = self._priority_then_backfill(
            component_list, tier1_clause, days, max_results,
        ) if priority_filter else self._search(
            f"project = OCPBUGS AND component IN ({component_list})"
            f"{tier1_clause} AND created >= -{days}d ORDER BY created DESC",
            max_results,
        )
        for b in tier1:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 1 (%s.*): %d bugs", release, len(tier1))

        if len(all_bugs) >= max_results:
            return all_bugs[:max_results]

        # Tier 2: Open bugs from older versions (unfixed, likely still present)
        remaining = max_results - len(all_bugs)
        tier2_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f' AND affectedVersion < "{release}"'
            f' AND status NOT IN (Closed, Verified, "Release Pending")'
            f" AND created >= -{days}d ORDER BY priority ASC, created DESC"
        )
        tier2 = self._search(tier2_jql, remaining)
        for b in tier2:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 2 (older, open): %d bugs", len(tier2))

        if len(all_bugs) >= max_results:
            return all_bugs[:max_results]

        # Tier 3: Open bugs from newer versions (if it exists on 5.0, it likely exists on 4.21)
        remaining = max_results - len(all_bugs)
        tier3_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f' AND affectedVersion >= "{next_minor}"'
            f' AND status NOT IN (Closed, Verified, "Release Pending")'
            f" AND created >= -{days}d ORDER BY priority ASC, created DESC"
        )
        tier3 = self._search(tier3_jql, remaining)
        for b in tier3:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 3 (newer, open): %d bugs", len(tier3))

        if len(all_bugs) >= max_results:
            return all_bugs[:max_results]

        # Tier 4: Bugs with no affectedVersion set
        remaining = max_results - len(all_bugs)
        tier4_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f" AND affectedVersion IS EMPTY"
            f" AND created >= -{days}d ORDER BY created DESC"
        )
        tier4 = self._search(tier4_jql, remaining)
        for b in tier4:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 4 (no version): %d bugs", len(tier4))

        return all_bugs[:max_results]

    def _priority_then_backfill(
        self,
        component_list: str,
        version_clause: str,
        days: int,
        max_results: int,
    ) -> list[Bug]:
        """Fetch priority bugs first, then backfill with remaining."""
        priority_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f"{version_clause}"
            f" AND priority IN (Blocker, Critical, Major)"
            f" AND created >= -{days}d ORDER BY priority ASC, created DESC"
        )
        priority_bugs = self._search(priority_jql, max_results)

        if len(priority_bugs) >= max_results:
            return priority_bugs

        seen_keys = {b.key for b in priority_bugs}
        remaining = max_results - len(priority_bugs)
        all_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f"{version_clause}"
            f" AND created >= -{days}d ORDER BY created DESC"
        )
        all_bugs = self._search(all_jql, max_results)
        backfill = [b for b in all_bugs if b.key not in seen_keys][:remaining]

        return priority_bugs + backfill

    def get_bugs_by_keys(self, keys: list[str], batch_size: int = 100) -> list[Bug]:
        """Fetch bugs by their JIRA keys. Used for backfilling Neo4j data."""
        all_bugs: list[Bug] = []
        for i in range(0, len(keys), batch_size):
            batch = keys[i : i + batch_size]
            key_list = ", ".join(batch)
            jql = f"key IN ({key_list})"
            all_bugs.extend(self._search(jql, batch_size))
            logger.info("Backfill: fetched %d/%d bugs", len(all_bugs), len(keys))
        return all_bugs

    def _search(self, jql: str, max_results: int) -> list[Bug]:
        """Execute a JQL search with cursor-based pagination and return Bug objects.

        Atlassian's /rest/api/3/search/jql uses nextPageToken (not startAt).
        """
        url = f"{self._config.url}/rest/api/3/search/jql"
        logger.info("JIRA query: %s (max: %d)", jql, max_results)

        bugs = []
        page_size = min(max_results, 100)
        next_token = None

        while len(bugs) < max_results:
            params = {
                "jql": jql,
                "maxResults": page_size,
                "fields": "summary,description,status,priority,components,created",
            }
            if next_token:
                params["nextPageToken"] = next_token

            try:
                response = self._session.get(url, params=params, timeout=30)
                response.raise_for_status()
            except requests.RequestException as e:
                logger.error("JIRA query failed: %s", e)
                break

            data = response.json()
            issues = data.get("issues", [])
            if not issues:
                break

            for issue in issues:
                fields = issue["fields"]
                components = fields.get("components", [])
                component_names = tuple(c["name"] for c in components) if components else ("Unknown",)

                description = fields.get("description", "") or ""
                if isinstance(description, dict):
                    description = _extract_text_from_adf(description)

                bugs.append(
                    Bug(
                        key=issue["key"],
                        summary=fields.get("summary", ""),
                        description=description,
                        component=", ".join(component_names),
                        priority=fields.get("priority", {}).get("name", "Unknown"),
                        status=fields.get("status", {}).get("name", "Unknown"),
                        created=fields.get("created", ""),
                        url=f"{self._config.url}/browse/{issue['key']}",
                        all_components=component_names,
                    )
                )

            # Cursor-based pagination
            next_token = data.get("nextPageToken")
            is_last = data.get("isLast", True)

            if is_last or not next_token:
                break

        logger.info("Found %d bugs (unique)", len(bugs))
        return bugs[:max_results]
