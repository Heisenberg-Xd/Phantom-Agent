"""
Phantom — Autonomous QA Agent
CLI entry point with Rich terminal output.
"""

import logging
# ── Silence ALL logging before any module loads ──────────────────────────────
logging.getLogger().handlers = []
for _name in list(logging.root.manager.loggerDict):
    logging.getLogger(_name).handlers = []
    logging.getLogger(_name).propagate = False
logging.getLogger().setLevel(logging.CRITICAL)

import asyncio
import io
import json
import os
import shutil
import sys
import getpass
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
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ── Phantom file-only logger ─────────────────────────────────────────────────
ph = logging.getLogger("phantom")
ph.handlers = []
ph.propagate = False
ph.setLevel(logging.DEBUG)
_log_fh = logging.FileHandler("phantom.log", encoding="utf-8")
_log_fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
ph.addHandler(_log_fh)

LOG_FILE = Path("phantom.log")
logger = ph

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
            "[dim]Autonomous QA Agent — powered by ImagineX[/dim]",
            border_style="magenta",
            padding=(1, 4),
        )
    )
    console.print()


def make_bug_table(bug_reports: list) -> Table:
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


def should_report_bug(bug) -> bool:
    
    actual = bug.get("actual_behavior", "").lower()
    title = bug.get("title", "").lower()
    
    # Hard discard phrases — any match = throw away
    discard_phrases = [
        "no hard evidence",
        "but no hard evidence",
        "could not confirm",
        "no evidence",
        "submitted payload",      # means unconfirmed
        "without validation",     # too generic, unconfirmed
    ]
    
    for phrase in discard_phrases:
        if phrase in actual or phrase in title:
            return False
            
    # Generic AI slop titles to drop
    if any(title.strip() == t for t in ["[UNKNOWN]", "Unknown Bug", "error", "bug", "issue"]):
        return False
        
    # Validation gates for descriptions
    desc = bug.get("description", "").lower()
    if desc.startswith("generic") or title.startswith("generic") or title == "unknown":
        return False
    
    # Must have confirmed evidence field
    evidence = bug.get("evidence", {})
    if not evidence.get("confirmed", False):
        # Only allow medium/low through without 
        # confirmation if they have a different 
        # detection method (e.g. slow page timer)
        if bug.get("raw_event_type") not in [
            "slow_page", "console_error", 
            "broken_link", "http_error",
            "invalid_input", "journey_failure"
        ]:
            return False
    
    return True

def deduplicate_bugs(bugs):
    groups = {}
    for bug in bugs:
        event_type = bug.get("raw_event_type") or bug.get("category", "unknown")
        url = bug.get("affected_url") or bug.get("url", "unknown")
        severity = bug.get("severity", "low")
        key = f"{event_type}_{url}_{severity}"
        if key not in groups:
            groups[key] = {
                "title": bug.get("title", "Unknown"),
                "severity": severity,
                "type": event_type,
                "category": bug.get("category", "unknown"),
                "affected_urls": [],
                "steps_to_reproduce": bug.get("steps_to_reproduce", []),
                "expected_behavior": bug.get("expected_behavior", ""),
                "actual_behavior": bug.get("actual_behavior", ""),
                "suggested_fix": bug.get("suggested_fix", ""),
                "screenshot_path": bug.get("screenshot_path", ""),
                "raw_event_type": bug.get("raw_event_type", event_type)
            }
        
        # Make sure current url is added to affected URLs list
        url_to_add = bug.get("affected_url") or bug.get("url")
        if url_to_add and url_to_add not in groups[key]["affected_urls"]:
            groups[key]["affected_urls"].append(url_to_add)
            groups[key]["affected_urls"].append(url)
    return list(groups.values())


def collect_credentials_hitl(url: str) -> dict:
    """Synchronous helper to collect credentials from terminal."""
    print("\n" + "="*60)
    print("🔐 [LOGIN REQUIRED] Authentication Wall Detected")
    print(f"URL: {url}")
    print("="*60)
    
    attempts = 0
    while attempts < 3:
        try:
            username = input("\nEnter username: ")
            password = getpass.getpass("Enter password: ")
            if username and password:
                return {"username": username, "password": password, "email": username}
            print("❌ Username and password are required.")
            attempts += 1
        except (KeyboardInterrupt, EOFError):
            print("\n⚠️ Login aborted by user.")
            break
    return {}

