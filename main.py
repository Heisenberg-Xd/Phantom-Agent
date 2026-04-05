"""
Phantom — Autonomous QA Agent
CLI entry point with Rich terminal output.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Force UTF-8 output on Windows to support emoji in Rich output
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import typer
from dotenv import load_dotenv

load_dotenv()

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ── logging setup ───────────────────────────────────────────────────────────
LOG_FILE = Path("phantom.log")

# Strip any existing handlers to prevent console leakage
root_logger = logging.getLogger()
root_logger.handlers.clear()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("phantom")

# ── Typer app ────────────────────────────────────────────────────────────────
app = typer.Typer(
    name="phantom",
    help="👻 Phantom — Autonomous QA Agent",
    add_completion=False,
)
console = Console()


# ── Rich helpers ─────────────────────────────────────────────────────────────

SEVERITY_COLORS = {
    "critical": "bold red",
    "high": "bold yellow",
    "medium": "bold blue",
    "low": "white",
}

SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "⚪",
}


def print_banner():
    console.print()
    console.print(
        Panel.fit(
            "[bold magenta]👻  P H A N T O M[/bold magenta]\n"
            "[dim]Autonomous QA Agent — powered by Claude[/dim]",
            border_style="magenta",
            padding=(1, 4),
        )
    )
    console.print()


def make_bug_table(bug_reports: list[dict]) -> Table:
    table = Table(
        title="🐛 Bugs Found",
        box=box.ROUNDED,
        border_style="bright_black",
        show_lines=True,
        expand=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Severity", width=12)
    table.add_column("Title", min_width=30)
    table.add_column("Category", width=14)
    table.add_column("Page", min_width=20, no_wrap=False)
    table.add_column("Status", width=12)

    for i, report in enumerate(bug_reports, 1):
        sev = report.get("severity", "low").lower()
        color = SEVERITY_COLORS.get(sev, "white")
        emoji = SEVERITY_EMOJI.get(sev, "⚪")
        status = report.get("status", "open")
        status_style = {
            "open": "bright_red",
            "fixed": "green",
            "regression": "bold yellow",
        }.get(status, "white")

        table.add_row(
            str(i),
            Text(f"{emoji} {sev.capitalize()}", style=color),
            report.get("title", "Unknown")[:60],
            report.get("category", "unknown"),
            report.get("affected_url", "")[:45],
            Text(status, style=status_style),
        )

    return table


def make_stats_panel(
    bugs_found: int,
    journeys_tested: int,
    pages_visited: int,
    coverage_pct: float,
    duration: float,
) -> Panel:
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_column(justify="center")
    grid.add_column(justify="center")

    grid.add_row(
        f"[bold red]{bugs_found}[/bold red]\n[dim]Bugs Found[/dim]",
        f"[bold cyan]{journeys_tested}[/bold cyan]\n[dim]Journeys[/dim]",
        f"[bold green]{pages_visited}[/bold green]\n[dim]Pages Visited[/dim]",
        f"[bold yellow]{coverage_pct:.0f}%[/bold yellow]\n[dim]Coverage[/dim]",
        f"[bold magenta]{duration:.0f}s[/bold magenta]\n[dim]Duration[/dim]",
    )
    return Panel(grid, title="📊 Scan Summary", border_style="bright_blue", padding=(1, 2))


def cleanup_old_scans(project_dir: Path, max_scans: int = 10):
    """Keep only the latest max_scans folders inside project_dir."""
    if not project_dir.exists():
        return
    scans = [d for d in project_dir.iterdir() if d.is_dir()]
    scans.sort(key=lambda d: d.name)
    if len(scans) > max_scans:
        for old_scan in scans[:-max_scans]:
            try:
                shutil.rmtree(old_scan)
            except Exception as e:
                logger.error(f"Failed to delete old scan folder {old_scan}: {e}")

# ── Main scan command ─────────────────────────────────────────────────────────

@app.command()
def scan(
    url: str = typer.Option(..., "--url", "-u", help="Target URL to scan"),
    description: str = typer.Option(..., "--description", "-d", help="Plain-English app description"),
    project_name: str = typer.Option(None, "--name", help="Project name for reports folder"),
    deep: bool = typer.Option(False, "--deep", help="Run full scan instead of fast"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run browser headlessly"),
    skip_explorer: bool = typer.Option(False, "--skip-explorer", help="Skip adversarial exploration"),
):
    """
    👻 Run Phantom QA scan on a web application.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        console.print("[bold red]❌ GEMINI_API_KEY environment variable not set![/bold red]")
        raise typer.Exit(1)

    max_pages = 25 if deep else 10
    if not project_name:
        project_name = urlparse(url).netloc.replace(":", "_") or "unnamed"
        
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    reports_dir = Path("reports") / project_name / timestamp
    reports_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_scans(Path("reports") / project_name, max_scans=10)

    print_banner()
    console.print(f"[bold]🎯 Target:[/bold] {url}")
    console.print(f"[bold]📋 Description:[/bold] {description}")
    console.print(f"[bold]📁 Output:[/bold] {reports_dir}")
    console.print()

    asyncio.run(_run_scan(project_name, url, description, max_pages, headless, skip_explorer, reports_dir))


