import re
from decimal import Decimal

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from retail_agent.models import Report


def executive_text(text: str, evidence_ids: tuple[str, ...]) -> str:
    def replace(match: re.Match[str]) -> str:
        metric, raw, source = match.groups()
        if source not in evidence_ids:
            return match.group(0)
        number = Decimal(raw)
        return (
            format(number, ",.2f")
            if metric in {"revenue", "average_order_value"}
            else format(number, ",f")
        )

    return re.sub(
        r"\b([a-z_]+)=(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?) \[(q\d+), row \d+\]",
        replace,
        text,
    )


def render_report(console: Console, report: Report, *, show_details: bool = True) -> None:
    def display(text: str) -> str:
        return text if show_details else executive_text(text, report.evidence_ids)

    console.print()
    console.print(
        Panel(Text(display(report.summary)), title=Text(display(report.title)), border_style="cyan")
    )
    if report.scope:
        console.print(Text(report.scope, style="dim"))
    for label, items in [
        ("Findings", report.findings),
        ("Actions", report.actions),
        ("Limitations", report.limitations),
        ("Definitions", report.definitions),
    ]:
        if label == "Definitions" and not show_details:
            continue
        if items:
            console.print(Text(f"\n{label}:", style="bold"))
            for item in items:
                console.print(Text("• " + display(item)))
    if not show_details and (report.definitions or report.evidence_ids):
        console.print(Text("\nUse /details for evidence IDs and query definitions.", style="dim"))
    if show_details and report.evidence_ids:
        console.print(Text("Evidence: " + ", ".join(report.evidence_ids), style="dim"))
    console.print()
