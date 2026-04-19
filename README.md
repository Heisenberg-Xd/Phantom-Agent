```
██████╗ ██╗  ██╗ █████╗ ███╗   ██╗████████╗ ██████╗ ███╗   ███╗
██╔══██╗██║  ██║██╔══██╗████╗  ██║╚══██╔══╝██╔═══██╗████╗ ████║
██████╔╝███████║███████║██╔██╗ ██║   ██║   ██║   ██║██╔████╔██║
██╔═══╝ ██╔══██║██╔══██║██║╚██╗██║   ██║   ██║   ██║██║╚██╔╝██║
██║     ██║  ██║██║  ██║██║ ╚████║   ██║   ╚██████╔╝██║ ╚═╝ ██║
╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝ ╚═╝     ╚═╝
```

**Autonomous QA Agent — powered by Gemini AI**

> Your app's worst nightmare but Your team's best friend.

An agent that crawls, breaks, and reports bugs in any web app.
No test scripts. No setup. No source code access. Just a URL..Dang.

```bash
phantom scan --url "http://localhost:3000" --description "my app"
```

---

## Overview

Phantom acts as a senior QA engineer running continuously in the background.

Give it a URL and describe your app in plain English. It opens a real browser, maps every page, executes real user journeys, attempts adversarial edge cases, and produces a structured bug report — complete with screenshots, severity scores, and suggested fixes.

Works on any web app. Local dev servers. Staging environments. Production URLs. Anything with an address.

---

## Terminal Output

```
+-------------------------------------------------------+
|                                                       |
|   P H A N T O M   Q A   A G E N T                    |
|   Autonomous browser-based bug detection              |
|                                                       |
+-------------------------------------------------------+

  Target      : https://your-app.com
  Description : SaaS app -- users upload PDFs and get AI summaries
  Max Pages   : 20

  [Phase 1]  Crawling app................................ done   14 pages
  [Phase 2]  Adversarial exploration..................... done    7 events
  [Phase 3]  Validating with Gemini 2.5 Pro............. done    5 bugs
  [Phase 4]  Regression memory.......................... done    2 regressions

+----------+----------+----------+----------+----------+
|    5     |    4     |    14    |   93%    |   87s    |
|  Bugs    | Journeys |  Pages   | Coverage | Duration |
+----------+----------+----------+----------+----------+

  Bug Severity Distribution
  -------------------------
  Critical  [##########]  1
  High      [##########]  2
  Medium    [#####     ]  1
  Low       [##        ]  1

  Bug Report
  ----------
  1  CRITICAL  Double-click on checkout creates duplicate charges
  2  HIGH      Session token persists after logout
  3  HIGH      File upload silently fails for PDFs over 10MB
  4  MEDIUM    Filter by status shows stale count after deletion
  5  LOW       Console error: mixed content on homepage

  Output
  ------
  Summary    : reports/summary.md
  Bug JSON   : reports/*.json
  Screenshots: reports/screenshots/
  Memory     : memory/app_model.json
  Log        : phantom.log

  Scan complete -- 5 bug(s) found across 14 page(s) in 87.3s
```

---

## How It Works

Phantom runs four phases end-to-end without any intervention.

**Phase 1 -- App Comprehension**

Opens a real Chromium browser. Crawls every reachable page. Maps every button, form, link, input, and state transition. Sends the full interaction graph to Gemini, which identifies user journeys and risk areas based on your plain-English description.

**Phase 2 -- Adversarial Exploration**

Executes each identified user journey from start to finish. Then attacks every form with a library of adversarial inputs: empty fields, SQL injection strings, XSS payloads, Unicode characters, oversized files, wrong file types, negative numbers, and past dates. Monitors every network request, console log, and page state change.

**Phase 3 -- AI Bug Validation**

Every flagged event is sent to Gemini 2.5 Pro with the screenshot. The model classifies severity, writes a structured bug report in plain English, and suggests a concrete fix. No noise -- only events with reproducible evidence become reports.

**Phase 4 -- Regression Memory**

Phantom stores every scan result in a local SQLite database. On subsequent scans of the same URL, it compares results against history and surfaces new bugs, returning regressions, and confirmed fixes.

```
  URL + Description
        |
        v
  +---------------------+
  |  App Comprehension  |  <-- Playwright crawler + Gemini journey inference
  +---------------------+
        |
        v
  +---------------------+
  | Adversarial Explorer|  <-- Journey execution + edge case fuzzing
  +---------------------+
        |
        v
  +---------------------+
  |  Validation Agent   |  <-- Gemini 2.5 Pro bug reasoning
  +---------------------+
        |
        v
  +---------------------+
  |  Regression Memory  |  <-- SQLite diff across scans
  +---------------------+
        |
        v
  Bug Reports + Summary
```

---

## Install

```bash
git clone https://github.com/yourname/phantom-qa
cd phantom-qa
pip install -e .
playwright install chromium
```

---

## Setup

```bash
cp .env.example .env
```

Add your Gemini API key to `.env`:

```
GEMINI_API_KEY=your_key_here
```

Get a free key at **aistudio.google.com** -- no credit card required.

---

## Usage