async def _run_scan(
    project_name: str,
    url: str,
    description: str,
    max_pages: int,
    headless: bool,
    skip_explorer: bool,
    reports_dir: Path,
):
    from browser.runner import PhantomBrowser
    from agents.crawler import CrawlerAgent
    from agents.explorer import ExplorerAgent
    from agents.validator import ValidatorAgent
    from agents.reporter import ReporterAgent
    from memory.store import MemoryStore

    scan_start = datetime.now(timezone.utc).replace(tzinfo=None)
    screenshots_dir = reports_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    app_model = {}
    bug_reports = []
    regression_delta = {}
    fix_prompts = []
    reporter = ReporterAgent(reports_dir=reports_dir)

    try:
            # ── PHASE 1: Crawl ────────────────────────────────────────────────
            console.print("  [Phase 1]  Crawling.............. ", end="")
            async with PhantomBrowser(screenshots_dir, headless=headless) as browser:
                crawler = CrawlerAgent(
                    browser=browser,
                    base_url=url,
                    description=description,
                    max_pages=max_pages,
                    progress_callback=None,
                )
                await crawler.crawl()
                app_model = await crawler.build_app_model()
    
            pages_count = len(app_model.get("interaction_graph", {}).get("nodes", []))
            console.print(f"done   {pages_count} pages")

            # ── PHASE 2: Explore ──────────────────────────────────────────────
            console.print("  [Phase 2]  Exploring............. ", end="")
            flagged_events = []
            if not skip_explorer:
                async with PhantomBrowser(screenshots_dir, headless=headless) as browser:
                    explorer = ExplorerAgent(
                        browser=browser,
                        app_model=app_model,
                        progress_callback=None,
                        bug_counter_callback=None,
                    )
                    flagged_events = await explorer.run()
                console.print(f"done   {len(flagged_events)} events")
            else:
                console.print("skipped")

            # ── PHASE 3: Validate ────────────────────────────────────────────
            console.print("  [Phase 3]  Validating............ ", end="")
            validator = ValidatorAgent(screenshots_dir=screenshots_dir)
            bug_reports = await validator.validate_all(flagged_events, None)
            console.print(f"done   {len(bug_reports)} bugs")

            # ── PHASE 3b: Fix Prompts ─────────────────────────────────────────
            console.print("  [Phase 3b] Fix Prompts........... ", end="")
            try:
                reporter = ReporterAgent(reports_dir=reports_dir)
                fix_prompts = await reporter.generate_fix_prompts(bug_reports, None)
                console.print(f"done   {len(fix_prompts)} prompts")
            except Exception as e:
                logger.error(f"Fix prompts failed: {e}")
                console.print("done   0 prompts (error)")

            # ── PHASE 4: Memory ──────────────────────────────────────────────
            console.print("  [Phase 4]  Memory................ ", end="")
            store = MemoryStore()
            await store.initialize()
            scan_id = await store.create_scan(url, app_model)

            bug_titles = []
            for report in bug_reports:
                report_title = report.get("title", "Unknown Bug")
                bug_titles.append(report_title)
                await store.save_bug(
                    scan_id=scan_id,
                    title=report_title,
                    severity=report.get("severity", "low"),
                    steps=json.dumps(report.get("steps_to_reproduce", [])),
                    expected_behavior=report.get("expected_behavior", ""),
                    actual_behavior=report.get("actual_behavior", ""),
                    suggested_fix=report.get("suggested_fix", ""),
                    screenshot_path=report.get("screenshot_path", ""),
                    page_url=report.get("affected_url", ""),
                )

            regression_delta = await store.compute_regression_delta(url, scan_id, bug_titles)

            crawled = app_model.get("crawled_urls", [])
            nodes = app_model.get("interaction_graph", {}).get("nodes", [])
            coverage = len(crawled) / max(len(nodes), 1) * 100
            await store.update_scan_stats(
                scan_id,
                bugs_found=len(bug_reports),
                coverage_score=coverage,
                journeys_tested=len(app_model.get("journeys", [])),
            )
            reg_len = len(regression_delta.get("regressions", []))
            console.print(f"done   {reg_len} regression")

    finally:
            # ── PHASE 5: Report ────────────────────────────────────────────────────
            scan_end = datetime.now(timezone.utc).replace(tzinfo=None)
    
            def dual_print(*args, **kwargs):
                console.print(*args, **kwargs)

            dual_print()
            dual_print(Rule("[bold magenta]== PHANTOM REPORT ==[/bold magenta]", style="magenta"))
            dual_print()

            # Generate summary string into reports_dir directly
            summary_md = reporter.generate_summary(
                project_name=project_name,
                bug_reports=bug_reports,
                app_model=app_model,
                regression_delta=regression_delta,
                fix_prompts=fix_prompts,
                scan_start_time=scan_start,
                scan_end_time=scan_end,
            )

            duration = (scan_end - scan_start).total_seconds()
            crawled_count = len(app_model.get("crawled_urls", []))
            node_count = len(app_model.get("interaction_graph", {}).get("nodes", []))
            cov_pct = (crawled_count / max(node_count, 1)) * 100
            journeys_count = len(app_model.get("journeys", []))

            dual_print(make_stats_panel(
                bugs_found=len(bug_reports),
                journeys_tested=journeys_count,
                pages_visited=crawled_count,
                coverage_pct=cov_pct,
                duration=duration,
            ))
            dual_print()

            if bug_reports:
                dual_print(make_bug_table(bug_reports))
                dual_print()

            regressions = regression_delta.get("regressions", [])
            fixed_bugs = regression_delta.get("fixed", [])
            if regressions or fixed_bugs:
                reg_text = ""
                if regressions:
                    reg_text += f"[bold red]⚠️  {len(regressions)} Regression(s):[/bold red]\n"
                    for r in regressions:
                        reg_text += f"  • {r}\n"
                if fixed_bugs:
                    reg_text += f"[bold green]🎉 {len(fixed_bugs)} Fixed Bug(s):[/bold green]\n"
                    for f_ in fixed_bugs:
                        reg_text += f"  • {f_}\n"
                dual_print(Panel(reg_text.strip(), title="🔄 Regression Delta", border_style="yellow"))
                dual_print()

            # Paths reflect the dynamic folder structure
            dual_print(
                Panel(
                    f"[bold]Summary Report:[/bold] [link={reports_dir}/report.md]{reports_dir}/report.md[/link]\n"
                    f"[bold]Bug Reports:[/bold] {reports_dir}/*.json\n"
                    f"[bold]Screenshots:[/bold] {reports_dir}/screenshots/\n"
                    f"[bold]Full Log:[/bold] phantom.log\n"
                    f"[bold]App Model:[/bold] memory/app_model.json",
                    title="📁 Output Files",
                    border_style="bright_blue",
                )
            )
            dual_print()

            if fix_prompts:
                prompt_table = Table(
                    show_header=True, 
                    header_style="bold", 
                    box=box.ASCII,
                    show_lines=False
                )
                prompt_table.add_column("FIX PROMPTS GENERATED")
                for prompt_file, sev in fix_prompts:
                    prompt_table.add_row(f"{prompt_file}  →  {sev.upper()} bug")
            
                dual_print(prompt_table)
                dual_print("  [dim]Paste any fix prompt into Cursor, Antigravity,\n  or Claude to fix the bug with full context.[/dim]")
                dual_print()

            dual_print(
                f"[bold green]✅ Scan complete in {duration:.1f}s — "
                f"{len(bug_reports)} bug(s) found across {crawled_count} page(s)[/bold green]"
            )
            dual_print()
    
            # report.md is saved by reporter.generate_summary



# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
