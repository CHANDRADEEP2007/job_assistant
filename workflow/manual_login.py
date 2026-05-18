from playwright.sync_api import sync_playwright
import os
import time

def manual_login():
    profile_path = os.path.join(os.getcwd(), "chrome_profile_v3")
    if not os.path.exists(profile_path):
        os.makedirs(profile_path)
        
    with sync_playwright() as p:
        print(f"Opening browser for manual login. Profile will be saved to: {profile_path}")
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_path,
            headless=False,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()
        page.goto("https://my.greenhouse.io/jobs")
        
        print("\n*** PLEASE LOG IN TO GREENHOUSE MANUALLY IN THE BROWSER WINDOW ***")
        print("The browser will remain open for 2 minutes. Close it or wait after you have logged in.")
        
        try:
            for i in range(120):
                if i % 10 == 0:
                    print(f"Waiting... ({120-i}s remaining)")
                time.sleep(1)
        except KeyboardInterrupt:
            print("Closing...")
        
        context.close()
        print("Greenhouse session saved. You can now run the agent in Live mode.")

if __name__ == "__main__":
    manual_login()
