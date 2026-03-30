"""Streamlit web dashboard for krkn-chaos-coordinator."""

import json
import sys
from pathlib import Path

import streamlit as st

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.filter.chaos_filter import filter_bugs, filter_bug
from src.knowledge.chromadb_store import ChromaStore, DocChunk
from src.knowledge.scenario_index import index_scenarios_from_repo
from src.knowledge.component_map import AGENT_COMPONENTS, get_all_agents
from src.coordinator.orchestrator import deduplicate_gaps
from src.agents.act import build_issue_title, build_issue_body
from src.models import (
    ActionType, Bug, Confidence,
    GapAnalysis, MatchResult, ScenarioMatch,
)


def load_bugs_from_json(path: str) -> list[Bug]:
    """Load bugs from saved JIRA JSON."""
    with open(path) as f:
        data = json.load(f)
    bugs = []
    for issue in data.get("issues", {}).get("nodes", []):
        fields = issue["fields"]
        components = fields.get("components", [])
        comp_name = components[0]["name"] if components else "Unknown"
        desc = fields.get("description", "") or ""
        bugs.append(Bug(
            key=issue["key"], summary=fields.get("summary", ""),
            description=desc, component=comp_name,
            priority=fields.get("priority", {}).get("name", "Unknown"),
            status=fields.get("status", {}).get("name", "Unknown"),
            created=fields.get("created", ""),
            url=f"https://redhat.atlassian.net/browse/{issue['key']}",
        ))
    return bugs


def run_pipeline(bugs: list[Bug], krkn_path: str) -> tuple:
    """Run the pipeline and return results."""
    relevant, skipped = filter_bugs(bugs)
    scenarios = index_scenarios_from_repo(Path(krkn_path))
    chroma = ChromaStore(persist_dir="/tmp/krkn_chroma_streamlit")
    chunks = [
        DocChunk(
            text=f"{s.scenario_type}: {s.file_path} ({s.description})",
            component=s.plugin_name, doc_type="scenario", source="krkn",
        )
        for s in scenarios
    ]
    chroma.add_scenario_docs(chunks)

    matched, unmatched = [], []
    for fr in relevant:
        bug = fr.bug
        query = f"{bug.component} {bug.summary}"
        chroma_results = chroma.search_scenarios(query, n_results=5)
        comp_lower = bug.component.lower()
        matching = [
            s for s in scenarios
            if comp_lower in s.name.lower()
            or comp_lower in s.scenario_type.lower()
            or any(kw in s.file_path.lower() for kw in comp_lower.split())
        ]
        if matching and chroma_results and chroma_results[0]["distance"] < 0.3:
            matched.append(ScenarioMatch(
                bug=bug, match_result=MatchResult.FULL_MATCH,
                matched_scenario=matching[0].file_path, matched_repo="krkn-chaos/krkn",
            ))
        elif matching:
            unmatched.append(ScenarioMatch(
                bug=bug, match_result=MatchResult.PARTIAL_MATCH,
                matched_scenario=matching[0].file_path, matched_repo="krkn-chaos/krkn",
            ))
        else:
            unmatched.append(ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH))

    gaps = []
    for match in unmatched:
        bug = match.bug
        score, reasons = 0, []
        if bug.description and len(bug.description) > 200:
            score += 20; reasons.append("Clear repro (+20)")
        if match.match_result == MatchResult.PARTIAL_MATCH:
            score += 25; reasons.append(f"Partial: {match.matched_scenario} (+25)")
        if any(kw in bug.summary.lower() for kw in [
            "timeout", "crash", "unavailable", "degraded", "unhealthy",
            "not cleared", "failure", "failed",
        ]):
            score += 20; reasons.append("Known failure mode (+20)")
        score += 10; reasons.append("Domain match (+10)")
        confidence = Confidence.HIGH if score >= 70 else Confidence.MEDIUM if score >= 40 else Confidence.LOW
        action = ActionType.DRAFT_PR if score >= 70 else ActionType.GITHUB_ISSUE
        modifications = [f"Extend {match.matched_scenario}"] if match.matched_scenario else []
        gaps.append(GapAnalysis(
            bug=bug, confidence_score=score, confidence_level=confidence,
            action_type=action, reasoning="; ".join(reasons),
            base_scenario=match.matched_scenario, modifications=modifications,
        ))

    return relevant, skipped, matched, unmatched, gaps, scenarios


# === STREAMLIT APP ===

st.set_page_config(page_title="krkn-chaos-coordinator", page_icon="🔥", layout="wide")

st.title("krkn-chaos-coordinator")
st.caption("AI-driven chaos test coverage expansion for OpenShift")

# Sidebar
with st.sidebar:
    st.header("Configuration")
    release = st.text_input("OCP Release", value="4.21")
    krkn_path = st.text_input("krkn Repo Path", value="/Users/sahil/krkn")
    jira_path = st.text_input("JIRA Data (JSON)", value="tests/fixtures/jira_etcd_bugs.json")

    st.divider()
    st.header("Agents")
    for agent in get_all_agents():
        display = agent.replace("_", " ").title()
        components = AGENT_COMPONENTS.get(agent, [])
        with st.expander(display):
            for c in components:
                st.text(f"  {c}")

    st.divider()
    run_button = st.button("Run Pipeline", type="primary", use_container_width=True)

