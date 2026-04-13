import sys
import codecs
content = codecs.open('main.py', 'r', 'utf-8').read()

import textwrap

# update max_pages
content = content.replace("max_pages = 25 if deep else 5", "max_pages = 25 if deep else 10")

# fix Phase 3b
old_phase_3b = """    # ── PHASE 3b: Fix Prompts ─────────────────────────────────────────
    console.print("  [Phase 3b] Fix Prompts........... ", end="")
    reporter = ReporterAgent(reports_dir=reports_dir)
    fix_prompts = await reporter.generate_fix_prompts(bug_reports, None)
    console.print(f"done   {len(fix_prompts)} prompts")"""

new_phase_3b = """    # ── PHASE 3b: Fix Prompts ─────────────────────────────────────────
    console.print("  [Phase 3b] Fix Prompts........... ", end="")
    try:
        reporter = ReporterAgent(reports_dir=reports_dir)
        fix_prompts = await reporter.generate_fix_prompts(bug_reports, None)
        console.print(f"done   {len(fix_prompts)} prompts")
    except Exception as e:
        logger.error(f"Fix prompts failed: {e}")
        console.print("done   0 prompts (error)")"""
content = content.replace(old_phase_3b, new_phase_3b)

# Wait we need to capture `fix_prompts = []` before try

# Split into phases

part1 = content[:content.find("    # ── PHASE 1: Crawl ────────────────────────────────────────────────")]
part2 = content[content.find("    # ── PHASE 1: Crawl ────────────────────────────────────────────────"):content.find("    # ── PHASE 5: Report ────────────────────────────────────────────────────")]
part3 = content[content.find("    # ── PHASE 5: Report ────────────────────────────────────────────────────"):]

# create new string
new_content = part1 + """    app_model = {}
    bug_reports = []
    regression_delta = {}
    fix_prompts = []
    reporter = ReporterAgent(reports_dir=reports_dir)

    try:
""" + textwrap.indent(part2, "        ") + """    finally:
""" + textwrap.indent(part3.split("# ── Entry point ───────────────────────────────────────────────────────────────")[0], "        ") + """
# ── Entry point ───────────────────────────────────────────────────────────────""" + part3.split("# ── Entry point ───────────────────────────────────────────────────────────────")[1]

codecs.open('main.py', 'w', 'utf-8').write(new_content)
print("Updated main.py!")
