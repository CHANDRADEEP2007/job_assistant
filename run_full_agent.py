import json
import os
import pandas as pd
from navigation.browser import BrowserManager
from navigation.vision_agent import VisionAgent
from navigation.job_search import scrape_job_urls
from intelligence.knowledge_ledger import KnowledgeLedger
from main import process_job_url
from workflow.backfill_applications import run_backfill

def run():
    if not os.path.exists('settings.json'):
        print("Error: settings.json not found")
        return

    with open('settings.json', 'r') as f:
        settings = json.load(f)

    queries = settings.get('search_queries', ["Product Manager"])
    locations = settings.get('preferred_locations', ["California"])
    run_id = "full-run-" + pd.Timestamp.now().strftime("%Y%m%d-%H%M")

    print(f"=== FULL SCALE AGENT STARTING ===")
    print(f"Run ID: {run_id}")
    print(f"Queries: {queries}")
    print(f"Locations: {locations}")

    # PHASE 1: Backfill existing eligible jobs
    run_backfill()

    # PHASE 2: New Job Search
    print(f"\n=== PHASE 2: SEARCHING FOR NEW JOBS ===")
    browser = BrowserManager()
    vision = VisionAgent(browser)
    ledger = KnowledgeLedger()

    try:
        browser.start()
        for query in queries:
            for loc in locations:
                print(f"\n>>> SEARCHING: \"{query}\" in \"{loc}\"")
                urls = scrape_job_urls(browser, query, loc)
                print(f"Found {len(urls)} URLs")
                
                for i, url in enumerate(urls):
                    print(f"\n--- [{query}] Job {i+1}/{len(urls)} ---")
                    process_job_url(url, browser, vision, ledger, run_id, 'live')
                    
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        browser.close()
        print(f"\n=== AGENT COMPLETE: {run_id} ===")

if __name__ == "__main__":
    run()
