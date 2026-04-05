"""
Playwright browser controller for Phantom QA agent.
Handles all browser automation with async Playwright.
"""

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

logger = logging.getLogger("phantom")


@dataclass
class NetworkEvent:
    url: str
    method: str
    status: int
    resource_type: str
    duration_ms: float = 0.0


@dataclass
class ConsoleEvent:
    level: str  # log, warn, error, info
    text: str
    location: str = ""


@dataclass
class PageCapture:
    url: str
    title: str
    screenshot_path: str
    load_time_seconds: float
    console_events: list[ConsoleEvent] = field(default_factory=list)
    network_events: list[NetworkEvent] = field(default_factory=list)
    html_content: str = ""
    timestamp: str = ""


class PhantomBrowser:
    """Async Playwright browser controller with monitoring capabilities."""

    def __init__(self, screenshots_dir: Path, headless: bool = True):
        self.screenshots_dir = screenshots_dir
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._console_events: list[ConsoleEvent] = []
        self._network_events: list[NetworkEvent] = []
        self._request_start_times: dict[str, float] = {}

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    async def start(self):
        """Launch browser and create context."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()
        self._attach_listeners()
        logger.info("Browser started successfully")

    async def stop(self):
        """Close browser and cleanup."""
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning(f"Error during browser cleanup: {e}")

    def _attach_listeners(self):
        """Attach console and network listeners to the page."""
        if not self._page:
            return

        self._page.on("console", self._on_console)
        self._page.on("response", self._on_response)
        self._page.on("request", self._on_request)

    def _on_console(self, msg):
        """Capture console messages."""
        event = ConsoleEvent(
            level=msg.type,
            text=msg.text,
            location=str(msg.location),
        )
        self._console_events.append(event)
        if msg.type == "error":
            logger.debug(f"Console error: {msg.text}")

    def _on_request(self, request):
        """Record request start time."""
        self._request_start_times[request.url] = time.time()

    def _on_response(self, response: Response):
        """Capture network responses."""
        start = self._request_start_times.pop(response.url, time.time())
        duration_ms = (time.time() - start) * 1000
        event = NetworkEvent(
            url=response.url,
            method=response.request.method,
            status=response.status,
            resource_type=response.request.resource_type,
            duration_ms=duration_ms,
        )
        self._network_events.append(event)

    def clear_events(self):
        """Clear captured events (call before each action)."""
        self._console_events.clear()
        self._network_events.clear()
        self._request_start_times.clear()

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Browser not started")
        return self._page

    async def navigate(self, url: str, timeout: int = 30000, take_screenshot: bool = True) -> PageCapture:
        """Navigate to URL and capture full page state."""
        self.clear_events()
        start = time.time()

        try:
            await self._page.goto(url, wait_until="networkidle", timeout=timeout)
        except Exception as e:
            logger.warning(f"Navigation timeout/error for {url}: {e}")
            # Still try to capture whatever loaded
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass

        load_time = time.time() - start
        return await self._capture_page(url, load_time, take_screenshot)

    async def _capture_page(self, url: str, load_time: float, take_screenshot: bool = True) -> PageCapture:
        """Take screenshot and capture page details."""
        import re
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_url = re.sub(r"[^\w]", "_", url)[:60]
        screenshot_filename = f"{timestamp}_{safe_url}.png"
        screenshot_path = ""
        if take_screenshot:
            screenshot_path_obj = self.screenshots_dir / screenshot_filename
            try:
                await self._page.screenshot(path=str(screenshot_path_obj), full_page=True)
                screenshot_path = str(screenshot_path_obj)
            except Exception as e:
                logger.warning(f"Screenshot failed: {e}")

        try:
            title = await self._page.title()
        except Exception:
            title = ""

        try:
            html = await self._page.content()
        except Exception:
            html = ""

        current_url = self._page.url

        return PageCapture(
            url=current_url,
            title=title,
            screenshot_path=screenshot_path,
            load_time_seconds=load_time,
            console_events=list(self._console_events),
            network_events=list(self._network_events),
            html_content=html,
            timestamp=timestamp,
        )

    async def screenshot_base64(self) -> str:
        """Capture screenshot and return as base64 string."""
        try:
            data = await self._page.screenshot(full_page=False)
            return base64.b64encode(data).decode("utf-8")
        except Exception as e:
            logger.warning(f"Base64 screenshot failed: {e}")
            return ""

    async def get_all_links(self) -> list[str]:
        """Extract all unique href links from the current page."""
        try:
            links = await self._page.evaluate("""() => {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                return [...new Set(anchors.map(a => a.href).filter(h => 
                    h.startsWith('http') || h.startsWith('/')
                ))];
            }""")
            return links or []
        except Exception as e:
            logger.warning(f"get_all_links failed: {e}")
            return []

    async def get_all_forms(self) -> list[dict]:
        """Extract all forms with their inputs from the current page."""
        try:
            forms = await self._page.evaluate("""() => {
                return Array.from(document.querySelectorAll('form')).map(form => ({
                    action: form.action || '',
                    method: form.method || 'get',
                    inputs: Array.from(form.querySelectorAll('input, textarea, select')).map(el => ({
                        name: el.name || el.id || el.placeholder || '',
                        type: el.type || el.tagName.toLowerCase(),
                        id: el.id || '',
                        placeholder: el.placeholder || '',
                        required: el.required,
                        selector: el.id ? `#${el.id}` : (el.name ? `[name="${el.name}"]` : '')
                    })).filter(el => el.type !== 'hidden')
                }));
            }""")
            return forms or []
        except Exception as e:
            logger.warning(f"get_all_forms failed: {e}")
            return []

    async def get_interactive_elements(self) -> list[dict]:
        """Get all buttons and clickable elements."""
        try:
            elements = await self._page.evaluate("""() => {
                const selectors = 'button, [role="button"], input[type="submit"], input[type="button"], a[href]';
                return Array.from(document.querySelectorAll(selectors)).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || el.value || el.placeholder || '').trim().substring(0, 80),
                    id: el.id || '',
                    href: el.href || '',
                    type: el.type || '',
                    selector: el.id ? `#${el.id}` : ''
                })).filter(el => el.text || el.href);
            }""")
            return elements or []
        except Exception as e:
            logger.warning(f"get_interactive_elements failed: {e}")
            return []

    async def fill_input(self, selector: str, value: str, timeout: int = 5000) -> bool:
        """Fill an input field safely."""
        try:
            await self._page.wait_for_selector(selector, timeout=timeout)
            await self._page.fill(selector, "")
            await self._page.fill(selector, value)
            return True
        except Exception as e:
            logger.debug(f"fill_input({selector}) failed: {e}")
            return False

    async def click_element(self, selector: str, timeout: int = 5000) -> bool:
        """Click an element safely."""
        try:
            await self._page.wait_for_selector(selector, timeout=timeout)
            await self._page.click(selector)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            logger.debug(f"click_element({selector}) failed: {e}")
            return False

    async def submit_form(
        self, form_selector: str = "form", timeout: int = 5000, take_screenshot: bool = True
    ) -> PageCapture:
        """Submit a form and capture results."""
        self.clear_events()
        start = time.time()
        try:
            submit_btn = await self._page.query_selector(
                f"{form_selector} [type='submit'], {form_selector} button[type='submit'], {form_selector} button"
            )
            if submit_btn:
                await submit_btn.click()
            else:
                await self._page.keyboard.press("Enter")
            await self._page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception as e:
            logger.debug(f"submit_form failed: {e}")

        load_time = time.time() - start
        return await self._capture_page(self._page.url, load_time, take_screenshot)

    async def wait_for_navigation(self, timeout: int = 5000):
        """Wait for any ongoing navigation."""
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    async def is_blank_page(self) -> bool:
        """Check if current page is blank or empty."""
        try:
            body_text = await self._page.evaluate(
                "document.body ? document.body.innerText.trim() : ''"
            )
            title = await self._page.title()
            return len(body_text) < 20 and len(title) < 5
        except Exception:
            return False

    async def new_page(self) -> Page:
        """Open a new page in the same context."""
        page = await self._context.new_page()
        return page
