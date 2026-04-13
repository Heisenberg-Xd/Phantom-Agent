import asyncio
import logging
from pathlib import Path

from browser.runner import PhantomBrowser
from agents.explorer import handle_login

logger = logging.getLogger("phantom")


class SecurityAgent:
    def __init__(self, base_url: str, pages: list, screenshots_dir: Path, headless: bool = True, login_steps: list = None):
        self.base_url = base_url
        self.pages = pages
        self.screenshots_dir = screenshots_dir
        self.headless = headless
        self.login_steps = login_steps

    async def run_tests(self, browser, events: list):
        all_security_bugs = []
        page = browser.page

        logger.info("  [Security] Running Universal Security Scan...")
        bugs = await self.universal_security_scan(browser)
        all_security_bugs.extend(bugs)

        logger.info("  [Security] Testing session after logout...")
        bugs = await self.test_session_persistence(browser)
        all_security_bugs.extend(bugs)

        return all_security_bugs

    async def _login(self, page, browser):
        if self.login_steps:
            await browser.execute_login(self.login_steps)
        else:
            await handle_login(page, self.base_url)

    async def universal_security_scan(self, browser) -> list:
        """Test ALL inputs for security vulnerabilities"""
        bugs = []
        security_payloads = {
            'sql_injection': [
                "' OR '1'='1",
                "admin'--",
            ],
            'xss': [
                '<script>window.__phantom_xss_triggered=true;</script>',
                '<img src=x onerror=window.__phantom_xss_triggered=true;>',
            ],
            'path_traversal': [
                '../../../../etc/passwd',
                '....//....//....//etc/passwd'
            ]
        }
        
        try:
            from browser.dom_analyzer import smart_find_element
            # Dynamically test forms from universal crawler
            for page_data in self.pages[:3]: # Limit to avoid extreme long tail
                url = page_data.get("url")
                if not url: continue
                
                for form in page_data.get("forms", [])[:2]:
                    for input_field in form.get("inputs", []):
                        if input_field.get("type") in ("hidden", "submit", "button", "file"):
                            continue
                            
                        for attack_type, payloads in security_payloads.items():
                            for payload in payloads:
                                try:
                                    await browser.navigate(url, timeout=10000, take_screenshot=False)
                                    browser.clear_events()
                                    
                                    # Target current field with payload
                                    target_selector = input_field.get("selector", {}).get("primary")
                                    if not target_selector:
                                        target_selector = input_field.get("selector")
                                        
                                    elem = await smart_find_element(browser.page, target_selector)
                                    if not elem: continue
                                    logger.info(f"Fuzzing {url} element {target_selector} with {payload}")
                                    await elem.fill(payload)
                                    
                                    if form.get("buttons"):
                                        btn_sel = form["buttons"][0].get("selector", {}).get("primary")
                                        if not btn_sel: btn_sel = form["buttons"][0].get("selector")
                                        if btn_sel:
                                            btn = await smart_find_element(browser.page, btn_sel)
                                            if btn: await btn.click()
                                            
                                    await browser.page.wait_for_timeout(1000)
                                    content = await browser.page.content()
                                    
                                    # Verification
                                    if attack_type == 'sql_injection':
                                        if any(e in content.lower() for e in ["sql syntax", "mysql_fetch", "ora-", "postgresql"]):
                                            bugs.append({
                                                "title": f"SQL Injection vulnerability in {input_field.get('name', 'input')} field",
                                                "severity": "critical",
                                                "type": "sql_injection",
                                                "category": "security",
                                                "evidence": {
                                                    "confirmed": True,
                                                    "payload": payload,
                                                    "result": "SQL syntax error exposed on page"
                                                },
                                                "actual_behavior": f"Payload `{payload}` triggered a database error.",
                                                "expected_behavior": "Input should be parameterized and sanitized.",
                                                "steps_to_reproduce": [f"Submit `{payload}` to {input_field.get('name')} field"],
                                                "affected_url": url,
                                                "suggested_fix": "Use parameterized queries."
                                            })
                                            break
                                    elif attack_type == 'xss':
                                        has_script = await browser.page.evaluate("typeof window.__phantom_xss_triggered !== 'undefined'")
                                        if has_script:
                                            bugs.append({
                                                "title": f"Cross-Site Scripting (XSS) in {input_field.get('name', 'input')} field",
                                                "severity": "critical",
                                                "type": "xss",
                                                "category": "security",
                                                "evidence": {"confirmed": True, "payload": payload, "result": "Script evaluated in DOM"},
                                                "actual_behavior": f"Payload `{payload}` evaluated successfully.",
                                                "expected_behavior": "Input reflected securely without evaluation.",
                                                "steps_to_reproduce": [f"Submit `{payload}` to {input_field.get('name')} field"],
                                                "affected_url": url,
                                                "suggested_fix": "Escape user input before rendering and use DOMPurify."
                                            })
                                            break
                                except Exception as e:
                                    logger.debug(f"[Security] Scan step error: {e}")
                                    pass
        except Exception as e:
            logger.error(f"Universal security scan error: {e}")
            
        return bugs

    async def test_session_persistence(self, browser):
        bugs = []

        from playwright.async_api import async_playwright
        try:
            playwright_instance = await async_playwright().start()
            browser2 = await playwright_instance.chromium.launch(headless=self.headless)
            browser_context = await browser2.new_context()
            page = await browser_context.new_page()

            # Login
            await self._login(page, browser)
            await page.wait_for_timeout(1000)

            cookies = await browser_context.cookies()
            session_cookies = [c for c in cookies if "jsession" in c["name"].lower() or "session" in c["name"].lower() or "auth" in c["name"].lower() or "token" in c["name"].lower()]

            if not session_cookies:
                await browser_context.close()
                await browser2.close()
                await playwright_instance.stop()
                return bugs

            logout_urls = [
                f"{self.base_url}/logout.jsp",
                f"{self.base_url}/bank/logout.jsp",
                f"{self.base_url}/signoff.jsp",
                f"{self.base_url}/logout",
                f"{self.base_url}/signout"
            ]
            
            for logout_url in logout_urls:
                try:
                    await page.goto(logout_url, timeout=5000)
                    break
                except:
                    continue

            await page.wait_for_timeout(1000)
            await browser_context.close()

            # Step 4: Re-use cookies in a new context
            new_context = await browser2.new_context()
            await new_context.add_cookies(session_cookies)
            new_page = await new_context.new_page()

            try:
                # Try a few common protected paths
                for target_path in ["/bank/main.jsp", "/dashboard", "/account", "/profile"]:
                    target_url = f"{self.base_url}{target_path}"
                    try:
                        await new_page.goto(target_url, timeout=8000)
                        content = await new_page.content()
                        current_url = new_page.url

                        if any(ind in current_url.lower() for ind in ["main.jsp", "dashboard", "account"]) or ("account" in content.lower() and "login" not in current_url):
                            bugs.append({
                                "title": "Session token remains valid after logout",
                                "severity": "high",
                                "type": "session_persistence_after_logout",
                                "category": "security",
                                "evidence": {
                                    "confirmed": True,
                                    "result": "Old session cookie still grants access after logout"
                                },
                                "actual_behavior": "Session cookie remains valid safely after user logs out. Attacker can reuse stolen session.",
                                "expected_behavior": "Server should invalidate session token instantly on the backend upon logout",
                                "steps_to_reproduce": [
                                    "Login to application",
                                    "Copy session cookie value",
                                    "Click logout",
                                    "Make new request with old session cookie",
                                    "Observe: still authenticated"
                                ],
                                "affected_url": target_url,
                                "suggested_fix": "Call session.invalidate() on the backend exclusively upon logout. Do not merely discard cookies on client-side."
                            })
                            break
                    except:
                        continue
            except Exception as e:
                logger.debug(f"Session persistence error: {e}")

            await new_context.close()
            await browser2.close()
            await playwright_instance.stop()
        except Exception as e:
            logger.debug(f"Setup error in test_session_persistence: {e}")

        return bugs
