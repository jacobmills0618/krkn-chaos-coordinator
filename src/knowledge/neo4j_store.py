"""Neo4j Direct knowledge graph for the REMEMBER phase.

Uses the synchronous Neo4j driver. No LLM needed.
5ms per write instead of 30 seconds with Graphiti.
"""
from __future__ import annotations


import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

from neo4j import GraphDatabase

from src.apis.github_client import load_project_env
from src.models import AgentResult, Bug, FilterResult, GapAnalysis

logger = logging.getLogger(__name__)

NEO4J_CONTAINER = os.environ.get("NEO4J_CONTAINER", "neo4j-coordinator")

_PODMAN_CANDIDATES = (
    "podman",
    "/opt/podman/bin/podman",
    "/usr/local/bin/podman",
    "/opt/homebrew/bin/podman",
)
_DOCKER_CANDIDATES = ("docker", "/usr/local/bin/docker", "/opt/homebrew/bin/docker")


def _resolve_engine() -> str | None:
    """Return absolute path to podman or docker, including common macOS install paths."""
    for name in _PODMAN_CANDIDATES + _DOCKER_CANDIDATES:
        if os.path.isabs(name):
            if os.path.isfile(name) and os.access(name, os.X_OK):
                return name
        else:
            found = shutil.which(name)
            if found:
                return found
    return None