# ── Core async pipeline ───────────────────────────────────────────────────────

async def run_pipeline(
    url: str,
    name: str,
    description: str,
    reports_dir: Path,
    max_pages: int = 8,
    headless: bool = True,
    credentials: dict = None,
    login_steps: list = None,
) -> list:
    """
    Clean one-way async pipeline. Every phase returns its data explicitly.
    No global state. Asserts after every phase catch empty results immediately.
    """
    from agents.crawler import CrawlerAgent
    from agents.explorer import ExplorerAgent
    from agents.validator import ValidatorAgent
    from agents.reporter import ReporterAgent
    from memory.store import MemoryStore
    from browser.runner import PhantomBrowser

    pipeline_start = datetime.now(timezone.utc).replace(tzinfo=None)
    screenshots_dir = reports_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: Crawl ────────────────────────────────────────────────────────
    import time
    t0 = time.time()
    console.print("  [Phase 1]  Crawling.............. ", end="")
    try:
        crawler = CrawlerAgent(
            base_url=url,
            description=description,
            max_pages=max_pages,
            screenshots_dir=screenshots_dir,
            headless=headless,
            login_steps=login_steps,
        )
        pages = await crawler.crawl()
        assert isinstance(pages, list), "Crawler must return a list"
        assert len(pages) > 0, f"Crawler returned 0 pages for {url}"
        console.print(f"done   {len(pages)} pages ({time.time()-t0:.1f}s)")
    except AssertionError as e:
        console.print(f"[red]FAILED[/red]   {e}")
        logger.error(f"Phase 1 assertion: {e}")
        pages = []
    except Exception as e:
        console.print(f"[red]ERROR[/red]")
        logger.error(f"Phase 1 crawl error: {e}")
        pages = []

    # Build journeys from pages via Gemini
    journeys = []
    try:
        if pages:
            journeys = await crawler.build_journeys(pages)
    except Exception as e:
        logger.error(f"build_journeys failed: {e}")

    auth_stats = {}
    bug_reports = []
    events = []

    # ── Phase 2: Explore ──────────────────────────────────────────────────────
    t0 = time.time()
    console.print("  [Phase 2]  Exploring............. ", end="")
    
    # Same-session browser management
    async with PhantomBrowser(screenshots_dir, headless=headless) as browser:
        try:
            explorer = ExplorerAgent(
                base_url=url,
                pages=pages,
                journeys=journeys,
                screenshots_dir=screenshots_dir,
                headless=headless,
                credentials=credentials,
                login_steps=login_steps,
            )
            
            # Phase 2a: Generic Exploration
            exploration_result = await explorer.explore(browser)
                
            events = exploration_result.events
            journeys = exploration_result.journeys
            
            logger.info(f"[phase_runner] received {len(events)} events")
            console.print(f"done   {len(events)} events ({time.time()-t0:.1f}s)")
            
            # No hard-fail on 0 events for generic recovery
            if True: # Always proceed to security/validate
                # ── Phase 2b: Security Tests ──────────────────────────────────
                t0 = time.time()
                console.print("  [Phase 2b] Security Tests......... ", end="")
                try:
                    from agents.security import SecurityAgent
                    security = SecurityAgent(
                        base_url=url,
                        pages=pages,
                        screenshots_dir=screenshots_dir,
                        headless=headless,
                        login_steps=login_steps,
                    )
                    # Maintains session in the same browser instance
                    security_bugs = await security.run_tests(browser, events)
                    bug_reports.extend(security_bugs)
                    console.print(f"done   {len(security_bugs)} bugs ({time.time()-t0:.1f}s)")
                except Exception as e:
                    logger.error(f"Phase 2b security error: {e}")
                    console.print("done   (failed)")

                # ── Phase 3: Validate ─────────────────────────────────────────
                t0 = time.time()
                console.print("  [Phase 3]  Validating Bugs....... ", end="")
                try:
                    validator = ValidatorAgent(
                        screenshots_dir=screenshots_dir,
                    )
                    # Maintains session
                    validated_bugs = await validator.validate(browser, bug_reports)
                    bug_reports = deduplicate_bugs(validated_bugs)
                    console.print(f"done   {len(bug_reports)} bugs ({time.time()-t0:.1f}s)")
                except Exception as e:
                    logger.error(f"Phase 3 validate error: {e}")
                    console.print("done   (failed)")
        except Exception as e:
            console.print(f"[red]ERROR[/red]")
            logger.error(f"Phase 2 exploration failure: {e}")

    # ── Phase 3b: Fix Prompts ─────────────────────────────────────────────────
    console.print("  [Phase 3b] Fix Prompts........... ", end="")
    reporter = ReporterAgent(reports_dir=reports_dir)
    fix_prompts = []
    try:
        fix_prompts = await reporter.generate_fix_prompts(bug_reports)
        console.print(f"done   {len(fix_prompts)} prompts")
    except Exception as e:
        logger.error(f"Fix prompts failed: {e}")
        console.print("done   0 prompts (error)")

    # ── Phase 4: Memory ───────────────────────────────────────────────────────
    console.print("  [Phase 4]  Memory................ ", end="")
    regression = {}
    try:
        memory = MemoryStore()
        await memory.initialize()
        scan_id = await memory.create_scan(url, {"pages": len(pages)})
        bug_titles = [r.get("title", "Unknown Bug") for r in bug_reports]
        for report in bug_reports:
            await memory.save_bug(
                scan_id=scan_id,
                title=report.get("title", "Unknown Bug"),
                severity=report.get("severity", "low"),
                steps=json.dumps(report.get("steps_to_reproduce", [])),
                expected_behavior=report.get("expected_behavior", ""),
                actual_behavior=report.get("actual_behavior", ""),
                suggested_fix=report.get("suggested_fix", ""),
                screenshot_path=report.get("screenshot_path", ""),
                page_url=report.get("affected_urls", [""])[0] if report.get("affected_urls") else "",
            )
        regression = await memory.compute_regression_delta(url, scan_id, bug_titles)
        await memory.update_scan_stats(
            scan_id,
            bugs_found=len(bug_reports),
            coverage_score=len(pages) / max(max_pages, 1) * 100,
            journeys_tested=len(journeys),
        )
        reg_len = len(regression.get("regressions", []))
        console.print(f"done   {reg_len} regressions")
    except Exception as e:
        logger.error(f"Memory phase failed: {e}")
        console.print("done   (memory error)")

    # Read journeys from app_model.json if they were built offline or returned empty due to parsing
    if not journeys:
        try:
            am_path = Path("memory/app_model.json")
            if am_path.exists():
                with open(am_path) as f:
                    am = json.load(f)
                journeys = am.get("journeys", [])
        except Exception:
            pass

    # ── Phase 5: Report ───────────────────────────────────────────────────────
    pipeline_end = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed = (pipeline_end - pipeline_start).total_seconds()

    try:
        reporter.write_report(
            url=url,
            name=name,
            pages=pages,
            bug_reports=bug_reports,
            fix_prompts=fix_prompts,
            regression=regression,
            journeys=journeys,
            duration=elapsed,
            scan_start=pipeline_start,
        )
    except Exception as e:
        logger.error(f"Report write failed: {e}")

    return bug_reports, pages, journeys


