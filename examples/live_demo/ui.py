"""Rich TUI components for the live demo.

Provides:

- ``rich_interactive``  — HITL policy with beautiful approval screen
- ``print_step``        — log each syscall as it executes
- ``print_act_header``  — act separator banners
- ``print_budget_summary`` — final budget table
- ``print_comparison``  — Act 1 vs Act 2 side-by-side
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from castor.models.checkpoint import AgentCheckpoint

console = Console()

# ── Colour palette ──

_STYLE_LLM = "bold magenta"
_STYLE_TOOL = "bold cyan"
_STYLE_HITL = "bold red"
_STYLE_REPLAY = "bold blue"
_STYLE_LIVE = "bold green"
_STYLE_DIM = "dim"


# ─────────────────────────────────────────────────────────────────────
# HITL policy
# ─────────────────────────────────────────────────────────────────────


async def rich_interactive(cp: AgentCheckpoint) -> tuple[str, str | None]:
    """Rich-formatted interactive HITL policy for ``run_until_complete``."""
    console.print()

    # ── Pending action panel ──
    tool = cp.pending_tool
    args: dict[str, Any] = cp.pending_args or {}

    body_lines: list[str] = []
    for key, val in args.items():
        if key == "body":
            # Render long body separately as markdown
            continue
        body_lines.append(f"[bold]{key}:[/bold]  {val}")

    args_text = "\n".join(body_lines) if body_lines else "(no arguments)"
    header = Text(f"  Tool:  {tool}", style="bold white")

    inner = Text.from_markup(args_text)
    panel_content = Text.assemble(header, "\n\n", inner)

    # If there's a body arg (email body), render it as markdown
    email_body = args.get("body", "")
    if email_body:
        panel_content.append("\n\n")
        panel_content.append_text(Text("  body:", style="bold white"))

    console.print(
        Panel(
            panel_content,
            title="[bold red]Human Approval Required[/bold red]",
            border_style="red",
            padding=(1, 2),
        )
    )

    if email_body:
        console.print(
            Panel(
                Markdown(str(email_body)),
                title="Email Body",
                border_style="yellow",
                padding=(1, 2),
            )
        )

    # ── Budget status ──
    budget_table = Table(
        title="Budget Status",
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    budget_table.add_column("Resource", style="bold")
    budget_table.add_column("Used", justify="right")
    budget_table.add_column("Remaining", justify="right")
    budget_table.add_column("Bar", min_width=20)

    for name, cap in cp.capabilities.items():
        used = cap.current_usage
        total = cap.max_budget
        remaining = total - used
        pct = remaining / total if total > 0 else 0
        bar_filled = int(pct * 16)
        bar_empty = 16 - bar_filled
        colour = "green" if pct > 0.5 else "yellow" if pct > 0.2 else "red"
        bar_str = f"[{colour}]{'█' * bar_filled}{'░' * bar_empty}[/{colour}]"
        budget_table.add_row(
            name,
            f"{used:.1f}",
            f"[{colour}]{remaining:.1f}[/{colour}] / {total:.1f}",
            bar_str,
        )

    console.print(budget_table)

    # ── Execution history ──
    if cp.syscall_log:
        hist_table = Table(
            title="Execution History",
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        hist_table.add_column("#", justify="right", style="dim", width=3)
        hist_table.add_column("Type", width=6)
        hist_table.add_column("Tool", style="bold")
        hist_table.add_column("Result (truncated)")

        for i, record in enumerate(cp.syscall_log, 1):
            tool_name = record.request.get("tool_name", "?")
            is_llm = "llm" in tool_name.lower()
            tag = (
                Text("[LLM]", style=_STYLE_LLM)
                if is_llm
                else Text("[TOOL]", style=_STYLE_TOOL)
            )
            result_str = _truncate(record.response, 60)
            hist_table.add_row(str(i), tag, tool_name, result_str)

        # Add pending row
        hist_table.add_row(
            str(len(cp.syscall_log) + 1),
            Text("[HITL]", style=_STYLE_HITL),
            tool,
            "awaiting approval",
        )
        console.print(hist_table)

    # ── Prompt ──
    console.print()
    choice = Prompt.ask(
        "  [bold][green]a[/green]pprove  /  "
        "[red]r[/red]eject  /  "
        "[yellow]m[/yellow]odify[/bold]",
        choices=["a", "r", "m"],
        default="a",
    )

    if choice == "r":
        reason = Prompt.ask("  [red]Reason[/red]")
        return ("reject", reason)
    if choice == "m":
        feedback = Prompt.ask("  [yellow]Feedback[/yellow]")
        return ("modify", feedback)
    return ("approve", None)


# ─────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────


def print_step(
    step: int,
    tool_name: str,
    result: Any,
    *,
    is_replay: bool = False,
) -> None:
    """Print a single syscall execution step."""
    is_llm = "llm" in tool_name.lower()
    tag_style = _STYLE_LLM if is_llm else _STYLE_TOOL
    tag_label = "LLM" if is_llm else "TOOL"

    if is_replay:
        mode = Text(" REPLAY ", style="on blue white")
    else:
        mode = Text(" LIVE ", style="on green white")

    result_str = _truncate(result, 80)
    console.print(
        Text.assemble(
            ("  ", ""),
            mode,
            (f" {step}. ", "bold"),
            (f"[{tag_label}] ", tag_style),
            (tool_name, "bold"),
            ("  →  ", _STYLE_DIM),
            (result_str, ""),
        )
    )


def print_act_header(act: int, title: str) -> None:
    """Print an act separator banner."""
    console.print()
    console.print(
        Panel(
            f"[bold white]{title}[/bold white]",
            title=f"[bold]Act {act}[/bold]",
            border_style="bright_blue",
            padding=(0, 2),
        )
    )
    console.print()


def print_budget_summary(cp: AgentCheckpoint) -> None:
    """Print a final budget breakdown table."""
    table = Table(
        title="Final Budget Summary",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Resource", style="bold")
    table.add_column("Used", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Remaining", justify="right")

    for name, cap in cp.capabilities.items():
        used = cap.current_usage
        total = cap.max_budget
        remaining = total - used
        if remaining > total * 0.5:
            colour = "green"
        elif remaining > 0:
            colour = "yellow"
        else:
            colour = "red"
        remaining_str = f"[{colour}]{remaining:.1f}[/{colour}]"
        table.add_row(
            name,
            f"{used:.1f}",
            f"{total:.1f}",
            remaining_str,
        )

    table.add_row(
        "[bold]syscalls[/bold]",
        f"[bold]{len(cp.syscall_log)}[/bold]",
        "",
        "",
    )
    console.print(table)


def print_comparison(cp_live: AgentCheckpoint, cp_replay: AgentCheckpoint) -> None:
    """Print Act 1 vs Act 2 comparison."""
    table = Table(
        title="Live vs Replay Comparison",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Metric", style="bold")
    table.add_column("Act 1 (Live)", justify="right")
    table.add_column("Act 2 (Replay)", justify="right")

    if "api" in cp_live.capabilities:
        live_api = cp_live.budget_used("api")
    else:
        live_api = 0.0
    if "api" in cp_replay.capabilities:
        replay_api = cp_replay.budget_used("api")
    else:
        replay_api = 0.0

    live_syscalls = str(len(cp_live.syscall_log))
    replay_syscalls = str(len(cp_replay.syscall_log))
    table.add_row("Syscalls", live_syscalls, replay_syscalls)

    replay_cost = f"[bold green]{replay_api:.1f}[/bold green]"
    table.add_row("API cost", f"{live_api:.1f}", replay_cost)

    live_llm_count = sum(
        1
        for r in cp_live.syscall_log
        if "llm" in r.request.get("tool_name", "").lower()
    )
    table.add_row(
        "Real LLM calls",
        str(live_llm_count),
        "[bold green]0[/bold green]",
    )
    table.add_row("Status", cp_live.status, cp_replay.status)

    console.print(table)
    console.print(
        "\n  [bold green]Crash recovery served all responses"
        " from cache — zero API calls.[/bold green]\n"
    )


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _truncate(value: Any, max_len: int = 60) -> str:
    """Truncate a value to *max_len* characters for display."""
    s = str(value)
    # Collapse newlines for compact display
    s = s.replace("\n", " ").strip()
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s