**Scan any public URL:**

```bash
phantom scan \
  --url "https://your-app.com" \
  --description "E-commerce app. Users browse products, add to cart, checkout."
```

**Scan a local dev server:**

```bash
# Terminal 1 -- start your app
npm run dev

# Terminal 2 -- run Phantom
phantom scan \
  --url "http://localhost:3000" \
  --description "Your app description here"
```

**Scan an app that requires login:**

```bash
phantom scan \
  --url "http://localhost:3000" \
  --description "Dashboard SaaS" \
  --login-steps '[
    {"action": "goto",  "url": "http://localhost:3000/login"},
    {"action": "fill",  "selector": "#email",    "value": "test@example.com"},
    {"action": "fill",  "selector": "#password", "value": "password123"},
    {"action": "click", "selector": "[type=submit]"},
    {"action": "wait",  "ms": 2000}
  ]'
```

---

## Output Structure

```
reports/
|-- summary.md                  full scan report
|-- bug_001_critical.json       structured bug report
|-- bug_002_high.json
|-- bug_003_high.json
|-- screenshots/
|   |-- bug_001.png             screenshot at moment of failure
|   |-- bug_002.png
|   `-- bug_003.png

memory/
`-- app_model.json              regression database

phantom.log                     full execution log with timestamps
```

Each bug report contains:

```json
{
  "title": "Double-click on checkout creates duplicate charges",
  "severity": "critical",
  "steps_to_reproduce": [
    "Navigate to /cart",
    "Add any item to cart",
    "Double-click the checkout button rapidly"
  ],
  "expected_behavior": "Single order created",
  "actual_behavior": "Two identical orders created, two charges processed",
  "suggested_fix": "Disable the checkout button immediately on first click",
  "screenshot_path": "reports/screenshots/bug_001.png",
  "affected_url": "https://your-app.com/cart",
  "timestamp": "2026-04-05T10:23:11"
}
```

---

## What Phantom Catches

| Category       | Examples                                                          |
|----------------|-------------------------------------------------------------------|
| Forms          | Empty submission accepted, no validation, silent failures         |
| Authentication | Session persists after logout, broken post-login redirect         |
| File Uploads   | Wrong file type accepted, large file crashes without feedback     |
| Navigation     | Broken links, infinite loading states, blank page transitions     |
| Console Errors | Unhandled JS exceptions, failed network requests, mixed content   |
| Performance    | Pages exceeding 3s load time, request timeouts                    |
| Edge Cases     | SQL strings, XSS payloads, Unicode input, integer overflow        |

---

## What Phantom Does Not Do

- Does not require access to your source code
- Does not require you to write selectors or test scripts
- Does not replace unit tests or integration tests
- Does not catch logic bugs that require deep domain knowledge

It catches the bugs that slip through because nobody thought to test that specific combination of inputs and actions -- the ones your users find before you do.

---

## Edge Case Library

Built-in adversarial inputs in `config/edge_cases.yaml`:

```yaml
strings:
  - ""                            # empty input
  - " "                           # whitespace only
  - "' OR 1=1 --"                 # SQL injection
  - "<script>alert(1)</script>"   # XSS payload
  - "AAAA..."                     # 10,000 character string
  - "日本語テスト"                  # unicode multibyte
  - "-1"                          # negative number
  - "2000-01-01"                  # past date
  - "999999999999"                # integer overflow attempt

files:
  - type: wrong_extension         # .exe renamed as .jpg
  - type: empty_file              # 0 byte file
  - type: oversized               # 50MB file
```

Extend this file with domain-specific inputs for your application.

---

## Regression Memory

Phantom maintains a local SQLite database of every bug it has ever found per URL.

On each subsequent scan of the same URL:

```
  Previous Scan     Current Scan     Result
  -------------     ------------     --------
  Bug A  (open)  -> Not found     -> [FIXED]
  Bug B  (open)  -> Found again   -> [REGRESSION]
  (none)         -> Bug C found   -> [NEW]
```

The regression delta is printed at the end of every scan and included in `summary.md`.

---

## Stack

| Component          | Technology          |
|--------------------|---------------------|
| CLI                | Python + Typer      |
| Browser Automation | Playwright (async)  |
| AI Reasoning       | Gemini 2.5 Pro      |
| Regression Memory  | SQLite              |
| Terminal UI        | Rich                |
| Data Models        | Pydantic            |

---

## Requirements

- Python 3.11 or higher
- Free Gemini API key from aistudio.google.com
- Chromium -- installed automatically via `playwright install chromium`

---

## Roadmap

- [ ] Visual diff engine -- compare screenshots across scans
- [ ] GitHub Actions integration -- run on every pull request
- [ ] Slack and Discord alert webhooks
- [ ] Parallel multi-page scanning
- [ ] HTML report with embedded screenshots and video
- [ ] Custom success criteria per user journey

---

## Contributing

Pull requests are welcome.

If Phantom caught a real bug in your app that would have reached production, open an issue and describe it. Those are the most valuable contributions.

---

## License

MIT -- use it, modify it, ship it.

---

```
  Built with zero patience for manual QA
  github.com/yourname/phantom-qa
```
