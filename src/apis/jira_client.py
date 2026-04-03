"""JIRA REST API client for querying OCPBUGS."""

import logging
from dataclasses import dataclass

import requests

from src.models import Bug

logger = logging.getLogger(__name__)


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
        self, components: list[str], days: int = 14, max_results: int = 50
    ) -> list[Bug]:
        """Query OCPBUGS for recent bugs in the given components."""
        component_list = ", ".join(f'"{c}"' for c in components)
        jql = (
            f"project = OCPBUGS AND component IN ({component_list}) "
            f"AND created >= -{days}d ORDER BY created DESC"
        )
        return self._search(jql, max_results)

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
                component_name = components[0]["name"] if components else "Unknown"

                bugs.append(
                    Bug(
                        key=issue["key"],
                        summary=fields.get("summary", ""),
                        description=fields.get("description", "") or "",
                        component=component_name,
                        priority=fields.get("priority", {}).get("name", "Unknown"),
                        status=fields.get("status", {}).get("name", "Unknown"),
                        created=fields.get("created", ""),
                        url=f"{self._config.url}/browse/{issue['key']}",
                    )
                )

            # Cursor-based pagination
            next_token = data.get("nextPageToken")
            is_last = data.get("isLast", True)

            if is_last or not next_token:
                break

        logger.info("Found %d bugs (unique)", len(bugs))
        return bugs[:max_results]