# Main content
if run_button or st.session_state.get("has_run"):
    st.session_state["has_run"] = True

    if not Path(jira_path).exists():
        st.error(f"JIRA data file not found: {jira_path}")
        st.stop()

    bugs = load_bugs_from_json(jira_path)

    with st.status("Running pipeline...", expanded=True) as status:
        st.write(f"**DISCOVER:** Loading {len(bugs)} bugs from JIRA...")
        relevant, skipped, matched, unmatched, gaps, scenarios = run_pipeline(bugs, krkn_path)
        st.write(f"**FILTER:** {len(relevant)} relevant, {len(skipped)} skipped")
        st.write(f"**MAP:** {len(scenarios)} scenarios indexed, {len(matched)} matched, {len(unmatched)} unmatched")
        st.write(f"**ANALYZE:** {len(gaps)} gaps identified")
        status.update(label="Pipeline complete!", state="complete")

    # Metrics row
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Bugs Scanned", len(bugs))
    col2.metric("Chaos Relevant", len(relevant))
    col3.metric("Skipped", len(skipped))
    col4.metric("Gaps Found", len(gaps))
    prs = sum(1 for g in gaps if g.action_type == ActionType.DRAFT_PR)
    col5.metric("Draft PRs", prs)

    st.divider()

    # Tabs
    tab1, tab2, tab3, tab4 = st.tabs(["Approval Queue", "Filter Results", "Scenario Coverage", "Issue Previews"])

    with tab1:
        st.subheader("Approval Queue")
        if not gaps:
            st.success("No chaos test coverage gaps identified!")
        else:
            for i, gap in enumerate(gaps):
                level = gap.confidence_level.value.upper()
                color = "green" if gap.confidence_score >= 70 else "orange" if gap.confidence_score >= 40 else "red"
                action = "DRAFT PR" if gap.action_type == ActionType.DRAFT_PR else "ISSUE"

                with st.container(border=True):
                    c1, c2, c3 = st.columns([1, 6, 2])
                    with c1:
                        st.markdown(f"### #{i+1}")
                    with c2:
                        st.markdown(f"**[{gap.bug.key}]({gap.bug.url})**: {gap.bug.summary[:80]}")
                        st.caption(f"Component: {gap.bug.component} | Priority: {gap.bug.priority}")
                        st.caption(f"Reasoning: {gap.reasoning}")
                        if gap.base_scenario:
                            st.caption(f"Base: `{gap.base_scenario}`")
                    with c3:
                        st.markdown(f":{color}[**{level}** {gap.confidence_score}/100]")
                        st.markdown(f"**{action}**")
                        col_a, col_b = st.columns(2)
                        col_a.button("Approve", key=f"approve_{i}", type="primary")
                        col_b.button("Reject", key=f"reject_{i}")

    with tab2:
        st.subheader("Filter Results")

        st.markdown("#### Chaos Relevant")
        for r in relevant:
            st.markdown(f"- **{r.bug.key}**: {r.bug.summary[:70]}  \n"
                        f"  Mode: `{r.failure_mode}` | Inject: `{r.injection_method}`")

        st.markdown("#### Skipped (Not Chaos Relevant)")
        for s in skipped:
            st.markdown(f"- ~~{s.bug.key}~~: {s.bug.summary[:70]}  \n"
                        f"  Reason: {s.skip_reason[:80]}")

    with tab3:
        st.subheader(f"krkn Scenario Coverage ({len(scenarios)} scenarios)")
        scenario_data = [
            {"File": s.file_path, "Type": s.scenario_type, "Plugin": s.plugin_name}
            for s in scenarios
        ]
        st.dataframe(scenario_data, use_container_width=True)

    with tab4:
        st.subheader("Issue Previews")
        for gap in gaps:
            title = build_issue_title(gap)
            body = build_issue_body(gap, "control_plane")
            with st.expander(f"{gap.bug.key}: {gap.bug.summary[:60]}"):
                st.markdown(f"**Title:** {title}")
                st.divider()
                st.markdown(body)

else:
    st.info("Click **Run Pipeline** in the sidebar to start analyzing bugs.")

    # Show agent architecture
    st.subheader("Architecture")
    st.code("""
    Orchestrator
    ├── Upgrade & Lifecycle    (CVO, MCO, Installer)
    ├── Control Plane          (etcd, kube-apiserver, scheduler)
    ├── Node & Machine         (kubelet, Machine API, Cloud Compute)
    ├── Networking             (OVN-K, DNS, router, ingress)
    ├── Storage                (CSI, Image Registry)
    └── Operators & Platform   (OLM, Console, Auth, Monitoring)

    Pipeline: DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER
    """)
