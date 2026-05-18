import os
import time
from playwright.sync_api import sync_playwright


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.browser_context = None
        self.page = None
        self.local_profile_path = os.path.join(os.getcwd(), "chrome_profile_v3")

    def sync_page(self):
        try:
            if self.browser_context:
                pages = [page for page in self.browser_context.pages if not page.is_closed()]
                if pages:
                    self.page = pages[-1]
            if self.page and self.page.is_closed():
                self.page = None
            if not self.page and self.browser_context:
                self.page = self.browser_context.new_page()
            if self.page:
                try:
                    self.page.bring_to_front()
                except Exception:
                    pass
        except Exception:
            pass
        return self.page

    def start(self):
        self.playwright = sync_playwright().start()
        
        print("[Browser] Launching Chromium with local profile...")
        try:
            # We use the local_profile_path which might have been populated by manual_login.py
            if not os.path.exists(self.local_profile_path):
                os.makedirs(self.local_profile_path)
                
            self.browser_context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.local_profile_path,
                headless=False,
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox"
                ],
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
            self.page = self.browser_context.pages[0] if self.browser_context.pages else self.browser_context.new_page()
            self.sync_page()
            print("[Browser] Browser ready (Persistent Context).")
        except Exception as e:
            print(f"[Browser] Persistent launch failed: {e}. Falling back to clean browser.")
            self.browser = self.playwright.chromium.launch(headless=False)
            self.browser_context = self.browser.new_context(viewport={"width": 1920, "height": 1080})
            self.page = self.browser_context.new_page()
            self.sync_page()

    def navigate(self, url: str, timeout: int = 30000):
        if not self.page:
            self.start()
        self.sync_page()

        print(f"[Browser] Navigating to {url[:80]}...")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            print(f"[Browser] Navigation error: {str(e)[:100]}")

    def get_page_text(self) -> str:
        self.sync_page()
        if not self.page:
            return ""
        try:
            return self.page.evaluate("document.body.innerText")
        except Exception:
            return ""

    def take_screenshot(self, path="screenshot.png"):
        self.sync_page()
        if self.page:
            try:
                self.page.screenshot(path=path)
                return path
            except Exception:
                pass
        return None

    def detect_challenge(self) -> bool:
        """Returns True if a CAPTCHA or bot-detection page is active."""
        self.sync_page()
        if not self.page:
            return False
        try:
            url = self.page.url or ""
            text = self.get_page_text()[:800].lower()
            return (
                "checkpoint" in url or
                "challenge" in url or
                "captcha" in text or
                "verify you are human" in text or
                "unusual activity" in text or
                "security check" in text or
                "let us know you're not a robot" in text
            )
        except Exception:
            return False

    def close(self):
        try:
            if self.browser_context:
                self.browser_context.close()
            elif self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except: pass
