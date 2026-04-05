import sys
import re

content = open('agents/explorer.py', 'r', encoding='utf-8').read()

# 1. Rename run to _run_exploration and add new run
content = content.replace("    async def run(self) -> list[FlaggedEvent]:", """    async def run(self) -> list[FlaggedEvent]:
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

    async def _run_exploration(self) -> list[FlaggedEvent]:""")

# 2. Add tested_pages in _run_exploration block 3
old_block_3 = """        # 3. Fuzz all forms found in graph
        for node in graph_nodes:
            url = node.get("url", "")
            forms = node.get("forms", [])
            if url and forms:
                await self._fuzz_forms_on_page(url, forms)"""

new_block_3 = """        # 3. Fuzz all forms found in graph
        tested_pages = set()
        for node in graph_nodes:
            url = node.get("url", "")
            forms = node.get("forms", [])
            if url and forms and url not in tested_pages:
                tested_pages.add(url)
                await self._fuzz_forms_on_page(url, forms)"""
content = content.replace(old_block_3, new_block_3)

# 3. disable default screenshots
content = content.replace("capture = await self.browser.navigate(url, timeout=15000)", "capture = await self.browser.navigate(url, timeout=15000, take_screenshot=False)")
content = content.replace("capture = await self.browser.navigate(start_url, timeout=20000)", "capture = await self.browser.navigate(start_url, timeout=20000, take_screenshot=False)")

content = re.sub(
    r"capture = await self\.browser\.navigate\(\s*self\.browser\.page\.url,\s*timeout=3000\s*\)",
    "capture = await self.browser.navigate(self.browser.page.url, timeout=3000, take_screenshot=False)",
    content
)

content = content.replace("await self.browser.navigate(url, timeout=15000)", "await self.browser.navigate(url, timeout=15000, take_screenshot=False)")
content = content.replace("capture = await self.browser.submit_form(timeout=8000)", "capture = await self.browser.submit_form(timeout=8000, take_screenshot=False)")

# 4. update _flag
old_flag = """        self.flagged_events.append(event)
        self.bug_count += 1
        logger.warning("""

new_flag = """        # Take screenshot ONLY on bug detection!
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
        logger.warning("""
content = content.replace(old_flag, new_flag)

open('agents/explorer.py', 'w', encoding='utf-8').write(content)
print("Updated explorer.py!")
