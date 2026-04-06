"""
Explorer agent — Phase 2.
Executes user journeys with real element finding, login handling,
file upload testing, and strict false-positive filtering.
"""

import asyncio
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from browser.runner import PhantomBrowser

logger = logging.getLogger("phantom")


@dataclass
class FlaggedEvent:
    """A potential bug detected during exploration."""
    event_type: str
    severity_hint: str
    url: str
    description: str
    screenshot_path: str
    steps_taken: list = field(default_factory=list)
    network_status: Optional[int] = None
    console_message: Optional[str] = None
    load_time: Optional[float] = None
    edge_case_used: Optional[str] = None
    form_field: Optional[str] = None


# ── Real bug filters — no noise allowed ───────────────────────────────────────

JS_ERROR_KEYWORDS = [
    "uncaught", "typeerror", "referenceerror",
    "syntaxerror", "is not defined",
    "cannot read", "is not a function",
    "cannot set property", "unexpected token",
]


def _is_real_console_error(message: str) -> bool:
    """Only flag real JavaScript errors, not resource warnings or 3rd-party noise."""
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in JS_ERROR_KEYWORDS)


def _is_real_http_error(status: int, request_url: str, base_domain: str) -> bool:
    """Only flag 5xx errors on the BASE domain. External 4xx = not our bug."""
    if status < 500:
        return False
    try:
        req_netloc = urlparse(request_url).netloc
        return req_netloc == base_domain
    except Exception:
        return False


# ── Smart element finder ──────────────────────────────────────────────────────

async def find_element(page, hints: dict):
    """
    Multi-strategy element finder.
    hints = {"type": "input|button|link|file", "purpose": "...", "label": "..."}
    Returns the first visible locator, or None.
    """
    strategies = []
    purpose = hints.get("purpose", "")
    label = hints.get("label", "")

    if purpose == "login_email":
        strategies = [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="email" i]',
            'input[id*="email" i]',
            'input[type="text"]',
        ]
    elif purpose == "login_password":
        strategies = [
            'input[type="password"]',
            'input[name="password"]',
            'input[id*="password" i]',
        ]
    elif purpose == "submit":
        strategies = [
            'button[type="submit"]',
            'input[type="submit"]',
            f'button:has-text("{label}")',
            'button:last-of-type',
        ]
    elif purpose == "file_upload":
        strategies = [
            'input[type="file"]',
            '[data-testid*="upload"]',
            '.upload-area',
            '.dropzone',
        ]
    elif purpose == "search":
        strategies = [
            'input[type="search"]',
            'input[placeholder*="search" i]',
            'input[name="search"]',
            'input[name="q"]',
        ]

    for selector in strategies:
        try:
            el = page.locator(selector).first
            await el.wait_for(state="visible", timeout=3000)
            return el
        except Exception:
            continue

    return None


# ── Login flow handler ────────────────────────────────────────────────────────

async def handle_login(page, base_url: str, credentials: dict = None) -> bool:
    """
    Detect and handle login automatically.
    Returns True if login succeeded, False otherwise.
    """
    creds = credentials or {
        "email": "test@phantom.qa",
        "password": "Phantom@123!",
    }

    login_urls = [
        f"{base_url}/login",
        f"{base_url}/signin",
        f"{base_url}/auth",
        f"{base_url}/account/login",
    ]

    for login_url in login_urls:
        try:
            await asyncio.wait_for(
                page.goto(login_url, timeout=8000), timeout=10
            )
            await page.wait_for_timeout(800)

            email_field = await find_element(page, {"purpose": "login_email"})
            if not email_field:
                continue

            password_field = await find_element(page, {"purpose": "login_password"})
            if not password_field:
                continue

            await email_field.fill(creds["email"])
            await password_field.fill(creds["password"])

            submit = await find_element(page, {"purpose": "submit", "label": "Login"})
            if submit:
                await submit.click()
                await page.wait_for_timeout(2000)
                current = page.url
                if "login" not in current.lower() and "signin" not in current.lower():
                    logger.info(f"Login succeeded via {login_url}")
                    return True

        except Exception as e:
            logger.debug(f"Login attempt at {login_url} failed: {e}")
            continue

    # Try registration if login failed
    reg_urls = [
        f"{base_url}/register",
        f"{base_url}/signup",
        f"{base_url}/account/register",
    ]
    for reg_url in reg_urls:
        try:
            await asyncio.wait_for(
                page.goto(reg_url, timeout=8000), timeout=10
            )
            await page.wait_for_timeout(800)

            email_f = await find_element(page, {"purpose": "login_email"})
            pass_f = await find_element(page, {"purpose": "login_password"})

            if email_f and pass_f:
                await email_f.fill(creds["email"])
                await pass_f.fill(creds["password"])

                sub = await find_element(page, {"purpose": "submit", "label": "Register"})
                if not sub:
                    sub = await find_element(page, {"purpose": "submit", "label": "Sign Up"})
                if sub:
                    await sub.click()
                    await page.wait_for_timeout(2000)
                    logger.info(f"Registration attempt via {reg_url}")
                    return True
        except Exception as e:
            logger.debug(f"Registration attempt at {reg_url} failed: {e}")
            continue

    return False


