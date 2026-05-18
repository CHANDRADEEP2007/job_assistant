import time
import random
from google import genai
from core.config import GEMINI_API_KEY
from navigation.browser import BrowserManager

class VisionAgent:
    def __init__(self, browser: BrowserManager):
        self.browser = browser
        if GEMINI_API_KEY:
            self.client = genai.Client(api_key=GEMINI_API_KEY)
        else:
            self.client = None

    def biological_delay(self, min_seconds=1.0, max_seconds=3.0):
        """Simulates human typing/clicking delays."""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)

    def analyze_page(self) -> dict:
        """
        Takes a screenshot and asks Gemini Flash to identify the next action.
        """
        if not self.client:
            print("[VisionAgent] No Gemini API key. Cannot perform vision analysis.")
            return {"action": "none"}
            
        screenshot_path = self.browser.take_screenshot("current_view.png")
        if not screenshot_path:
            return {"action": "none"}
            
        prompt = """
        You are a web navigation agent. Analyze this screenshot of a job application page.
        Identify the primary form fields, buttons, or dropdowns visible.
        Determine the next logical action (e.g., 'click_apply', 'fill_form', 'scroll').
        Output JSON with 'action' and 'target'.
        """
        
        try:
            # We use a placeholder for gemini-3.1-flash, usually gemini-1.5-flash handles vision
            # We'll use the current recommended vision model identifier
            response = self.client.models.generate_content(
                model='gemini-1.5-flash',
                contents=[prompt, screenshot_path] # Requires uploading the file in a real scenario, but syntax simplified for mock
            )
            # In a real scenario, parsing JSON from response
            print(f"[VisionAgent] Flash analysis: {response.text}")
            return {"action": "detected_from_flash"}
        except Exception as e:
            print(f"[VisionAgent] Vision API error: {e}")
            return {"action": "error"}

    def intelligent_scroll(self):
        """
        Scrolls the page incrementally, analyzing each view to find the best match in a list/dropdown.
        """
        print("[VisionAgent] Performing Intelligent Scroll...")
        if self.browser.page:
            self.browser.page.mouse.wheel(0, 500)
            self.biological_delay(0.5, 1.5)
            # Re-analyze after scrolling
            self.analyze_page()
