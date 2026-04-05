"""
Validator agent — Phase 3.
Sends flagged events to Gemini 2.5 Pro to generate structured bug reports.
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

from agents.explorer import FlaggedEvent

logger = logging.getLogger("phantom")

REPORTS_DIR = Path("reports")

# ── Gemini client ─────────────────────────────────────────────────────────────

def _make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-1.5-flash"
WAIT_TIMES = [2, 4, 8, 16, 32]


class ValidatorAgent:
    """Converts flagged events into structured bug reports using Gemini."""

    def __init__(self, screenshots_dir: Path = Path("reports/screenshots")):
        self._client = _make_client()
        self.screenshots_dir = screenshots_dir
        self.reports_dir = screenshots_dir.parent
        self.bug_reports: list[dict] = []

    async def validate_all(
        self, events: list[FlaggedEvent], progress_callback=None
    ) -> list[dict]:
        """Process all flagged events into bug reports sequentially."""
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        for i, event in enumerate(events):
            if progress_callback:
                progress_callback(
                    f"Validating [{i+1}/{len(events)}]: {event.description[:50]}"
                )

            report = await self._generate_bug_report(event)
            if report:
                self.bug_reports.append(report)
                await self._save_bug_report(report)

            # 4-second rate-limit buffer between every API call
            await asyncio.sleep(4)

        return self.bug_reports

    async def _generate_bug_report(self, event: FlaggedEvent) -> Optional[dict]:
        """Call Gemini to generate one structured bug report."""
        # Load screenshot as base64 if available
        screenshot_b64 = ""
        try:
            if event.screenshot_path and Path(event.screenshot_path).exists():
                screenshot_b64 = base64.b64encode(
                    Path(event.screenshot_path).read_bytes()
                ).decode("utf-8")
        except Exception as e:
            logger.debug(f"Screenshot load failed: {e}")

        steps_str = "\n".join(
            f"{j+1}. {s}" for j, s in enumerate(event.steps_taken)
        )

        event_details = f"""Event Type: {event.event_type}
URL: {event.url}
Description: {event.description}
Severity Hint: {event.severity_hint}
Steps Taken:
{steps_str}
HTTP Status: {event.network_status or 'N/A'}
Console Message: {event.console_message or 'N/A'}
Page Load Time: {f'{event.load_time:.2f}s' if event.load_time else 'N/A'}
Edge Case Used: {event.edge_case_used or 'N/A'}"""

        prompt = f"""You are a senior QA engineer writing bug reports. For the bug described, write a suggested_fix that is:
- Specific to this exact bug type
- Actionable in 1-2 sentences
- Technical and precise

Examples of good fixes:
- 'Add server-side input sanitization using a whitelist approach. Reject inputs containing SQL metacharacters.'
- 'Implement rate limiting on the login endpoint. Add account lockout after 5 failed attempts.'
- 'Set Cache-Control headers to reduce repeat load times. Enable gzip compression on the server.'

Never write generic advice like 'investigate' or 'add error handling'.

Here is what happened during automated testing:
{event_details}

{"A screenshot is included showing the issue." if screenshot_b64 else "No screenshot available."}

Write a structured bug report. Return JSON ONLY (no markdown, no explanation):
{{
  "title": "One-line bug title (concise and specific)",
  "severity": "critical|high|medium|low",
  "steps_to_reproduce": ["Step 1", "Step 2", "Step 3"],
  "expected_behavior": "What should have happened",
  "actual_behavior": "What actually happened",
  "suggested_fix": "Technical recommendation to fix this",
  "category": "security|performance|functionality|ux|validation",
  "affected_url": "{event.url}"
}}"""

        for attempt in range(5):
            try:
                logger.info(
                    f"Gemini bug report (attempt {attempt+1}/5): "
                    f"{event.description[:50]}"
                )

                loop = asyncio.get_event_loop()

                if screenshot_b64:
                    # Multimodal — attach screenshot as inline image part
                    contents = [
                        prompt,
                        types.Part.from_bytes(
                            data=base64.b64decode(screenshot_b64),
                            mime_type="image/png",
                        ),
                    ]
                    response = await loop.run_in_executor(
                        None,
                        lambda c=contents: self._client.models.generate_content(
                            model=MODEL, contents=c
                        ),
                    )
                else:
                    response = await loop.run_in_executor(
                        None,
                        lambda p=prompt: self._client.models.generate_content(
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

    def _enrich(self, parsed: dict, event: FlaggedEvent) -> dict:
        """Add metadata fields to the parsed report."""
        parsed["screenshot_path"] = event.screenshot_path
        parsed["raw_event_type"] = event.event_type
        parsed["timestamp"] = datetime.utcnow().isoformat()
        return parsed

    def _fallback_report(self, event: FlaggedEvent) -> dict:
        """Fallback report when Gemini is unavailable."""
        return {
            "title": f"[{event.event_type.upper()}] {event.description[:80]}",
            "severity": event.severity_hint,
            "steps_to_reproduce": event.steps_taken or ["Navigate to the affected URL"],
            "expected_behavior": "Application should handle this gracefully",
            "actual_behavior": event.description,
            "suggested_fix": "Investigate and add proper error handling",
            "category": "functionality",
            "affected_url": event.url,
            "screenshot_path": event.screenshot_path,
            "raw_event_type": event.event_type,
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
