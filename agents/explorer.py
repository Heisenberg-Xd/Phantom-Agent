"""
Explorer agent — Phase 2.
Executes user journeys adversarially with edge case fuzzing.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from browser.runner import PhantomBrowser, PageCapture

logger = logging.getLogger("phantom")

EDGE_CASES_PATH = Path("config/edge_cases.yaml")
SCREENSHOTS_DIR = Path("reports/screenshots")


@dataclass
class FlaggedEvent:
    """A potential bug detected during exploration."""
    event_type: str          # http_error | console_error | slow_page | element_missing | blank_page | invalid_input_accepted
    severity_hint: str       # critical | high | medium | low
    url: str
    description: str
    screenshot_path: str
    steps_taken: list[str] = field(default_factory=list)
    network_status: Optional[int] = None
    console_message: Optional[str] = None
    load_time: Optional[float] = None
    edge_case_used: Optional[str] = None
    form_field: Optional[str] = None


class ExplorerAgent:
    """Adversarial exploration agent that stress-tests journeys."""

    def __init__(
        self,
        browser: PhantomBrowser,
        app_model: dict,
        progress_callback=None,
        bug_counter_callback=None,
    ):
        self.browser = browser
        self.app_model = app_model
        self.progress_callback = progress_callback
        self.bug_counter_callback = bug_counter_callback
        self.flagged_events: list[FlaggedEvent] = []
        self.bug_count = 0

        # Load edge cases
        self.edge_cases = self._load_edge_cases()
        self.perf_threshold = self.edge_cases.get("performance_thresholds", {}).get(
            "page_load_max_seconds", 10.0
        )
        self._first_load_done = False
        self.error_codes = set(
            self.edge_cases.get("bug_detection_rules", {}).get("http_error_codes", [])
        )
        self.suspicious_keywords = self.edge_cases.get("bug_detection_rules", {}).get(
            "suspicious_console_keywords", []
        )

    def _load_edge_cases(self) -> dict:
        """Load edge case library from YAML."""
        try:
            return yaml.safe_load(EDGE_CASES_PATH.read_text())
        except Exception as e:
            logger.error(f"Failed to load edge_cases.yaml: {e}")
            return {}

    async def run(self) -> list[FlaggedEvent]:
        import asyncio
        EXPLORER_TIMEOUT = 90
        try:
            await asyncio.wait_for(
                self._run_exploration(),
                timeout=EXPLORER_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.info("Explorer timeout — moving to validation")
        return self.flagged_events

    async def _run_exploration(self) -> list[FlaggedEvent]:
        """Execute all journeys adversarially."""
        journeys = self.app_model.get("journeys", [])
        base_url = self.app_model.get("base_url", "")
        graph_nodes = self.app_model.get("interaction_graph", {}).get("nodes", [])

        logger.info(f"Explorer: {len(journeys)} journeys, {len(graph_nodes)} pages")

        # 1. Navigate every discovered page
        for node in graph_nodes:
            url = node.get("url", "")
            if url:
                await self._probe_page(url)

        # 2. Execute each journey
        for i, journey in enumerate(journeys):
            name = journey.get("name", f"Journey {i+1}")
            if self.progress_callback:
                self.progress_callback(f"[{i+1}/{len(journeys)}] {name}")
            logger.info(f"Executing journey: {name}")
            await self._execute_journey(journey, base_url)

        # 3. Fuzz all forms found in graph
        tested_pages = set()
        for node in graph_nodes:
            url = node.get("url", "")
            forms = node.get("forms", [])
            if url and forms and url not in tested_pages:
                tested_pages.add(url)
                await self._fuzz_forms_on_page(url, forms)

        logger.info(
            f"Explorer complete. {len(self.flagged_events)} events flagged."
        )
        return self.flagged_events

    async def _probe_page(self, url: str):
        """Navigate to a page and check for basic issues."""
        try:
            if self.progress_callback:
                self.progress_callback(f"Probing: {url[:60]}")

            capture = await self.browser.navigate(url, timeout=15000, take_screenshot=False)

            # Check load time
            if capture.load_time_seconds > self.perf_threshold and self._first_load_done:
                await self._flag(
                    FlaggedEvent(
                        event_type="slow_page",
                        severity_hint="medium",
                        url=url,
                        description=(
                            f"Page loaded in {capture.load_time_seconds:.1f}s "
                            f"(threshold: {self.perf_threshold}s)"
                        ),
                        screenshot_path=capture.screenshot_path,
                        load_time=capture.load_time_seconds,
                        steps_taken=[f"Navigate to {url}"],
                    )
                )

            # Mark first load as done after the very first check (or navigation)
            self._first_load_done = True

            # Check HTTP errors in network
            for ne in capture.network_events:
                if ne.status in self.error_codes:
                    await self._flag(
                        FlaggedEvent(
                            event_type="http_error",
                            severity_hint="high" if ne.status >= 500 else "medium",
                            url=url,
                            description=f"HTTP {ne.status} on {ne.method} {ne.url}",
                            screenshot_path=capture.screenshot_path,
                            network_status=ne.status,
                            steps_taken=[f"Navigate to {url}"],
                        )
                    )

            # Check console errors
            for ce in capture.console_events:
                if ce.level == "error" or any(
                    kw.lower() in ce.text.lower() for kw in self.suspicious_keywords
                ):
                    await self._flag(
                        FlaggedEvent(
                            event_type="console_error",
                            severity_hint="medium",
                            url=url,
                            description=f"Console {ce.level}: {ce.text[:200]}",
                            screenshot_path=capture.screenshot_path,
                            console_message=ce.text,
                            steps_taken=[f"Navigate to {url}"],
                        )
                    )

            # Check blank page
            if await self.browser.is_blank_page():
                await self._flag(
                    FlaggedEvent(
                        event_type="blank_page",
                        severity_hint="critical",
                        url=url,
                        description="Page appears blank or has no visible content",
                        screenshot_path=capture.screenshot_path,
                        steps_taken=[f"Navigate to {url}"],
                    )
                )

        except Exception as e:
            logger.error(f"_probe_page error for {url}: {e}")

    async def _execute_journey(self, journey: dict, base_url: str):
        """Execute a single user journey end-to-end."""
        steps = journey.get("steps", [])
        pages = journey.get("pages_involved", [base_url])
        name = journey.get("name", "Unknown Journey")

        steps_taken = [f"Starting journey: {name}"]

        try:
            # Navigate to first page of journey
            start_url = pages[0] if pages else base_url
            capture = await self.browser.navigate(start_url, timeout=20000, take_screenshot=False)
            steps_taken.append(f"Navigated to {start_url}")

            for step in steps:
                # Formulate log strings whether it's dict or string
                if isinstance(step, dict):
                    action = step.get('action', '')
                    selector = step.get('selector', '')
                    value = step.get('value', '')
                    steps_taken.append(f"Step: {action} on '{selector}' (value: {value})")
                else:
                    steps_taken.append(f"Step: {step}")

                try:
                    if isinstance(step, dict):
                        action = step.get('action', '').lower()
                        selector = step.get('selector', '')
                        value = step.get('value', '')

                        if action in ["click", "tap", "select"]:
                            await self.browser.page.click(selector, timeout=3000)
                            await self.browser.wait_for_navigation(2000)
                        elif action in ["fill", "type", "input"]:
                            await self.browser.page.fill(selector, value)
                        elif action in ["press"]:
                            await self.browser.page.press(selector, value)
                    else:
                        # Fallback for old string-based format if any
                        step_lower = step.lower()
                        if any(w in step_lower for w in ["click", "press", "tap", "select"]):
                            words = [w for w in step.split() if len(w) > 3 and w.isalpha()]
                            for word in words:
                                try:
                                    await self.browser.page.click(f"text={word}", timeout=3000)
                                    await self.browser.wait_for_navigation(2000)
                                    break
                                except Exception:
                                    pass
                        elif any(w in step_lower for w in ["type", "enter", "fill", "input"]):
                            try:
                                inputs = await self.browser.page.query_selector_all(
                                    "input:not([type='hidden']):not([type='submit']), textarea"
                                )
                                if inputs:
                                    await inputs[0].fill("test@example.com")
                            except Exception:
                                pass

                    await asyncio.sleep(0.5)

                    # Check for issues after each step
                    capture = await self.browser.navigate(self.browser.page.url, timeout=3000, take_screenshot=False)
                    for ne in capture.network_events:
                        if ne.status in self.error_codes:
                            await self._flag(
                                FlaggedEvent(
                                    event_type="http_error",
                                    severity_hint="high" if ne.status >= 500 else "medium",
                                    url=self.browser.page.url,
                                    description=f"HTTP {ne.status} during journey step: {step}",
                                    screenshot_path=capture.screenshot_path,
                                    network_status=ne.status,
                                    steps_taken=list(steps_taken),
                                )
                            )

                except Exception as e:
                    logger.debug(f"Step execution error '{step}': {e}")

        except Exception as e:
            logger.error(f"Journey '{name}' execution error: {e}")

    async def _fuzz_forms_on_page(self, url: str, forms: list[dict]):
        """Fuzz all forms on a page with adversarial inputs."""
        string_attacks = self.edge_cases.get("string_attacks", [])
        numeric_attacks = self.edge_cases.get("numeric_attacks", [])
        date_attacks = self.edge_cases.get("date_attacks", [])

        # Select a subset of important attacks to avoid too many requests
        test_attacks = [
            a for a in string_attacks
            if a.get("name") in [
                "Empty submission",
                "SQL Injection - Classic OR",
                "XSS - Script Alert",
                "Max length string",
                "Unicode - Japanese",
                "Whitespace only",
            ]
        ] + [
            a for a in numeric_attacks
            if a.get("name") in ["Negative number", "Zero", "Very large number"]
        ]

        for form in forms[:3]:  # Limit to first 3 forms per page
            inputs = form.get("inputs", [])
            if not inputs:
                continue

            for attack in test_attacks[:8]:  # Limit attacks per form
                await self._attempt_form_attack(url, form, inputs, attack)

    async def _attempt_form_attack(
        self, url: str, form: dict, inputs: list[dict], attack: dict
    ):
        """Attempt a single attack on a form."""
        attack_name = attack.get("name", "Unknown")
        attack_value = attack.get("value", "")
        steps_taken = [
            f"Navigate to {url}",
            f"Attemping form attack: {attack_name}",
        ]

        try:
            # Re-navigate to get a fresh page state
            await self.browser.navigate(url, timeout=15000, take_screenshot=False)
            self.browser.clear_events()

            form_filled = False
            for inp in inputs:
                selector = inp.get("selector", "")
                inp_type = inp.get("type", "text")

                # Skip file inputs and submit buttons for string attacks
                if inp_type in ("file", "submit", "button", "reset"):
                    continue

                if not selector:
                    # Try by placeholder or name
                    placeholder = inp.get("placeholder", "")
                    name = inp.get("name", "")
                    if placeholder:
                        selector = f"[placeholder='{placeholder}']"
                    elif name:
                        selector = f"[name='{name}']"

                if selector:
                    filled = await self.browser.fill_input(selector, attack_value)
                    if filled:
                        form_filled = True
                        steps_taken.append(f"Filled '{selector}' with: {attack_name}")

            if not form_filled:
                # Try filling any visible input
                try:
                    await self.browser.page.fill(
                        "input:not([type='hidden']):not([type='submit'])",
                        attack_value,
                    )
                    form_filled = True
                    steps_taken.append(f"Filled first input with: {attack_name}")
                except Exception:
                    pass

            if not form_filled:
                return

            # Pre-submit state
            pre_url = self.browser.page.url
            pre_content = await self.browser.page.content()

            # Submit the form
            capture = await self.browser.submit_form(timeout=8000, take_screenshot=False)
            steps_taken.append("Submitted form")

            post_url = self.browser.page.url
            post_content = await self.browser.page.content()

            # Detect if invalid input was silently accepted
            url_changed = pre_url != post_url
            content_changed = len(post_content) != len(pre_content)
            no_error_shown = not any(
                kw in post_content.lower()
                for kw in ["error", "invalid", "required", "warning", "validation"]
            )

            if (url_changed or content_changed) and no_error_shown:
                # Potentially accepted bad input silently
                if attack.get("category") == "security":
                    await self._flag(
                        FlaggedEvent(
                            event_type="invalid_input_accepted",
                            severity_hint="high",
                            url=url,
                            description=(
                                f"Form accepted security attack silently: {attack_name} "
                                f"with value: {str(attack_value)[:60]}"
                            ),
                            screenshot_path=capture.screenshot_path,
                            steps_taken=list(steps_taken),
                            edge_case_used=attack_name,
                        )
                    )
                elif attack.get("name") == "Empty submission":
                    await self._flag(
                        FlaggedEvent(
                            event_type="invalid_input_accepted",
                            severity_hint="medium",
                            url=url,
                            description="Form accepted empty submission without validation",
                            screenshot_path=capture.screenshot_path,
                            steps_taken=list(steps_taken),
                            edge_case_used=attack_name,
                        )
                    )

            # Check for HTTP errors after submission
            for ne in capture.network_events:
                if ne.status in self.error_codes:
                    await self._flag(
                        FlaggedEvent(
                            event_type="http_error",
                            severity_hint="critical" if ne.status >= 500 else "high",
                            url=url,
                            description=(
                                f"HTTP {ne.status} after form attack: {attack_name}"
                            ),
                            screenshot_path=capture.screenshot_path,
                            network_status=ne.status,
                            steps_taken=list(steps_taken),
                            edge_case_used=attack_name,
                        )
                    )

            # Check for JS errors
            for ce in capture.console_events:
                if ce.level == "error" or any(
                    kw.lower() in ce.text.lower() for kw in self.suspicious_keywords
                ):
                    await self._flag(
                        FlaggedEvent(
                            event_type="console_error",
                            severity_hint="medium",
                            url=url,
                            description=f"Console error after attack '{attack_name}': {ce.text[:150]}",
                            screenshot_path=capture.screenshot_path,
                            console_message=ce.text,
                            steps_taken=list(steps_taken),
                            edge_case_used=attack_name,
                        )
                    )

        except Exception as e:
            logger.debug(f"Form attack error ({attack_name} on {url}): {e}")

    async def _flag(self, event: FlaggedEvent):
        """Add a flagged event and notify callbacks."""
        # Deduplicate similar events
        for existing in self.flagged_events:
            if (
                existing.event_type == event.event_type
                and existing.url == event.url
                and existing.description[:80] == event.description[:80]
            ):
                return

        # Take screenshot ONLY on bug detection!
        if not event.screenshot_path:
            import datetime
            import re
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_url = re.sub(r"[^\w]", "_", event.url)[:60]
            screenshot_path_obj = self.browser.screenshots_dir / f"bug_{timestamp}_{safe_url}.png"
            try:
                await self.browser.page.screenshot(path=str(screenshot_path_obj), full_page=True)
                event.screenshot_path = str(screenshot_path_obj)
            except Exception:
                pass
                
        self.flagged_events.append(event)
        self.bug_count += 1
        logger.warning(
            f"🚨 BUG FLAGGED [{event.severity_hint.upper()}] "
            f"{event.event_type}: {event.description[:80]}"
        )
        if self.bug_counter_callback:
            self.bug_counter_callback(self.bug_count, event)