# ── Main scan command ─────────────────────────────────────────────────────────

@app.command()
def scan(
    url: str = typer.Option(..., "--url", "-u", help="Target URL to scan"),
    description: str = typer.Option(..., "--description", "-d", help="Plain-English app description"),
    project_name: str = typer.Option(None, "--name", help="Project name for reports folder"),
    deep: bool = typer.Option(False, "--deep", help="Run full scan instead of fast"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run browser headlessly"),
    credentials_json: str = typer.Option(None, "--credentials", help='JSON credentials e.g. \'{"email":"x@x.com","password":"123"}\''),
    login_steps: str = typer.Option(None, "--login-steps", help='JSON array of login steps'),
):
    """
    👻 Run Phantom QA scan on a web application.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        console.print("[bold red]❌ GEMINI_API_KEY environment variable not set![/bold red]")
        raise typer.Exit(1)

    max_pages = 20 if deep else 8

    if not project_name:
        project_name = urlparse(url).netloc.replace(":", "_") or "unnamed"

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    reports_dir = Path("reports") / project_name / timestamp
    reports_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_scans(Path("reports") / project_name, max_scans=10)

    credentials = None
    if credentials_json:
        try:
            credentials = json.loads(credentials_json)
        except Exception:
            console.print("[yellow]⚠ Could not parse --credentials JSON. Proceeding without credentials.[/yellow]")

    login_steps_parsed = None
    if login_steps:
        try:
            login_steps_parsed = json.loads(login_steps)
        except Exception:
            console.print("[yellow]⚠ Could not parse --login-steps JSON. Proceeding without steps.[/yellow]")

    print_banner()
    console.print(f"[bold]🎯 Target:[/bold] {url}")
    console.print(f"[bold]📋 Description:[/bold] {description}")
    console.print(f"[bold]📁 Output:[/bold] {reports_dir}")
    if credentials:
        console.print(f"[bold]🔑 Credentials:[/bold] {credentials.get('email', '?')}")
    console.print()

    scan_start = datetime.now(timezone.utc).replace(tzinfo=None)

    bug_reports, pages_visited, journeys_run = asyncio.run(
        run_pipeline(
            url=url,
            name=project_name,
            description=description,
            reports_dir=reports_dir,
            max_pages=max_pages,
            headless=headless,
            credentials=credentials,
            login_steps=login_steps_parsed,
        )
    )

    scan_end = datetime.now(timezone.utc).replace(tzinfo=None)
    scan_end = datetime.now(timezone.utc).replace(tzinfo=None)
    duration = (scan_end - scan_start).total_seconds()

    console.print()
    console.print(Rule("[bold magenta]== PHANTOM REPORT ==[/bold magenta]", style="magenta"))
    console.print()

    # Coverage Patch - Interaction based
    hardcoded_journeys = [j for j in journeys_run if j.get("hardcoded")]
    generic_journeys = [j for j in journeys_run if not j.get("hardcoded")]
    
    expected_steps = sum([len(j.get("steps", [])) for j in generic_journeys])
    executed_steps = sum([j.get("executed_steps", 0) for j in generic_journeys])
    
    if expected_steps > 0:
        cov_pct = round((executed_steps / expected_steps) * 100)
    elif hardcoded_journeys:
        passed = sum(1 for j in hardcoded_journeys if not j.get("failed"))
        cov_pct = round((passed / len(hardcoded_journeys)) * 100) if hardcoded_journeys else 0
    else:
        cov_pct = 0
    
    # Truthful stats for panel
    executed_journeys_count = len([j for j in generic_journeys if j.get("executed_steps", 0) > 0]) + len(hardcoded_journeys)

    console.print(make_stats_panel(
        bugs_found=len(bug_reports),
        journeys_tested=executed_journeys_count,
        pages_visited=len(pages_visited),
        coverage_pct=cov_pct,
        duration=duration,
    ))
    console.print()

    # Regression Safety Patch
    if executed_journeys_count == 0 or (expected_steps > 0 and cov_pct < 40):
        console.print("[bold red]Critical failure: no executable journeys detected (Coverage: " + str(cov_pct) + "%)[/bold red]")
        console.print("[red]Regression Safety Check Failed. Halting pipeline integrations.[/red]")
        sys.exit(1)

    if bug_reports:
        console.print(make_bug_table(bug_reports))
        console.print()

    console.print(
        Panel(
            f"[bold]Summary Report:[/bold] {reports_dir / 'report.md'}\n"
            f"[bold]Bug Reports:[/bold] {reports_dir}/*.json\n"
            f"[bold]Screenshots:[/bold] {reports_dir / 'screenshots'}\n"
            f"[bold]Fix Prompts:[/bold] {reports_dir / 'fix_prompts'}\n"
            f"[bold]Full Log:[/bold] phantom.log",
            title="📁 Output Files",
            border_style="bright_blue",
        )
    )
    console.print()
    console.print(
        f"[bold green]✅ Scan complete in {duration:.1f}s — "
        f"{len(bug_reports)} bug(s) found[/bold green]"
    )
    console.print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
