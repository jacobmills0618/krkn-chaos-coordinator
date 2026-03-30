"""Rich terminal UI for krkn-chaos-coordinator."""

import time
from enum import Enum

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskID
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from src.models import AgentResult, FilterResult, GapAnalysis


class AgentStatus(Enum):
    WAITING = "[dim]WAITING[/dim]"
    DISCOVERING = "[yellow]DISCOVER[/yellow]"
    FILTERING = "[yellow]FILTER[/yellow]"
    MAPPING = "[cyan]MAP[/cyan]"
    ANALYZING = "[blue]ANALYZE[/blue]"
    ACTING = "[magenta]ACT[/magenta]"
    REMEMBERING = "[magenta]REMEMBER[/magenta]"
    DONE = "[green]DONE[/green]"
    ERROR = "[red]ERROR[/red]"


class TerminalUI:
    """Rich terminal dashboard for the coordinator."""

    def __init__(self, release: str):
        self.console = Console()
        self.release = release
        self._agent_statuses: dict[str, AgentStatus] = {}
        self._agent_progress: dict[str, str] = {}
        self._feed: list[str] = []
        self._gaps_count = 0
        self._issues_count = 0
        self._prs_count = 0
        self._total_bugs = 0
        self._total_relevant = 0
        self._total_skipped = 0

    def init_agents(self, agent_names: list[str]) -> None:
        """Initialize agent tracking."""
        for name in agent_names:
            self._agent_statuses[name] = AgentStatus.WAITING
            self._agent_progress[name] = ""

    def update_agent(self, name: str, status: AgentStatus, progress: str = "") -> None:
        """Update an agent's status."""
        self._agent_statuses[name] = status
        self._agent_progress[name] = progress

    def add_feed(self, message: str) -> None:
        """Add a message to the live feed."""
        self._feed.append(message)
        if len(self._feed) > 15:
            self._feed = self._feed[-15:]

    def set_counts(
        self, total_bugs: int = 0, relevant: int = 0, skipped: int = 0,
        gaps: int = 0, issues: int = 0, prs: int = 0
    ) -> None:
        """Update summary counts."""
        self._total_bugs = total_bugs
        self._total_relevant = relevant
        self._total_skipped = skipped
        self._gaps_count = gaps
        self._issues_count = issues
        self._prs_count = prs

    def _build_header(self) -> Panel:
        """Build the header panel."""
        text = Text()
        text.append("krkn-chaos-coordinator", style="bold cyan")
        text.append(f"  |  Release: ", style="dim")
        text.append(self.release, style="bold green")
        return Panel(text, style="cyan")

    def _build_agents_panel(self) -> Panel:
        """Build the agents status tree."""
        tree = Tree("[bold]Orchestrator[/bold]")
        for name, status in self._agent_statuses.items():
            display_name = name.replace("_", " ").title()
            progress = self._agent_progress.get(name, "")
            label = f"{display_name}  {status.value}"
            if progress:
                label += f"  [dim]{progress}[/dim]"
            tree.add(label)
        return Panel(tree, title="[bold]Agents[/bold]", border_style="blue")

    def _build_feed_panel(self) -> Panel:
        """Build the live feed panel."""
        text = Text()
        for msg in self._feed:
            text.append(msg + "\n")
        return Panel(text, title="[bold]Live Feed[/bold]", border_style="yellow")

    def _build_stats_panel(self) -> Panel:
        """Build the stats panel."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("label", style="dim")
        table.add_column("value", style="bold")
        table.add_row("Bugs Scanned", str(self._total_bugs))
        table.add_row("Chaos Relevant", f"[green]{self._total_relevant}[/green]")
        table.add_row("Skipped", f"[dim]{self._total_skipped}[/dim]")
        table.add_row("Gaps Found", f"[yellow]{self._gaps_count}[/yellow]")
        table.add_row("Issues", f"[cyan]{self._issues_count}[/cyan]")
        table.add_row("Draft PRs", f"[magenta]{self._prs_count}[/magenta]")
        return Panel(table, title="[bold]Summary[/bold]", border_style="green")

    def build_layout(self) -> Layout:
        """Build the full dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(self._build_header(), name="header", size=3),
            Layout(name="body"),
            Layout(self._build_stats_panel(), name="footer", size=10),
        )
        layout["body"].split_row(
            Layout(self._build_agents_panel(), name="agents", ratio=1),
            Layout(self._build_feed_panel(), name="feed", ratio=2),
        )
        return layout

    def render_final_results(self, results: list[AgentResult], gaps: list[GapAnalysis]) -> None:
        """Render final results as a rich table."""
        self.console.print()

        # Summary table
        summary = Table(title="Run Summary", border_style="cyan")
        summary.add_column("Agent", style="bold")
        summary.add_column("Discovered", justify="right")
        summary.add_column("Skipped", justify="right")
        summary.add_column("Relevant", justify="right")
        summary.add_column("Matched", justify="right")
        summary.add_column("Gaps", justify="right", style="yellow")

        for r in results:
            discovered = len(r.bugs_discovered)
            skipped = len(r.bugs_filtered_out)
            relevant = discovered - skipped
            matched = len(r.bugs_matched)
            gap_count = len(r.gaps)
            summary.add_row(
                r.agent_name.replace("_", " ").title(),
                str(discovered), str(skipped), str(relevant),
                str(matched), str(gap_count),
            )

        self.console.print(summary)
        self.console.print()

        if not gaps:
            self.console.print("[green]No chaos test coverage gaps identified.[/green]")
            return

        # Approval queue table
        queue = Table(title="Approval Queue", border_style="yellow")
        queue.add_column("#", justify="right", style="dim")
        queue.add_column("Confidence", justify="center")
        queue.add_column("Action", justify="center")
        queue.add_column("Bug", style="bold")
        queue.add_column("Summary")
        queue.add_column("Base Scenario", style="dim")

        for i, gap in enumerate(gaps, 1):
            level = gap.confidence_level.value.upper()
            score = gap.confidence_score
            if score >= 70:
                conf_style = "[bold green]"
            elif score >= 40:
                conf_style = "[bold yellow]"
            else:
                conf_style = "[bold red]"

            action = "DRAFT PR" if gap.action_type.value == "draft_pr" else "ISSUE"
            action_style = "[magenta]" if action == "DRAFT PR" else "[cyan]"

            queue.add_row(
                str(i),
                f"{conf_style}{level} {score}/100[/]",
                f"{action_style}{action}[/]",
                gap.bug.key,
                gap.bug.summary[:60],
                gap.base_scenario or "—",
            )

        self.console.print(queue)
        self.console.print()

        # Issue previews
        for i, gap in enumerate(gaps, 1):
            self.console.print(
                Panel(
                    f"[bold]{gap.bug.key}[/bold]: {gap.bug.summary}\n\n"
                    f"[dim]Component:[/dim] {gap.bug.component}\n"
                    f"[dim]Reasoning:[/dim] {gap.reasoning}\n"
                    f"[dim]Base:[/dim] {gap.base_scenario or 'none'}\n"
                    f"[dim]Modifications:[/dim] {', '.join(gap.modifications) or 'none'}",
                    title=f"Gap {i}: [{gap.confidence_level.value.upper()}]",
                    border_style="yellow" if gap.confidence_score >= 70 else "dim",
                )
            )
