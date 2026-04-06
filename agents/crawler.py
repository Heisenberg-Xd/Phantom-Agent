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

logger = logging.getLogger("phantom")

APP_MODEL_PATH = Path("memory/app_model.json")

# ── Gemini client ─────────────────────────────────────────────────────────────

def _make_client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-1.5-flash"
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
    ):
        self.base_url = base_url
        self.base_domain = urlparse(base_url).netloc
        self.description = description
        self.max_pages = max_pages
        self.screenshots_dir = screenshots_dir
        self.headless = headless
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
        Each page: {url, title, screenshot, load_time, forms, interactive_elements}
        """
        logger.info(f"Starting crawl of {self.base_url}")
        pages: list[dict] = []
        visited: set[str] = set()
        queue = deque([self.base_url])

        async with PhantomBrowser(self.screenshots_dir, headless=self.headless) as browser:
            while queue and len(visited) < self.max_pages:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)

                try:
                    capture = await browser.navigate(url, timeout=20000)
                    page_dict = {
                        "url": capture.url,
                        "title": capture.title,
                        "screenshot": capture.screenshot_path,
                        "load_time": capture.load_time_seconds,
                        "forms": await browser.get_all_forms(),
                        "interactive_elements": await browser.get_interactive_elements(),
                        "console_errors": [
                            e.text for e in capture.console_events if e.level == "error"
                        ],
                        "http_errors": [
                            {"url": e.url, "status": e.status}
                            for e in capture.network_events
                            if e.status >= 400
                        ],
                    }
                    pages.append(page_dict)

                    links = await browser.get_all_links()
                    for link in links:
                        normalized = self._normalize_url(link, capture.url)
                        if normalized and self._is_allowed(self.base_url, normalized):
                            if normalized not in visited:
                                queue.append(normalized)

                    await asyncio.sleep(0.3)

                except Exception as e:
                    logger.debug(f"Journey step error ({url}): {e}")

        logger.info(f"Crawl complete. Visited {len(pages)} pages.")
        return pages

    # ── Journey builder — separate from crawl, takes page list ───────────────

    async def build_journeys(self, pages: list) -> list:
        """
        Ask Gemini to suggest user journeys based on discovered pages.
        Returns list of journey dicts.
        """
        logger.info("Building journeys with Gemini...")

        graph_summary = {
            "total_pages": len(pages),
            "pages": [
                {
                    "url": p["url"],
                    "title": p["title"],
                    "forms_count": len(p.get("forms", [])),
                    "buttons_count": len(p.get("interactive_elements", [])),
                    "forms": p.get("forms", [])[:3],
                    "buttons": [
                        e.get("text", "") for e in p.get("interactive_elements", [])[:10]
                    ],
                }
                for p in pages
            ],
        }

        prompt = f"""This is a {self.description}. Based on these pages and interactive elements:
{json.dumps(graph_summary, indent=2)}

Generate 3-5 concrete user journeys as step-by-step Playwright actions.
Each journey must include:
- journey name
- steps: list of {{action, selector, value}} dicts
- success_criteria: what must be true at the end

Return JSON only. No markdown fences. Example:
{{
  "journeys": [
    {{
      "name": "login flow",
      "steps": [
        {{"action": "fill", "selector": "input[type='email']", "value": "test@example.com"}},
        {{"action": "fill", "selector": "input[type='password']", "value": "password123"}},
        {{"action": "click", "selector": "button[type='submit']"}}
      ],
      "success_criteria": "user is logged in and redirected to dashboard",
      "pages_involved": ["url1"],
      "priority": "high"
    }}
  ]
}}"""

        try:
            result = await asyncio.wait_for(
                self._call_gemini(prompt, max_retries=1),
                timeout=30,
            )
            journeys = result.get("journeys", [])

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

            logger.info(f"Built {len(journeys)} journeys.")
            return journeys
        except asyncio.TimeoutError:
            logger.warning("build_journeys timed out after 30s — proceeding without journeys")
            return []
        except Exception as e:
            logger.error(f"build_journeys error: {e}")
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