def restart_neo4j_container(container: str = NEO4J_CONTAINER) -> bool:
    """Restart (or start) Neo4j. For Podman, wake the machine first if needed."""
    engine = _resolve_engine()
    if not engine:
        logger.error("Cannot restart Neo4j: neither podman nor docker found")
        return False

    is_podman = os.path.basename(engine) == "podman"
    if is_podman:
        try:
            result = subprocess.run(
                [engine, "machine", "start"],
                capture_output=True, text=True, timeout=120,
            )
            # 0 = started; non-zero often means already running — continue either way
            if result.returncode == 0:
                logger.warning("Podman machine started via %s", engine)
            else:
                err = (result.stderr or result.stdout).strip()
                if err and "already" not in err.lower():
                    logger.warning("podman machine start: %s", err)
        except Exception as e:
            logger.warning("podman machine start error: %s", e)

    for action in ("restart", "start"):
        try:
            result = subprocess.run(
                [engine, action, container],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.warning("Neo4j container %s: %s via %s", container, action, engine)
                return True
            logger.warning(
                "%s %s %s failed: %s",
                engine, action, container, (result.stderr or result.stdout).strip(),
            )
        except Exception as e:
            logger.warning("%s %s %s error: %s", engine, action, container, e)
    return False


class Neo4jStore:
    """Direct Neo4j knowledge graph — sync driver, no LLM."""

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str | None = None,
    ):
        load_project_env()
        self._uri = os.environ.get("NEO4J_URI", uri)
        self._user = os.environ.get("NEO4J_USER", user)
        resolved_password = password or os.environ.get("NEO4J_PASSWORD")
        if not resolved_password:
            raise ValueError(
                "Neo4j password is required. Set the NEO4J_PASSWORD environment "
                "variable or pass password= explicitly."
            )
        self._password = resolved_password
        self._driver = None

    def connect(self) -> bool:
        """Connect to Neo4j and create schema."""
        self.close()
        try:
            self._driver = GraphDatabase.driver(
                self._uri, auth=(self._user, self._password)
            )
            self._driver.verify_connectivity()
            self._create_schema()
            logger.info("Neo4j connected at %s", self._uri)
            return True
        except (OSError, ConnectionError) as e:
            logger.error("Neo4j connection refused at %s: %s", self._uri, e)
            self.close()
            return False
        except Exception as e:
            logger.error("Neo4j connection failed (unexpected): %s: %s", type(e).__name__, e)
            self.close()
            return False

    def ensure_connected(self, retries: int = 3) -> bool:
        """Verify Bolt; restart the Neo4j container and reconnect if down."""
        for attempt in range(1, retries + 1):
            try:
                if self._driver is not None:
                    self._driver.verify_connectivity()
                    return True
            except Exception as e:
                logger.warning("Neo4j stale (attempt %d/%d): %s", attempt, retries, e)
                self.close()

            if self.connect():
                return True

            logger.warning(
                "Neo4j unreachable (attempt %d/%d) — restarting %s",
                attempt, retries, NEO4J_CONTAINER,
            )
            if restart_neo4j_container():
                time.sleep(5)
                if self.connect():
                    return True
            if attempt < retries:
                time.sleep(attempt)
        return False

    def _create_schema(self) -> None:
        queries = [
            "CREATE INDEX IF NOT EXISTS FOR (b:Bug) ON (b.key)",
            "CREATE INDEX IF NOT EXISTS FOR (c:Component) ON (c.name)",
            "CREATE INDEX IF NOT EXISTS FOR (g:Gap) ON (g.id)",
            "CREATE INDEX IF NOT EXISTS FOR (a:Action) ON (a.url)",
            "CREATE INDEX IF NOT EXISTS FOR (f:Finding) ON (f.id)",
            "CREATE INDEX IF NOT EXISTS FOR (r:Run) ON (r.id)",
            "CREATE INDEX IF NOT EXISTS FOR (m:RunMetrics) ON (m.created_at)",
        ]
        with self._driver.session() as session:
            for q in queries:
                try:
                    session.run(q)
                except Exception as e:
                    logger.warning("Index creation failed (%s): %s", q[:50], e)

    def record_run(self, result: AgentResult) -> str:
        """Create a Run node and link it to the agent."""
        timestamp = datetime.now(timezone.utc).isoformat()
        run_id = f"{result.agent_name}_{timestamp}"
        with self._driver.session() as session:
            session.run(
                """
                CREATE (r:Run {
                    id: $id, agent: $agent, timestamp: $ts,
                    bugs_discovered: $discovered, bugs_filtered: $filtered,
                    bugs_matched: $matched, gaps_found: $gaps,
                    filter_mode: $filter_mode
                })
                """,
                id=run_id, agent=result.agent_name, ts=timestamp,
                discovered=len(result.bugs_discovered),
                filtered=len(result.bugs_filtered_out),
                matched=len(result.bugs_matched),
                gaps=len(result.gaps),
                filter_mode=result.filter_mode,
            )
            session.run(
                """
                MERGE (a:Agent {name: $agent})
                WITH a
                MATCH (r:Run {id: $run_id})
                MERGE (a)-[:PERFORMED]->(r)
                """,
                agent=result.agent_name, run_id=run_id,
            )
        return run_id

    def remember_result(self, result: AgentResult) -> dict:
        """Store an agent run's results in the graph."""
        timestamp = datetime.now(timezone.utc).isoformat()
        new_bugs = 0
        new_gaps = 0
        domain_only = result.filter_mode == "domain"
        self.record_run(result)

        with self._driver.session() as session:
            for bug in result.bugs_discovered:
                if self._upsert_bug(session, bug, timestamp):
                    new_bugs += 1

            for fr in result.bugs_filtered_out:
                self._set_filter(session, fr, result.agent_name, domain_only, passed=False)
            for fr in result.bugs_passed_filter:
                self._set_filter(session, fr, result.agent_name, domain_only, passed=True)

            for gap in result.gaps:
                if self._upsert_gap(session, gap, result.agent_name, timestamp):
                    new_gaps += 1

        logger.info("Neo4j REMEMBER: %d new bugs, %d new gaps", new_bugs, new_gaps)
        return {"new_bugs": new_bugs, "new_gaps": new_gaps}

    def persist_bug(self, bug: Bug) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._driver.session() as session:
            self._upsert_bug(session, bug, ts)

    def persist_filter(
        self, fr: FilterResult, agent: str, *, filter_mode: str = "chaos",
    ) -> None:
        with self._driver.session() as session:
            self._upsert_bug(session, fr.bug, datetime.now(timezone.utc).isoformat())
            self._set_filter(
                session, fr, agent, domain_only=(filter_mode == "domain"),
                passed=fr.chaos_relevant,
            )

    def persist_gap(self, gap: GapAnalysis, agent: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._driver.session() as session:
            self._upsert_bug(session, gap.bug, ts)
            self._upsert_gap(session, gap, agent, ts)

    def _upsert_bug(self, session, bug: Bug, timestamp: str) -> bool:
        desc = (bug.description or "")[:2000]
        all_comps = list(bug.all_components) if bug.all_components else [bug.component]
        r = session.run(
            """
            MERGE (b:Bug {key: $key})
            ON CREATE SET b.first_seen = $ts, b.created = $created
            SET b.last_seen = $ts, b.summary = $summary,
                b.priority = $priority, b.status = $status,
                b.url = $url, b.description = $description,
                b.all_components = $all_components,
                b.fixed_in_release = $fixed_in_release,
                b.fix_image = $fix_image,
                b.fix_commits = $fix_commits
            RETURN b.first_seen = $ts AS is_new
            """,
            key=bug.key, summary=bug.summary,
            priority=bug.priority, status=bug.status,
            created=bug.created, url=bug.url, ts=timestamp,
            description=desc, all_components=all_comps,
            fixed_in_release=bug.fixed_in_release,
            fix_image=bug.fix_image,
            fix_commits=list(bug.fix_commits) if bug.fix_commits else [],
        )
        record = r.single()
        for comp_name in all_comps:
            session.run(
                """
                MERGE (c:Component {name: $component})
                MERGE (b:Bug {key: $key})
                MERGE (c)-[:HAS_BUG]->(b)
                """,
                component=comp_name, key=bug.key,
            )
        return bool(record and record["is_new"])

    def _set_filter(
        self, session, fr: FilterResult, agent: str, domain_only: bool, *, passed: bool,
    ) -> None:
        if domain_only:
            session.run(
                """
                MERGE (b:Bug {key: $key})
                SET b.virt_relevant = $passed,
                    b.skip_reason = $reason,
                    b.last_filter_agent = $agent,
                    b.last_filter_mode = 'domain',
                    b.last_filter_outcome = $outcome,
                    b.failure_mode = CASE WHEN $passed THEN $failure_mode ELSE b.failure_mode END
                """,
                key=fr.bug.key,
                passed=passed,
                reason=None if passed else fr.skip_reason,
                agent=agent,
                outcome="pass" if passed else "skip",
                failure_mode=fr.failure_mode,
            )
        else:
            session.run(
                """
                MERGE (b:Bug {key: $key})
                SET b.chaos_relevant = $passed,
                    b.skip_reason = $reason,
                    b.last_filter_agent = $agent,
                    b.last_filter_mode = 'chaos',
                    b.last_filter_outcome = $outcome,
                    b.failure_mode = CASE WHEN $passed THEN $failure_mode ELSE b.failure_mode END
                """,
                key=fr.bug.key,
                passed=passed,
                reason=None if passed else fr.skip_reason,
                agent=agent,
                outcome="pass" if passed else "skip",
                failure_mode=fr.failure_mode,
            )

    def _upsert_gap(self, session, gap: GapAnalysis, agent: str, timestamp: str) -> bool:
        gap_id = f"{gap.bug.key}_{agent}"
        r = session.run(
            """
            MATCH (b:Bug {key: $bug_key})
            MERGE (g:Gap {id: $gap_id})
            ON CREATE SET g.confidence = $confidence,
                g.confidence_level = $level,
                g.action_type = $action_type,
                g.reasoning = $reasoning,
                g.base_scenario = $base_scenario,
                g.status = 'open',
                g.opened_at = $ts,
                g.agent = $agent
            SET g.confidence = $confidence,
                g.confidence_level = $level,
                g.action_type = $action_type,
                g.reasoning = $reasoning,
                g.base_scenario = $base_scenario,
                g.agent = $agent
            MERGE (b)-[:HAS_GAP]->(g)
            RETURN g.opened_at = $ts AS is_new
            """,
            bug_key=gap.bug.key, gap_id=gap_id,
            confidence=gap.confidence_score,
            level=gap.confidence_level.value,
            action_type=gap.action_type.value,
            reasoning=gap.reasoning,
            base_scenario=gap.base_scenario,
            ts=timestamp, agent=agent,
        )
        record = r.single()
        return bool(record and record["is_new"])

    # Sync alias for pipeline compatibility
    remember_result_sync = remember_result

    def mark_gap_resolved(self, bug_key: str, issue_url: str) -> None:
        with self._driver.session() as session:
            session.run(
                """
                MATCH (b:Bug {key: $key})-[:HAS_GAP]->(g:Gap {status: 'open'})
                SET g.status = 'resolved', g.resolved_at = $ts
                CREATE (a:Action {type: 'issue', url: $url, created_at: $ts})
                MERGE (g)-[:RESOLVED_BY]->(a)
                """,
                key=bug_key, url=issue_url,
                ts=datetime.now(timezone.utc).isoformat(),
            )

    mark_gap_resolved_sync = mark_gap_resolved

    def add_finding(self, agent_name: str, finding: str) -> None:
        with self._driver.session() as session:
            session.run(
                """
                MERGE (a:Agent {name: $agent})
                WITH a
                CREATE (f:Finding {
                    id: $id, text: $finding, created_at: $ts
                })
                MERGE (a)-[:LEARNED]->(f)
                """,
                agent=agent_name, finding=finding,
                id=f"{agent_name}_{datetime.now(timezone.utc).isoformat()}",
                ts=datetime.now(timezone.utc).isoformat(),
            )

    def is_bug_analyzed(self, bug_key: str) -> bool:
        with self._driver.session() as session:
            r = session.run("MATCH (b:Bug {key: $key}) RETURN b.key AS key", key=bug_key)
            return r.single() is not None

    def get_analyzed_bug_keys(self) -> set[str]:
        with self._driver.session() as session:
            r = session.run("MATCH (b:Bug) RETURN b.key AS key")
            return {record["key"] for record in r}

    # Sync alias
    get_analyzed_bug_keys_sync = get_analyzed_bug_keys

    def get_chaos_relevant_bugs_by_component(self) -> list[dict]:
        """Count Neo4j bugs with chaos_relevant=true, grouped by JIRA component."""
        return self._count_bugs_by_component("chaos_relevant")

    def get_chaos_relevant_bugs_by_filter_agent(self) -> list[dict]:
        """Count Neo4j chaos_relevant bugs grouped by last_filter_agent."""
        return self._count_bugs_by_filter_agent("chaos_relevant")

    def count_chaos_relevant_bugs(self) -> int:
        """Total bugs in Neo4j marked chaos_relevant=true."""
        return self._count_bugs_by_flag("chaos_relevant")

    def get_virt_relevant_bugs_by_component(self) -> list[dict]:
        """Count Neo4j bugs with virt_relevant=true, grouped by JIRA component.

        Set by --domain-filter-only (ocp-virt keywords). Distinct from chaos_relevant.
        """
        return self._count_bugs_by_component("virt_relevant")

    def get_virt_relevant_bugs_by_filter_agent(self) -> list[dict]:
        """Count Neo4j virt_relevant bugs grouped by last_filter_agent."""
        return self._count_bugs_by_filter_agent("virt_relevant")

    def count_virt_relevant_bugs(self) -> int:
        """Total bugs in Neo4j marked virt_relevant=true (domain filter)."""
        return self._count_bugs_by_flag("virt_relevant")

    def _count_bugs_by_flag(self, flag: str) -> int:
        if flag not in ("chaos_relevant", "virt_relevant"):
            raise ValueError(f"Unsupported bug flag: {flag}")
        with self._driver.session() as session:
            r = session.run(
                f"""
                MATCH (b:Bug)
                WHERE b.{flag} = true
                RETURN count(b) AS total
                """
            )
            record = r.single()
            return int(record["total"]) if record else 0

    def _count_bugs_by_component(self, flag: str) -> list[dict]:
        if flag not in ("chaos_relevant", "virt_relevant"):
            raise ValueError(f"Unsupported bug flag: {flag}")
        with self._driver.session() as session:
            r = session.run(
                f"""
                MATCH (c:Component)-[:HAS_BUG]->(b:Bug)
                WHERE b.{flag} = true
                RETURN c.name AS component, count(DISTINCT b) AS bugs
                ORDER BY bugs DESC
                """
            )
            return [dict(record) for record in r]

    def _count_bugs_by_filter_agent(self, flag: str) -> list[dict]:
        if flag not in ("chaos_relevant", "virt_relevant"):
            raise ValueError(f"Unsupported bug flag: {flag}")
        with self._driver.session() as session:
            r = session.run(
                f"""
                MATCH (b:Bug)
                WHERE b.{flag} = true
                RETURN coalesce(b.last_filter_agent, 'unknown') AS filter_agent,
                       count(b) AS bugs
                ORDER BY bugs DESC
                """
            )
            return [dict(record) for record in r]

    def get_open_gaps(self) -> list[dict]:
        with self._driver.session() as session:
            r = session.run(
                """
                MATCH (b:Bug)-[:HAS_GAP]->(g:Gap {status: 'open'})
                RETURN b.key AS bug_key, b.summary AS summary,
                       g.confidence AS confidence, g.reasoning AS reasoning,
                       g.opened_at AS opened_at
                ORDER BY g.confidence DESC
                """
            )
            return [dict(record) for record in r]

    get_open_gaps_sync = get_open_gaps

    def get_component_gap_counts(self) -> list[dict]:
        with self._driver.session() as session:
            r = session.run(
                """
                MATCH (c:Component)-[:HAS_BUG]->(b)-[:HAS_GAP]->(g)
                RETURN c.name AS component, count(g) AS gaps,
                       sum(CASE WHEN g.status = 'open' THEN 1 ELSE 0 END) AS open_gaps,
                       sum(CASE WHEN g.status = 'resolved' THEN 1 ELSE 0 END) AS resolved_gaps
                ORDER BY gaps DESC
                """
            )
            return [dict(record) for record in r]

    get_component_gap_counts_sync = get_component_gap_counts

    def get_similar_resolved_bugs(self, component: str) -> list[dict]:
        with self._driver.session() as session:
            r = session.run(
                """
                MATCH (c:Component {name: $component})-[:HAS_BUG]->(b)
                      -[:HAS_GAP]->(g {status: 'resolved'})-[:RESOLVED_BY]->(a)
                RETURN b.key AS bug_key, b.summary AS summary,
                       a.url AS issue_url, g.reasoning AS reasoning
                """,
                component=component,
            )
            return [dict(record) for record in r]

    def get_run_history(self, limit: int = 20) -> list[dict]:
        with self._driver.session() as session:
            r = session.run(
                """
                MATCH (r:Run)
                RETURN r.agent AS agent, r.timestamp AS timestamp,
                       r.bugs_discovered AS discovered, r.gaps_found AS gaps
                ORDER BY r.timestamp DESC
                LIMIT $limit
                """,
                limit=limit,
            )
            return [dict(record) for record in r]

    RESOLVED_STATUSES = frozenset({
        "Closed", "Verified", "Release Pending", "ON_QA", "MODIFIED",
    })

    def update_bug_statuses(self, bugs: list) -> dict:
        """Update status/priority for known bugs and close gaps for resolved bugs.

        Called during DISCOVER for bugs already in Neo4j. Zero LLM cost.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        updated = 0
        gaps_closed = 0

        with self._driver.session() as session:
            for bug in bugs:
                desc = (bug.description or "")[:2000]
                all_comps = list(bug.all_components) if bug.all_components else [bug.component]

                session.run(
                    """
                    MATCH (b:Bug {key: $key})
                    SET b.status = $status, b.priority = $priority,
                        b.last_seen = $ts, b.description = $description,
                        b.all_components = $all_components
                    """,
                    key=bug.key, status=bug.status, priority=bug.priority,
                    ts=timestamp, description=desc, all_components=all_comps,
                )
                updated += 1

                # Close open gaps if bug is resolved
                if bug.status in self.RESOLVED_STATUSES:
                    r = session.run(
                        """
                        MATCH (b:Bug {key: $key})-[:HAS_GAP]->(g:Gap {status: 'open'})
                        SET g.status = 'resolved_upstream',
                            g.resolved_at = $ts,
                            g.resolve_reason = 'Bug resolved in JIRA'
                        RETURN count(g) AS closed
                        """,
                        key=bug.key, ts=timestamp,
                    )
                    record = r.single()
                    if record and record["closed"] > 0:
                        gaps_closed += record["closed"]
                        logger.info(
                            "Gap auto-closed: %s resolved in JIRA (%s)",
                            bug.key, bug.status,
                        )

        logger.info("Status update: %d bugs updated, %d gaps auto-closed", updated, gaps_closed)
        return {"updated": updated, "gaps_closed": gaps_closed}

    def get_bugs_missing_description(self) -> list[str]:
        """Get bug keys that have no description stored."""
        with self._driver.session() as session:
            r = session.run(
                """
                MATCH (b:Bug)
                WHERE b.description IS NULL OR b.all_components IS NULL
                RETURN b.key AS key
                """
            )
            return [record["key"] for record in r]

    def backfill_bugs(self, bugs: list) -> dict:
        """Update existing Bug nodes with fresh data from JIRA.

        Used to fill in description and all_components for bugs
        that were stored before those fields were tracked.
        """
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat()
        updated = 0

        with self._driver.session() as session:
            for bug in bugs:
                desc = (bug.description or "")[:2000]
                all_comps = list(bug.all_components) if bug.all_components else [bug.component]

                session.run(
                    """
                    MATCH (b:Bug {key: $key})
                    SET b.summary = $summary, b.description = $description,
                        b.all_components = $all_components,
                        b.priority = $priority, b.status = $status,
                        b.last_seen = $ts
                    """,
                    key=bug.key, summary=bug.summary,
                    description=desc, all_components=all_comps,
                    priority=bug.priority, status=bug.status, ts=timestamp,
                )

                # Ensure component relationships exist for all components
                for comp_name in all_comps:
                    session.run(
                        """
                        MERGE (c:Component {name: $component})
                        MERGE (b:Bug {key: $key})
                        MERGE (c)-[:HAS_BUG]->(b)
                        """,
                        component=comp_name, key=bug.key,
                    )
                updated += 1

        logger.info("Backfill: updated %d bugs", updated)
        return {"updated": updated}

    def store_run_metrics(self, metrics: dict) -> None:
        """Store run metrics linked to the most recent Run node for this agent."""
        with self._driver.session() as session:
            session.run(
                """
                MATCH (r:Run)
                WHERE r.agent = $agent
                WITH r ORDER BY r.timestamp DESC LIMIT 1
                CREATE (m:RunMetrics {
                    bugs_processed: $bugs_processed,
                    bugs_succeeded: $bugs_succeeded,
                    filter_retries: $filter_retries,
                    filter_escalations: $filter_escalations,
                    map_fallbacks: $map_fallbacks,
                    analyze_retries: $analyze_retries,
                    total_input_tokens: $total_input_tokens,
                    total_output_tokens: $total_output_tokens,
                    keyword_filter_hits: $keyword_filter_hits,
                    semantic_cache_hits: $semantic_cache_hits,
                    llm_filter_calls: $llm_filter_calls,
                    llm_map_calls: $llm_map_calls,
                    llm_analyze_calls: $llm_analyze_calls,
                    filter_duration_sec: $filter_duration,
                    map_duration_sec: $map_duration,
                    analyze_duration_sec: $analyze_duration,
                    created_at: $ts
                })
                MERGE (r)-[:HAS_METRICS]->(m)
                """,
                agent=metrics.get("agent", "unknown"),
                bugs_processed=metrics.get("bugs_processed", 0),
                bugs_succeeded=metrics.get("bugs_succeeded", 0),
                filter_retries=metrics.get("filter_retries", 0),
                filter_escalations=metrics.get("filter_escalations", 0),
                map_fallbacks=metrics.get("map_fallbacks", 0),
                analyze_retries=metrics.get("analyze_retries", 0),
                total_input_tokens=metrics.get("total_input_tokens", 0),
                total_output_tokens=metrics.get("total_output_tokens", 0),
                keyword_filter_hits=metrics.get("keyword_filter_hits", 0),
                semantic_cache_hits=metrics.get("semantic_cache_hits", 0),
                llm_filter_calls=metrics.get("llm_filter_calls", 0),
                llm_map_calls=metrics.get("llm_map_calls", 0),
                llm_analyze_calls=metrics.get("llm_analyze_calls", 0),
                filter_duration=metrics.get("filter_duration_sec", 0.0),
                map_duration=metrics.get("map_duration_sec", 0.0),
                analyze_duration=metrics.get("analyze_duration_sec", 0.0),
                ts=datetime.now(timezone.utc).isoformat(),
            )
        logger.info("Stored RunMetrics for agent %s", metrics.get("agent", "unknown"))

    def get_metrics_history(self, limit: int = 20) -> list[dict]:
        """Get recent run metrics for trend analysis."""
        with self._driver.session() as session:
            r = session.run(
                """
                MATCH (r:Run)-[:HAS_METRICS]->(m:RunMetrics)
                RETURN r.agent AS agent, r.timestamp AS run_timestamp,
                       m.bugs_processed AS bugs_processed,
                       m.filter_escalations AS escalations,
                       m.total_input_tokens AS input_tokens,
                       m.keyword_filter_hits AS keyword_hits,
                       m.semantic_cache_hits AS cache_hits
                ORDER BY m.created_at DESC
                LIMIT $limit
                """,
                limit=limit,
            )
            return [dict(record) for record in r]

    def close(self) -> None:
        if self._driver:
            try:
                self._driver.close()
            except Exception as e:
                logger.debug("Neo4j driver close error: %s", e)
            self._driver = None