# ── File upload tester ────────────────────────────────────────────────────────

async def test_file_upload(page, base_url: str) -> list:
    """Find and test file upload functionality. Returns list of flagged events."""
    test_files = {
        "valid_pdf": ("test.pdf", b"%PDF-1.4 test"),
        "valid_image": ("test.jpg", b"\xff\xd8\xff" + b"fake jpeg"),
        "wrong_type": ("malicious.exe", b"MZ fake executable"),
        "empty": ("empty.pdf", b""),
        "oversized": ("big.pdf", b"A" * 11_000_000),
    }

    upload_el = await find_element(page, {"purpose": "file_upload"})
    if not upload_el:
        return []

    events = []

    for file_type, (filename, content) in test_files.items():
        ext = os.path.splitext(filename)[1]
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(content)
                tmp_path = f.name

            await upload_el.set_input_files(tmp_path)
            await page.wait_for_timeout(2000)

            has_error = await page.locator(
                ".error, .alert-danger, [role=alert], .alert"
            ).count() > 0

            if file_type == "wrong_type" and not has_error:
                events.append(FlaggedEvent(
                    event_type="invalid_input",
                    severity_hint="high",
                    url=page.url,
                    description=f"App accepted {filename} (executable) without error",
                    screenshot_path="",
                    steps_taken=[f"Upload {filename} to {page.url}"],
                    edge_case_used="wrong_file_type",
                ))

            if file_type == "oversized" and not has_error:
                events.append(FlaggedEvent(
                    event_type="invalid_input",
                    severity_hint="medium",
                    url=page.url,
                    description="11MB file accepted without any size validation error",
                    screenshot_path="",
                    steps_taken=[f"Upload oversized file (11MB) to {page.url}"],
                    edge_case_used="oversized_file",
                ))

        except Exception as e:
            logger.debug(f"File upload test ({file_type}) failed: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    return events


# ── Explorer agent ────────────────────────────────────────────────────────────

class ExplorerAgent:
    """
    Adversarial exploration agent. Accepts pages list directly — no app_model.
    Applies real-bug filters before flagging any event.
    """

    def __init__(
        self,
        base_url: str,
        pages: list,
        journeys: list,
        screenshots_dir: Path,
        headless: bool = True,
        credentials: dict = None,
        login_steps: list = None,
    ):
        self.base_url = base_url
        self.base_domain = urlparse(base_url).netloc
        self.pages = pages
        self.journeys = journeys
        self.screenshots_dir = screenshots_dir
        self.headless = headless
        self.credentials = credentials
        self.login_steps = login_steps
        self.flagged_events: list[FlaggedEvent] = []

    async def explore(self) -> list:
        """
        Main explore method. Returns list of FlaggedEvent dicts.
        Wrapped in timeout — never blocks the pipeline.
        """
        try:
            await asyncio.wait_for(self._run_exploration(), timeout=240)
        except asyncio.TimeoutError:
            logger.info("Explorer timeout — moving to validation")
        except Exception as e:
            logger.error(f"Explorer error: {e}")

        # Convert dataclasses to dicts for pipeline compatibility
        return [self._event_to_dict(e) for e in self.flagged_events]

    def _event_to_dict(self, event: FlaggedEvent) -> dict:
        return {
            "event_type": event.event_type,
            "severity_hint": event.severity_hint,
            "url": event.url,
            "description": event.description,
            "screenshot_path": event.screenshot_path,
            "steps_taken": event.steps_taken,
            "network_status": event.network_status,
            "console_message": event.console_message,
            "load_time": event.load_time,
            "edge_case_used": event.edge_case_used,
        }

    async def _run_exploration(self):
        """Run all exploration phases."""
        async with PhantomBrowser(self.screenshots_dir, headless=self.headless) as browser:
            if self.login_steps:
                await browser.execute_login(self.login_steps)

            # Phase A: probe every discovered page FIRST (always runs)
            logger.info(f"Explorer probing {len(self.pages)} pages...")
            for page_info in self.pages:
                url = page_info.get("url", "")
                if url:
                    await self._probe_page(browser, url)

            # Phase B: attempt login if login page detected (runs after probing unless login_steps was used)
            if not self.login_steps:
                login_pages = [
                    p for p in self.pages
                    if any(kw in p.get("url", "").lower() for kw in ["login", "signin", "auth"])
                    or any(kw in p.get("title", "").lower() for kw in ["login", "sign in"])
                ]
                logged_in = False
                if login_pages or self.credentials:
                    logger.info("Login page detected — attempting login (max 30s)...")
                    try:
                        logged_in = await asyncio.wait_for(
                            handle_login(browser.page, self.base_url, self.credentials),
                            timeout=30,
                        )
                        logger.info(f"Login result: {'success' if logged_in else 'failed/not available'}")
                    except asyncio.TimeoutError:
                        logger.info("Login handler timed out after 30s")
                    except Exception as e:
                        logger.error(f"Login handler error: {e}")

            # The journeys will be loaded from app_model.json after edge case testing

            # Phase D: form fuzzing on pages with forms
            logger.info("Fuzzing forms...")
            for page_info in self.pages[:5]:  # limit to first 5 pages
                url = page_info.get("url", "")
                forms = page_info.get("forms", [])
                if url and forms:
                    try:
                        await self._fuzz_forms(browser, url, forms)
                    except Exception as e:
                        logger.error(f"Form fuzz error on {url}: {e}")

            # Phase E: file upload testing
            logger.info("Testing file uploads...")
            for page_info in self.pages:
                url = page_info.get("url", "")
                has_file_input = any(
                    inp.get("type") == "file"
                    for form in page_info.get("forms", [])
                    for inp in form.get("inputs", [])
                )
                if has_file_input and url:
                    try:
                        await browser.navigate(url, timeout=15000)
                        upload_events = await test_file_upload(browser.page, self.base_url)
                        for evt in upload_events:
                            await self._flag(browser, evt)
                    except Exception as e:
                        logger.error(f"File upload test error on {url}: {e}")

            # execute journeys
            logger.info(f"Executing {len(self.journeys)} journeys...")
            for journey in self.journeys:
                result = await self._execute_journey(browser, journey)
                if result and result.get("failed"):
                    journey["failed"] = True
                    await self._flag(browser, FlaggedEvent(
                        event_type="journey_failure",
                        severity_hint="medium",
                        url=result.get("url", ""),
                        description=f"Journey '{journey.get('name', 'Unknown')}' failed at step: {result.get('failed_step')}",
                        screenshot_path=result.get("screenshot", ""),
                        steps_taken=[f"Executing journey '{journey.get('name', 'Unknown')}'", f"Failed step: {result.get('failed_step')}"]
                    ))
                else:
                    journey["failed"] = False

        logger.info(f"Explorer complete. {len(self.flagged_events)} events flagged.")

    async def _probe_page(self, browser: PhantomBrowser, url: str):
        """Navigate to page and check for real bugs only."""
        try:
            capture = await browser.navigate(url, timeout=15000, take_screenshot=False)

            # Check HTTP errors — 5xx on base domain = server error
            # 401 on base domain = missing auth protection (auth_bypass)
            for ne in capture.network_events:
                if _is_real_http_error(ne.status, ne.url, self.base_domain):
                    await self._flag(browser, FlaggedEvent(
                        event_type="http_error",
                        severity_hint="high",
                        url=url,
                        description=f"HTTP {ne.status} server error on {ne.method} {ne.url}",
                        screenshot_path=capture.screenshot_path,
                        network_status=ne.status,
                        steps_taken=[f"Navigate to {url}"],
                    ))
                # Flag 401 on base domain — endpoint needs auth but may be misconfigured
                elif ne.status == 401:
                    try:
                        ne_netloc = urlparse(ne.url).netloc
                    except Exception:
                        ne_netloc = ""
                    if ne_netloc == self.base_domain:
                        await self._flag(browser, FlaggedEvent(
                            event_type="auth_bypass",
                            severity_hint="high",
                            url=url,
                            description=f"HTTP 401 Unauthorized on {ne.url} — authentication required but credentials may not be enforced",
                            screenshot_path=capture.screenshot_path,
                            network_status=401,
                            steps_taken=[f"Navigate to {url}"],
                        ))

            # Check console errors — only real JS errors
            seen_msgs = set()
            for ce in capture.console_events:
                if ce.level == "error" and _is_real_console_error(ce.text):
                    msg_key = ce.text[:80]
                    if msg_key not in seen_msgs:
                        seen_msgs.add(msg_key)
                        await self._flag(browser, FlaggedEvent(
                            event_type="console_error",
                            severity_hint="medium",
                            url=url,
                            description=f"JavaScript error: {ce.text[:200]}",
                            screenshot_path=capture.screenshot_path,
                            console_message=ce.text,
                            steps_taken=[f"Navigate to {url}"],
                        ))

            # Slow page — only if > 10s (rare, legitimate issue)
            if capture.load_time_seconds > 10:
                await self._flag(browser, FlaggedEvent(
                    event_type="slow_page",
                    severity_hint="medium",
                    url=url,
                    description=f"Page loaded in {capture.load_time_seconds:.1f}s (>10s threshold)",
                    screenshot_path=capture.screenshot_path,
                    load_time=capture.load_time_seconds,
                    steps_taken=[f"Navigate to {url}"],
                ))

        except Exception as e:
            logger.error(f"_probe_page error for {url}: {e}")

async def journey_login(page):
    await page.goto("https://demo.testfire.net/login.jsp")
    await page.fill("#uid", "admin")
    await page.fill("#passw", "admin")
    await page.click("[type=submit]")
    await page.wait_for_timeout(2000)
    
    # Verify login succeeded
    current_url = page.url
    if "main.jsp" in current_url or "bank" in current_url:
        return {"status": "PASSED", "reached": current_url}
    else:
        return {"status": "FAILED", 
                "reason": "Did not reach dashboard after login"}

async def journey_view_account(page):
    # Login first
    await journey_login(page)
    
    # Navigate to account
    await page.goto(
        "https://demo.testfire.net/bank/main.jsp"
    )
    await page.wait_for_timeout(1000)
    
    content = await page.content()
    if "account" in content.lower():
        return {"status": "PASSED"}
    return {"status": "FAILED", 
            "reason": "Account page not accessible"}

async def journey_transfer(page):
    await journey_login(page)
    await page.goto(
        "https://demo.testfire.net/bank/transfer.jsp"
    )
    await page.wait_for_timeout(1000)
    
    # Try a transfer
    try:
        await page.select_option(
            "select[name='fromAccount']", index=0
        )
        await page.select_option(
            "select[name='toAccount']", index=1
        )
        await page.fill(
            "input[name='transferAmount']", "100"
        )
        await page.click("[type=submit]")
        await page.wait_for_timeout(2000)
        
        content = await page.content()
        if "error" in content.lower():
            return {"status": "FAILED", 
                    "reason": "Transfer returned error"}
        return {"status": "PASSED"}
    except Exception as e:
        return {"status": "FAILED", "reason": str(e)}

    async def _execute_journey(self, browser: PhantomBrowser, journey: dict) -> dict:
        """Execute a single user journey using smart element finder."""
        hardcoded = journey.get("hardcoded")
        if hardcoded == "journey_login":
            return await journey_login(browser.page)
        elif hardcoded == "journey_view_account":
            return await journey_view_account(browser.page)
        elif hardcoded == "journey_transfer":
            return await journey_transfer(browser.page)

        steps = journey.get("steps", [])
        pages_involved = journey.get("pages_involved", [self.base_url])
        name = journey.get("name", "Unknown Journey")
        steps_taken = [f"Starting journey: {name}"]

        try:
            start_url = pages_involved[0] if pages_involved else self.base_url
            await browser.navigate(start_url, timeout=20000, take_screenshot=False)
            steps_taken.append(f"Navigated to {start_url}")
        except Exception as e:
            logger.debug(f"Journey start nav failed: {e}")
            return {"failed": True, "failed_step": f"Navigating to {start_url}", "url": self.base_url, "screenshot": ""}

        for step in steps:
            if not isinstance(step, dict):
                continue

            action = step.get("action", "").lower()
            selector = step.get("selector", "")
            value = step.get("value", "")
            steps_taken.append(f"Step: {action} on '{selector}'")

            try:
                if action in ("fill", "type", "input"):
                    try:
                        await browser.page.fill(selector, value, timeout=3000)
                    except Exception:
                        # Try smart finder as fallback
                        el = await find_element(browser.page, {"purpose": "login_email"})
                        if el:
                            await el.fill(value)

                        else:
                            raise Exception("Element not found via smart finder")

                elif action in ("click", "tap"):
                    try:
                        await browser.page.click(selector, timeout=3000)
                    except Exception:
                        # Try visible text click
                        try:
                            await browser.page.click(f"text={value}", timeout=2000)
                        except Exception:
                            raise Exception("Element not found for click")
                    await browser.wait_for_navigation(timeout=2000)

                elif action == "press":
                    try:
                        await browser.page.press(selector, value, timeout=3000)
                    except Exception:
                        raise Exception("Element not found for press")

                await asyncio.sleep(0.3)

            except Exception as e:
                logger.debug(f"Journey step error ({action} on {selector}): {e}")
                
                # capture screenshot of failure
                import datetime as _dt
                ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                shot_path = str(self.screenshots_dir / f"journey_fail_{ts}.png")
                try:
                    await browser.page.screenshot(path=shot_path, full_page=True)
                except Exception:
                    shot_path = ""
                
                return {
                    "failed": True,
                    "failed_step": f"{action} on '{selector}'",
                    "url": browser.page.url,
                    "screenshot": shot_path,
                    "reason": str(e)
                }
                
        return {"status": "PASSED"}

    async def _fuzz_forms(self, browser: PhantomBrowser, url: str, forms: list):
        """Fuzz forms with security-relevant inputs only."""
        security_payloads = [
            {"name": "SQL Injection", "value": "' OR '1'='1", "category": "security", "input_type": "sql_injection"},
            {"name": "XSS", "value": "<script>alert('xss')</script>", "category": "security", "input_type": "xss"},
            {"name": "Path Traversal", "value": "../../../../etc/passwd", "category": "security", "input_type": "path_traversal"},
            {"name": "Empty submission", "value": "", "category": "validation", "input_type": "empty"},
        ]

        for form in forms[:2]:  # limit to 2 forms per page
            inputs = form.get("inputs", [])
            if not inputs:
                continue

            fillable = [
                i for i in inputs
                if i.get("type") not in ("file", "submit", "button", "reset", "hidden")
            ]
            if not fillable:
                continue

            for attack in security_payloads:
                try:
                    t = time.time()
                    await browser.navigate(url, timeout=8000, take_screenshot=False)
                    browser.clear_events()

                    form_filled = False
                    for inp in fillable[:3]:
                        selector = inp.get("selector", "")
                        if not selector:
                            name_attr = inp.get("name", "")
                            if name_attr:
                                selector = f'[name="{name_attr}"]'
                        if selector:
                            try:
                                await browser.page.fill(selector, attack["value"], timeout=2000)
                                form_filled = True
                            except Exception:
                                pass

                    if not form_filled:
                        try:
                            await browser.page.fill(
                                "input:not([type='hidden']):not([type='submit'])",
                                attack["value"],
                            )
                            form_filled = True
                        except Exception:
                            pass

                    if not form_filled:
                        continue

                    pre_url = browser.page.url
                    capture = await browser.submit_form(timeout=8000, take_screenshot=False)
                    post_url = browser.page.url

                    url_changed = pre_url != post_url
                    content = await browser.page.content()
                    no_error = not any(
                        kw in content.lower()
                        for kw in ["error", "invalid", "required", "warning", "validation", "rejected"]
                    )

                    if attack["category"] == "security":
                        is_vuln = await self._verify_injection(browser.page, attack["input_type"])
                        if is_vuln:
                            await self._flag(browser, FlaggedEvent(
                                event_type="invalid_input",
                                severity_hint="high",
                                url=url,
                                description=f"Form accepted security payload '{attack['name']}' without validation",
                                screenshot_path=capture.screenshot_path,
                                steps_taken=[f"Navigate to {url}", f"Fill form with {attack['name']}", "Submit"],
                                edge_case_used=attack["name"],
                            ))
                        elif no_error and (url_changed or len(content) > 100):
                            pass  # Discard if it's security related but no hard evidence

                    elif (url_changed or len(content) > 100) and no_error and attack["category"] == "validation":
                        pass # discard

                    # Check for 5xx after submit
                    for ne in capture.network_events:
                        if _is_real_http_error(ne.status, ne.url, self.base_domain):
                            await self._flag(browser, FlaggedEvent(
                                event_type="http_error",
                                severity_hint="critical",
                                url=url,
                                description=f"HTTP {ne.status} after form attack '{attack['name']}'",
                                screenshot_path=capture.screenshot_path,
                                network_status=ne.status,
                                steps_taken=[f"Navigate to {url}", f"Submit {attack['name']}"],
                                edge_case_used=attack["name"],
                            ))

                    logger.info(f"ADVERSARIAL: {time.time() - t:.1f}s for {attack['name']} on {url}")
                    await asyncio.sleep(0.5)  # Add delay to prevent DOS-ing the server

                except Exception as e:
                    logger.debug(f"Form attack error ({attack['name']} on {url}): {e}")

    async def _flag(self, browser: PhantomBrowser, event: FlaggedEvent):
        """Deduplicate, take screenshot, and add event."""
        # Deduplicate
        for existing in self.flagged_events:
            if (
                existing.event_type == event.event_type
                and existing.url == event.url
                and existing.description[:80] == event.description[:80]
            ):
                return

        # Take screenshot if missing
        if not event.screenshot_path:
            try:
                import datetime as _dt
                ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                safe_url = re.sub(r"[^\w]", "_", event.url)[:60]
                shot_path = self.screenshots_dir / f"bug_{ts}_{safe_url}.png"
                await browser.page.screenshot(path=str(shot_path), full_page=True)
                event.screenshot_path = str(shot_path)
            except Exception:
                event.screenshot_path = ""

        self.flagged_events.append(event)
        logger.warning(
            f"BUG FLAGGED [{event.severity_hint.upper()}] "
            f"{event.event_type}: {event.description[:80]}"
        )

    async def _verify_injection(self, page, payload_type: str) -> bool:
        """Check for actual SQL/XSS error messages."""
        try:
            content = (await page.content()).lower()
            
            sql_error_signs = [
                "sql syntax", "mysql_fetch", "ora-", 
                "sqlite", "pg_query", "sqlstate",
                "unclosed quotation", "syntax error"
            ]
            
            xss_error_signs = [
                "<script>", "alert(", "onerror=",
                "javascript:"
            ]
            
            if payload_type == "sql_injection":
                return any(s in content for s in sql_error_signs)
            
            if payload_type == "xss":
                return any(s in content for s in xss_error_signs)
                
        except Exception as e:
            logger.debug(f"verify_injection error: {e}")
            
        return False
