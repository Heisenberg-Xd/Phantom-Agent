"""
Validator agent — Phase 3.
Converts flagged event dicts into structured bug reports using Gemini 1.5 Flash.
"""

import asyncio
import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger("phantom")

MODEL = "gemini-2.0-flash"
WAIT_TIMES = [2, 4, 8, 16, 32]


def _make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


class ValidatorAgent:
    """Converts flagged event dicts into structured bug reports using Gemini."""

    def __init__(self, screenshots_dir: Path = Path("reports/screenshots")):
        self.screenshots_dir = screenshots_dir
        self.reports_dir = screenshots_dir.parent
        self.bug_reports: list[dict] = []

    async def validate_all(self, events: list) -> list:
        """
        Process all flagged events into bug reports sequentially.
        events: list of dicts (from ExplorerAgent.explore())
        Returns: list of bug report dicts.
        """
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        for i, event in enumerate(events):
            try:
                report = await self._generate_bug_report(event)
                if report:
                    self.bug_reports.append(report)
                    await self._save_bug_report(report)
            except Exception as e:
                logger.error(f"validate_all event {i} error: {e}")

            # 4-second rate-limit buffer between every API call
            await asyncio.sleep(4)

        return self.bug_reports

    async def _generate_bug_report(self, event: dict) -> Optional[dict]:
        """Call Gemini 1.5 Flash to generate one structured bug report."""
        event_type = event.get("event_type", "unknown")
        url = event.get("url", "")
        description = event.get("description", "")
        severity_hint = event.get("severity_hint", "medium")
        steps_taken = event.get("steps_taken", [])
        network_status = event.get("network_status")
        console_message = event.get("console_message")
        load_time = event.get("load_time")
        edge_case = event.get("edge_case_used")
        screenshot_path = event.get("screenshot_path", "")

        # Load screenshot as base64 if available
        screenshot_b64 = ""
        try:
            if screenshot_path and Path(screenshot_path).exists():
                screenshot_b64 = base64.b64encode(
                    Path(screenshot_path).read_bytes()
                ).decode("utf-8")
        except Exception as e:
            logger.debug(f"Screenshot load failed: {e}")

        steps_str = "\n".join(f"{j+1}. {s}" for j, s in enumerate(steps_taken))

        event_details = f"""Event Type: {event_type}
URL: {url}
Description: {description}
Severity Hint: {severity_hint}
Steps Taken:
{steps_str}
HTTP Status: {network_status or 'N/A'}
Console Message: {console_message or 'N/A'}
Page Load Time: {f'{load_time:.2f}s' if load_time else 'N/A'}
Edge Case Used: {edge_case or 'N/A'}"""

        # ── Specific fix prompt per bug type ──────────────────────────────────
        fix_prompt_instructions = f"""
Bug type: {event_type}
Actual behavior: {description}
Affected URL: {url}

Write a suggested fix that is:
- Specific to THIS exact bug
- 2-3 sentences maximum
- Technical and actionable
- Names the specific file type, function, or config that needs changing

Good examples:
"Add input sanitization in the form handler. Use DOMPurify on the client and parameterized queries on the server."
"Set session.invalidate() on logout in the auth controller. Verify the session cookie is cleared in the response headers."

Bad examples (never write these):
"Investigate and add proper error handling"
"This should be fixed by the development team"
"Add appropriate validation"

Return only the fix text. No preamble.
"""

        prompt = f"""You are a senior QA engineer writing bug reports.

Here is what happened during automated testing:
{event_details}

{"A screenshot is included showing the issue." if screenshot_b64 else "No screenshot available."}

First, in your internal reasoning, write the suggested_fix following these exact instructions:
{fix_prompt_instructions}

Then return a structured bug report as JSON ONLY (no markdown, no explanation):
{{
  "title": "One-line bug title (concise and specific)",
  "severity": "critical|high|medium|low",
  "steps_to_reproduce": ["Step 1", "Step 2", "Step 3"],
  "expected_behavior": "What should have happened",
  "actual_behavior": "What actually happened",
  "suggested_fix": "Technical recommendation to fix this bug specifically",
  "category": "security|performance|functionality|ux|validation",
  "affected_url": "{url}"
}}"""

        for attempt in range(5):
            try:
                logger.info(
                    f"Gemini bug report (attempt {attempt+1}/5): "
                    f"{description[:50]}"
                )

                loop = asyncio.get_event_loop()
                client = _make_client()

                if screenshot_b64:
                    contents = [
                        prompt,
                        types.Part.from_bytes(
                            data=base64.b64decode(screenshot_b64),
                            mime_type="image/png",
                        ),
                    ]
                    response = await loop.run_in_executor(
                        None,
                        lambda c=contents: client.models.generate_content(
                            model=MODEL, contents=c
                        ),
                    )
                else:
                    response = await loop.run_in_executor(
                        None,
                        lambda p=prompt: client.models.generate_content(
                            model=MODEL, contents=p
                        ),
                    )

                text = response.text.strip()

                # Strip markdown fences if present
                if "```" in text:
                    for part in text.split("```"):
                        part = part.strip().lstrip("json").strip()
                        try:
                            parsed = json.loads(part)
                            return self._enrich(parsed, event)
                        except json.JSONDecodeError:
                            continue
                else:
                    parsed = json.loads(text)
                    return self._enrich(parsed, event)

            except json.JSONDecodeError as e:
                logger.warning(f"Gemini invalid JSON in bug report (attempt {attempt+1}): {e}")
                return self._fallback_report(event)

            except Exception as e:
                wait = WAIT_TIMES[min(attempt, len(WAIT_TIMES) - 1)]
                logger.warning(
                    f"Gemini API error in validator (attempt {attempt+1}/5): {e}. "
                    f"Retrying in {wait}s..."
                )
                if attempt < 4:
                    await asyncio.sleep(wait)
                else:
                    logger.error("Gemini validator failed after all retries.")
                    return self._fallback_report(event)

        return self._fallback_report(event)

    def _enrich(self, parsed: dict, event: dict) -> dict:
        """Add metadata fields to the parsed report."""
        parsed["screenshot_path"] = event.get("screenshot_path", "")
        parsed["raw_event_type"] = event.get("event_type", "unknown")
        parsed["timestamp"] = datetime.utcnow().isoformat()
        parsed.setdefault("affected_url", event.get("url", ""))
        return parsed

    def _fallback_report(self, event: dict) -> dict:
        """Fallback report when Gemini is unavailable."""
        event_type = event.get("event_type", "unknown")
        description = event.get("description", "")
        url = event.get("url", "")

        # Generate specific fallback fix based on event type
        fallback_fix_map = {
            "http_error": "Check server logs for the 5xx error. Fix the handler that returns this status code and add proper error recovery.",
            "console_error": "Open the browser console and trace the JavaScript exception. Fix the undefined variable or missing function reference.",
            "invalid_input": "Add server-side input validation. Reject dangerous payloads (SQL, XSS, executables) before processing.",
            "slow_page": "Profile the slow resource with Chrome DevTools. Add caching headers and optimize the largest contentful paint.",
            "auth_bypass": "Add authentication middleware to the route. Verify the session token is validated on every protected request.",
        }
        fix = fallback_fix_map.get(event_type, "Check the affected endpoint and add appropriate server-side validation and error handling.")

        return {
            "title": f"[{event_type.upper()}] {description[:80]}",
            "severity": event.get("severity_hint", "medium"),
            "steps_to_reproduce": event.get("steps_taken") or ["Navigate to the affected URL"],
            "expected_behavior": "Application should handle this request without error",
            "actual_behavior": description,
            "suggested_fix": fix,
            "category": "functionality",
            "affected_url": url,
            "screenshot_path": event.get("screenshot_path", ""),
            "raw_event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
        }

    async def _save_bug_report(self, report: dict):
        """Save individual bug report as a JSON file."""
        try:
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            title_safe = "".join(
                c if c.isalnum() or c in "-_ " else "_"
                for c in report.get("title", "bug")[:50]
            ).strip().replace(" ", "_")
            filename = f"{timestamp}_{title_safe}.json"
            path = self.reports_dir / filename
            path.write_text(json.dumps(report, indent=2))
            logger.info(f"Bug report saved: {path.name}")
        except Exception as e:
            logger.error(f"Failed to save bug report: {e}")
