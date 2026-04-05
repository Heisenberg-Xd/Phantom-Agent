import sys
import codecs

# ── 1. Fix main.py ────────────────────────────────────────────────────────
with codecs.open('main.py', 'r', 'utf-8') as f:
    main_content = f.read()

# Replace the method signature call
old_call = """            summary_md = reporter.generate_summary(
                bug_reports=bug_reports,
                app_model=app_model,
                regression_delta=regression_delta,
                scan_start_time=scan_start,
                scan_end_time=scan_end,
            )"""
new_call = """            summary_md = reporter.generate_summary(
                project_name=project_name,
                bug_reports=bug_reports,
                app_model=app_model,
                regression_delta=regression_delta,
                fix_prompts=fix_prompts,
                scan_start_time=scan_start,
                scan_end_time=scan_end,
            )"""
main_content = main_content.replace(old_call, new_call)

# We must also change `async def _run_scan` signature to accept project_name!
old_sig = """async def _run_scan(
    url: str,
    description: str,
    max_pages: int,
    headless: bool,
    skip_explorer: bool,
    reports_dir: Path,
):"""
new_sig = """async def _run_scan(
    project_name: str,
    url: str,
    description: str,
    max_pages: int,
    headless: bool,
    skip_explorer: bool,
    reports_dir: Path,
):"""
main_content = main_content.replace(old_sig, new_sig)

old_run = """    asyncio.run(_run_scan(url, description, max_pages, headless, skip_explorer, reports_dir))"""
new_run = """    asyncio.run(_run_scan(project_name, url, description, max_pages, headless, skip_explorer, reports_dir))"""
main_content = main_content.replace(old_run, new_run)


# Remove dual_print / file_console
main_content = main_content.replace("""            string_buffer = io.StringIO()
            file_console = Console(file=string_buffer, force_terminal=False)

            def dual_print(*args, **kwargs):
                console.print(*args, **kwargs)
                file_console.print(*args, **kwargs)""", """            def dual_print(*args, **kwargs):
                console.print(*args, **kwargs)""")

# Remove the file writing at the very bottom
main_content = main_content.replace("""            # Save exact string_buffer output to report.md
            with open(reports_dir / "report.md", "w", encoding="utf-8") as f:
                f.write(string_buffer.getvalue())""", """            # report.md is saved by reporter.generate_summary""")

with codecs.open('main.py', 'w', 'utf-8') as f:
    f.write(main_content)


# ── 2. Fix reporter.py ──────────────────────────────────────────────────
with codecs.open('agents/reporter.py', 'r', 'utf-8') as f:
    reporter_content = f.read()

