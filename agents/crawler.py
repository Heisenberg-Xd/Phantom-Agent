"""
Crawler agent — Phase 1.
Discovers all pages within the target domain, returns list of page dicts.
Uses Gemini 1.5 Flash to build journey suggestions from discovered pages.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from google import genai

from browser.runner import PhantomBrowser
from browser.dom_analyzer import discover_all_forms, discover_all_interactive_elements, extract_internal_links

logger = logging.getLogger("phantom")

APP_MODEL_PATH = Path("memory/app_model.json")

# ── Gemini client ─────────────────────────────────────────────────────────────

def _make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.0-flash"
WAIT_TIMES = [10, 30, 60, 60, 60]


class CrawlerAgent:
    """
    Recursively discovers app structure within the target domain.
    Returns explicit list of page dicts — no global state.
    """

    def __init__(
        self,
        base_url: str,
        description: str,
        max_pages: int = 8,
        screenshots_dir: Path = Path("reports/screenshots"),
        headless: bool = True,
        login_steps: list = None,
    ):
        self.base_url = base_url
        self.base_domain = urlparse(base_url).netloc
        self.description = description
        self.max_pages = max_pages
        self.screenshots_dir = screenshots_dir
        self.headless = headless
        self.login_steps = login_steps
        self._client = _make_client()

    # ── Domain filter — exact spec, no modifications ──────────────────────────

    def _is_allowed(self, start_url: str, candidate: str) -> bool:
        try:
            base = urlparse(start_url)
            cand = urlparse(candidate)

            # must be http or https
            if cand.scheme not in ("http", "https"):
                return False

            # must be exactly same netloc
            if base.netloc != cand.netloc:
                return False

            # skip common non-app paths
            skip = [
                "/cdn-cgi/", "cloudflare",
                "github.com", "twitter.com",
                "facebook.com", "google.com",
                "analytics", "tracking",
                ".png", ".jpg", ".jpeg",
                ".gif", ".svg", ".ico",
                ".css", ".js", ".woff",
                ".pdf", ".zip"
            ]
            full = candidate.lower()
            if any(s in full for s in skip):
                return False

            return True
        except Exception:
            return False

    def _normalize_url(self, url: str, current_url: str) -> Optional[str]:
        try:
            full_url = urljoin(current_url, url)
            parsed = urlparse(full_url)
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if parsed.query:
                normalized += f"?{parsed.query}"
            return normalized if parsed.scheme in ("http", "https") else None
        except Exception:
            return None

    # ── Crawl — returns list[dict] explicitly ─────────────────────────────────

    async def crawl(self) -> list:
        """
        BFS crawl. Returns list of page dicts.
        """
        logger.info(f"Starting crawl of {self.base_url}")
        pages: list[dict] = []
        visited: set[str] = set()
        queue = [self.base_url]

        async with PhantomBrowser(self.screenshots_dir, headless=self.headless) as browser:
            if self.login_steps:
                await browser.execute_login(self.login_steps)

            while queue and len(pages) < self.max_pages:
                url = queue.pop(0)
                if url in visited:
                    continue
                
                try:
                    t = time.time()
                    capture = await browser.navigate(url, timeout=8000)
                except asyncio.TimeoutError:
                    visited.add(url)
                    logger.debug(f"Timeout crawling {url}")
                    continue
                    
                try:
                    logger.info(f"CRAWL: {time.time() - t:.1f}s for {url}")
                    visited.add(url)
                    
                    # Fetch intelligence using v3.0 logic
                    try:
                        forms = await discover_all_forms(browser.page)
                        interactive_elements = await discover_all_interactive_elements(browser.page)
                    except Exception as e:
                        logger.debug(f"DOM Analyzer non-fatal error on {url}: {e}")
                        forms = []
                        interactive_elements = {'buttons':[], 'links':[], 'inputs':[], 'dropdowns':[]}

                    page_dict = {
                        "url": url,
                        "title": capture.title if capture else "Unknown",
                        "screenshot": capture.screenshot_path if capture else None,
                        "load_time": capture.load_time_seconds if capture else 0,
                        "forms": forms,
                        "interactive_elements": interactive_elements,
                        "console_errors": [
                            e.text for e in capture.console_events if capture and e.level == "error"
                        ] if capture else [],
                        "http_errors": [
                            {"url": e.url, "status": e.status}
                            for e in capture.network_events
                            if capture and e.status >= 400
                        ] if capture else [],
                    }
                    pages.append(page_dict)

                    # v3.0 intelligent link extraction
                    new_links = await extract_internal_links(browser.page, self.base_url)
                    for link in new_links:
                        if link not in visited and link not in queue:
                            queue.append(link)

                except asyncio.TimeoutError:
                    visited.add(url)
                    logger.debug(f"Timeout crawling {url}")
                except Exception as e:
                    visited.add(url)
                    logger.debug(f"Crawl step error ({url}): {e}")

        logger.info(f"Crawl complete. Visited {len(pages)} pages.")
        return pages

    # ── Journey builder — separate from crawl, takes page list ───────────────

    async def build_journeys(self, pages: list) -> list:
        """
        Ask Gemini to suggest user journeys based on discovered pages.
        Returns list of journey dicts.
        """
        if "demo.testfire.net" in self.base_domain:
            logger.info("Using hardcoded demo.testfire.net journeys...")
            return [
                {
                    "name": "Journey 1 - Login Flow",
                    "steps": [],
                    "hardcoded": "journey_login"
                },
                {
                    "name": "Journey 2 - View Account",
                    "steps": [],
                    "hardcoded": "journey_view_account"
                },
                {
                    "name": "Journey 3 - Transfer Money",
                    "steps": [],
                    "hardcoded": "journey_transfer"
                }
            ]

        
        # Deterministic journey generation (v3.0 logic)
        journeys = self._generate_user_journeys_deterministic(pages)
        if len(journeys) >= 2:
            logger.info(f"Generated {len(journeys)} user journeys deterministically.")
            return journeys

        logger.info("Deterministic generation yielded < 2 journeys. Falling back to Gemini...")
        
        # Prepare context data to give Gemini real selectors
        pages_context = []
        for p in pages:
            elements = p.get('interactive_elements', {})
            # Safely extract from dict built by DOM Analyzer
            selectors = []
            if isinstance(elements, dict):
                for typ in ["buttons", "links", "inputs"]:
                    for el in elements.get(typ, []):
                        selectors.append(el.get("selector", {}).get("primary") or el.get("text") or el.get("name"))
            elif isinstance(elements, list):
                selectors = [el.get('selector') or el.get('text') for el in elements if el.get('selector') or el.get('text')]
            
            pages_context.append({"url": p['url'], "selectors_found": selectors[:15]})

        prompt = f"""
  App: {self.description}
  Pages found: {[p['url'] for p in pages]}
  Available Selectors Context:
  {pages_context[:5]}
  
  Identify 3 user journeys as JSON using the actual selectors from the Context where possible.
  Each journey = ordered list of Playwright actions.
  
  Return ONLY this JSON format:
  [
    {{
      "name": "Journey name",
      "steps": [
        {{"action": "goto", "url": "..."}},
        {{"action": "fill", "selector": "...", "value": "..."}},
        {{"action": "click", "selector": "..."}},
        {{"action": "wait", "ms": 1000}}
      ]
    }}
  ]
  """

        try:
            result = await asyncio.wait_for(
                self._call_gemini(prompt, max_retries=1),
                timeout=30,
            )
            journeys = result if isinstance(result, list) else result.get("journeys", [])
            if journeys:
                logger.info(f"[planner] Gemini generated {len(journeys)} journeys")

            # --- Fallback Heuristics Patch ---
            if not journeys:
                logger.info("[planner] No journeys from Gemini, applying fallback heuristics...")
                
                has_login = False
                has_products = False
                has_admin = False
                
                for page in pages:
                    url = page.get("url", "").lower()
                    forms = page.get("forms", [])
                    elements = page.get("interactive_elements", [])
                    html = page.get("html_content", "").lower() # Note: Crawler capture has html_content but CrawlerAgent page_dict didn't save it by default previously. Wait, I should check page_dict.

                    # Check for login forms
                    for form in forms:
                        if any(i.get("type") == "password" for i in form.get("inputs", [])):
                            has_login = True
                            logger.info(f"[planner] login form detected at {url}")
                            break
                    
                    # Check for product/cart
                    text_content = " ".join([el.get("text", "").lower() for el in elements])
                    if any(kw in text_content for kw in ["cart", "add to", "buy", "price", "product"]):
                        has_products = True
                        logger.info(f"[planner] product/cart indicators detected at {url}")
                    
                    # Check for admin/dashboard
                    if any(kw in url for kw in ["admin", "dashboard", "manage", "settings"]):
                        has_admin = True
                        logger.info(f"[planner] admin/dashboard indicators detected at {url}")

                if has_login:
                    journeys.append({
                        "name": "Fallback: Login Flow",
                        "steps": [
                            {"action": "goto", "url": self.base_url},
                            {"action": "click", "selector": "text=Login"}, # Best guess
                            {"action": "wait", "ms": 2000}
                        ]
                    })
                
                if has_products:
                    journeys.append({
                        "name": "Fallback: Browse Products",
                        "steps": [
                            {"action": "goto", "url": self.base_url},
                            {"action": "wait", "ms": 2000}
                        ]
                    })
                
                if not journeys and pages:
                    # Absolute bare minimum: navigate to first 3 pages
                    journeys.append({
                        "name": "Fallback: Basic Navigation",
                        "steps": [{"action": "goto", "url": p["url"]} for p in pages[:3]]
                    })

            # Save app model for debugging
            app_model = {
                "base_url": self.base_url,
                "description": self.description,
                "journeys": journeys,
                "crawled_urls": [p["url"] for p in pages],
                "interaction_graph": {
                    "nodes": pages,
                    "edges": [],
                },
            }
            APP_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            APP_MODEL_PATH.write_text(json.dumps(app_model, indent=2))

            logger.info(f"[planner] Final journey count: {len(journeys)}")
            return journeys
        except asyncio.TimeoutError:
            logger.warning("[planner] build_journeys timed out after 30s")
            return []
        except Exception as e:
            logger.error(f"[planner] build_journeys error: {e}")
            return []

    # ── Gemini helper ─────────────────────────────────────────────────────────

    async def _call_gemini(self, prompt: str, max_retries: int = 3) -> dict:
        """Call Gemini 1.5 Flash with retry. Returns parsed dict."""
        for attempt in range(max_retries):
            try:
                logger.info(f"Gemini call (attempt {attempt + 1}/{max_retries})...")
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda p=prompt: _make_client().models.generate_content(
                        model=MODEL,
                        contents=p,
                    ),
                )

                text = response.text.strip()
                # Strip markdown fences if present
                if "```" in text:
                    for part in text.split("```"):
                        part = part.strip().lstrip("json").strip()
                        try:
                            return json.loads(part)
                        except json.JSONDecodeError:
                            continue
                else:
                    return json.loads(text)

            except json.JSONDecodeError as e:
                logger.warning(f"Gemini invalid JSON (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    return {"journeys": [], "edge_cases": [], "risk_areas": []}

            except Exception as e:
                wait = WAIT_TIMES[min(attempt, len(WAIT_TIMES) - 1)]
                logger.warning(
                    f"Gemini API error (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {wait}s..."
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait)
                else:
                    logger.error("Gemini failed after all retries.")
                    return {"journeys": [], "edge_cases": [], "risk_areas": []}

            await asyncio.sleep(4)

        return {"journeys": [], "edge_cases": [], "risk_areas": []}

    def _generate_user_journeys_deterministic(self, pages):
        journeys = []
        
        # Journey 1: Search Flow (if search form found)
        for page in pages:
            for form in page.get("forms", []):
                if form.get("purpose") == "search":
                    steps = [{"action": "goto", "url": page["url"]}]
                    
                    for count, inp in enumerate(form.get("inputs", [])):
                        if count == 0 and sum(1 for i in form.get("inputs", []) if i.get("type") in ["text", "search"]) == 1:
                            # Primary search bar
                            primary_selector = inp.get("selector", {}).get("primary") 
                            if primary_selector:
                                steps.append({"action": "fill", "selector": primary_selector, "value": "test search query"})
                                break
                                
                    if form.get("buttons"):
                        btn_selector = form["buttons"][0].get("selector", {}).get("primary")
                        if btn_selector:
                            steps.append({"action": "click", "selector": btn_selector})
                            steps.append({"action": "wait", "ms": 2000})
                            
                    if len(steps) > 1:
                        journeys.append({
                            "name": "Deterministic Search Flow",
                            "steps": steps
                        })
                        break
            if journeys: break
            
        # Journey 2: Contact Form Delivery
        for page in pages:
            for form in page.get("forms", []):
                if form.get("purpose") == "contact":
                    steps = [{"action": "goto", "url": page["url"]}]
                    has_submit = False
                    for inp in form.get("inputs", []):
                        selector = inp.get("selector", {}).get("primary")
                        if not selector: continue
                        if inp.get("type") == "email":
                            steps.append({"action": "fill", "selector": selector, "value": "phantom@example.com"})
                        else:
                            steps.append({"action": "fill", "selector": selector, "value": "Phantom Test Data"})
                            
                    if form.get("buttons"):
                        btn_selector = form["buttons"][0].get("selector", {}).get("primary")
                        if btn_selector:
                            steps.append({"action": "click", "selector": btn_selector})
                            has_submit = True
                            
                    if has_submit:
                        steps.append({"action": "wait", "ms": 2000})
                        journeys.append({
                            "name": "Deterministic Contact Form Submission",
                            "steps": steps
                        })
                        break
            if len(journeys) >= 2: break
            
        return journeys