part1 = reporter_content[:reporter_content.find("    def generate_summary(")]
new_summary = '''    def generate_summary(
        self,
        project_name: str,
        bug_reports: list[dict],
        app_model: dict,
        regression_delta: dict,
        fix_prompts: list,
        scan_start_time: datetime,
        scan_end_time: datetime,
    ) -> str:
        """Generate and save report.md using pure ASCII formatting."""

        self.reports_dir.mkdir(parents=True, exist_ok=True)
        summary_path = self.reports_dir / "report.md"

        duration_sec = (scan_end_time - scan_start_time).total_seconds()
        duration_fmt = f"{duration_sec:.0f}"
        scan_date_fmt = scan_start_time.strftime("%Y-%m-%d %H:%M:%S UTC")
        
        journeys = app_model.get("journeys", [])
        crawled_urls = app_model.get("crawled_urls", [])
        graph_nodes = app_model.get("interaction_graph", {}).get("nodes", [])
        base_url = app_model.get("base_url", "Unknown")

        total_bugs = len(bug_reports)
        visited_count = len(crawled_urls)
        total_discovered = len(graph_nodes) if graph_nodes else visited_count
        coverage_pct = round((visited_count / total_discovered) * 100) if total_discovered > 0 else 0
        cov_str = f"{coverage_pct}%"

        # Count severities
        from collections import Counter
        severity_counter = Counter(r.get("severity", "low").lower() for r in bug_reports)
        
        def bar(sev):
            count = severity_counter.get(sev, 0)
            if total_bugs == 0:
                filled = 0
            else:
                filled = round((count / total_bugs) * 10)
            return ("█" * filled) + ("░" * (10 - filled))

        def pad_lines(text, pad="             "):
            if not text: return "None"
            if isinstance(text, list):
                if not text: return "None"
                lines = [f"{i+1}. {s}" for i, s in enumerate(text)]
            else:
                lines = str(text).split("\\n")
            if not lines: return "None"
            return lines[0] + "".join("\\n" + pad + line for line in lines[1:])

        report = []
        report.append("================================================================================")
        report.append("    ____  __  _____    _    _   _ _______ ____  __  __")
        report.append(r"   |  _ \|  || ___ \  / \  | \ | |__   __|  _ \|  \/  |")
        report.append(r"   | |_) | |_| |_/ / / _ \ |  \| |  | |  | |_) | |\/| |")
        report.append(r"   |  __/|  _|  _ < / ___ \| |\  |  | |  |  _ <| |  | |")
        report.append(r"   |_|   |_| |_| \_/_/   \_|_| \_|  |_|  |_| \_|_|  |_|")
        report.append("")
        report.append("                   A U T O N O M O U S   Q A   A G E N T")
        report.append("================================================================================")
        report.append("")
        report.append(f"  Project   : {project_name}")
        report.append(f"  Target    : {base_url}")
        report.append(f"  Scan Date : {scan_date_fmt}")
        report.append(f"  Duration  : {duration_fmt}s")
        report.append("  Generated : Phantom QA Agent v1.0")
        report.append("")
        report.append("================================================================================")
        report.append("  SCAN SUMMARY")
        report.append("================================================================================")
        report.append("")
        report.append("  +-------------+----------+---------------+-----------+----------+")
        report.append("  | Bugs Found  | Journeys | Pages Visited | Coverage  | Duration |")
        report.append("  +-------------+----------+---------------+-----------+----------+")
        report.append(f"  | {total_bugs:^11} | {len(journeys):^8} | {visited_count:^13} | {cov_str:^9} | {duration_fmt+'s':^8} |")
        report.append("  +-------------+----------+---------------+-----------+----------+")
        report.append("")
        report.append("  Severity Breakdown")
        report.append("  ------------------")
        report.append(f"  CRITICAL  [{bar('critical')}]  {severity_counter.get('critical', 0)}")
        report.append(f"  HIGH      [{bar('high')}]  {severity_counter.get('high', 0)}")
        report.append(f"  MEDIUM    [{bar('medium')}]  {severity_counter.get('medium', 0)}")
        report.append(f"  LOW       [{bar('low')}]  {severity_counter.get('low', 0)}")
        report.append("")
        report.append("")
        report.append("================================================================================")
        report.append("  BUGS FOUND")
        report.append("================================================================================")
        report.append("")
        
        if not bug_reports:
            report.append("  [*] No bugs found during this scan.")
            report.append("")
        else:
            for idx, bug in enumerate(bug_reports, start=1):
                sev = bug.get("severity", "low").upper()
                cat = bug.get("category", "unknown")
                # Title padding logic to handle multiline safely though title is usually 1 line
                report.append(f"  #{idx:03d}  {sev}  {cat}")
                report.append("  -----------------------------------------------------------------------")
                report.append(f"  Title    : {pad_lines(bug.get('title', 'Unknown'))}")
                report.append(f"  URL      : {pad_lines(bug.get('affected_url', ''))}")
                report.append(f"  Steps    : {pad_lines(bug.get('steps_to_reproduce', []))}")
                report.append(f"  Expected : {pad_lines(bug.get('expected_behavior', ''))}")
                report.append(f"  Actual   : {pad_lines(bug.get('actual_behavior', ''))}")
                report.append(f"  Fix      : {pad_lines(bug.get('suggested_fix', ''))}")
                report.append("  -----------------------------------------------------------------------")
                report.append("")

        report.append("")
        report.append("================================================================================")
        report.append("  REGRESSION DELTA")
        report.append("================================================================================")
        report.append("")
        
        regressions = regression_delta.get("regressions", [])
        fixed = regression_delta.get("fixed", [])
        
        report.append("  Regressions (bugs that came back)")
        report.append("  ----------------------------------")
        if regressions:
            for r in regressions:
                report.append(f"  [!] {r}")
        else:
            report.append("  [*] No regressions detected")
        report.append("")
        
        report.append("  Fixed Since Last Scan")
        report.append("  ----------------------")
        if fixed:
            for f_ in fixed:
                report.append(f"  [+] {f_}")
        else:
            report.append("  [*] No verified fixes")
        report.append("")
        report.append("")
        
        report.append("================================================================================")
        report.append("  FIX PROMPTS")
        report.append("================================================================================")
        report.append("")
        if not fix_prompts:
            report.append("  [*] No fix prompts generated.")
        else:
            report.append("  Fix prompts have been generated for each bug.")
            report.append("  Paste them into Cursor, Antigravity, or Claude")
            report.append("  to get an AI-assisted fix with full context.")
            report.append("")
            # fix_prompts contains list of tuples: (filename, severity)
            # we need to match it with titles inside bug_reports.
            # Usually they are 1:1 matching by index.
            for idx, (p_file, _) in enumerate(fix_prompts):
                title = bug_reports[idx].get("title", "Unknown") if idx < len(bug_reports) else "Unknown"
                report.append(f"  {p_file:<31} ->  {title}")
                
        report.append("")
        report.append("")
        report.append("================================================================================")
        report.append("  OUTPUT FILES")
        report.append("================================================================================")
        report.append("")
        report.append("  report.md      -> this file")
        report.append(f"  screenshots/   -> {total_bugs} screenshots at moment of each bug")
        report.append(f"  fix_prompts/   -> {len(fix_prompts)} ready-to-paste AI fix prompts")
        report.append("  phantom.log    -> full execution log")
        report.append("")
        report.append("")
        report.append("================================================================================")
        report.append("  END OF REPORT")
        report.append("  Phantom QA Agent | github.com/yourname/phantom-qa")
        report.append("================================================================================")
        report.append("")

        markdown = "\\n".join(report)
        summary_path.write_text(markdown, encoding="utf-8")
        logger.info(f"Summary report saved to {summary_path}")
        return markdown
'''
with codecs.open('agents/reporter.py', 'w', 'utf-8') as f:
    f.write(part1 + new_summary)

print("Report generation updated successfully!")
