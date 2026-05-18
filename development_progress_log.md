# ApexApply - Development Progress Log

## Project State: Fully Autonomous 6-Tab Command Center

### 1. Data Layer Transformation
The core data tracking was migrated from flat CSV files to a robust, 4-Excel Database Architecture:
- **`Knowledge_Ledger.xlsx`**: Central memory store containing profile Q&As and preferred locations.
- **`Applied_Applications.xlsx`**: A success-only log tracking jobs the agent successfully submitted.
- **`All_Applications.xlsx`**: A comprehensive master log tracking every job evaluated, including granular metadata (`run_id`, `decision_category`, `decision_detail`, `match_score`, `mode`).
- **`Credentials.xlsx`**: A secure repository for company-specific logins.

### 2. Location-Based Search Engine
- Implemented a dynamic location parsing hook in `app.py` that reads the `location` row directly from `Knowledge_Ledger.xlsx` on startup.
- These locations (e.g., Dallas, San Francisco, New York) are injected into `settings.json`.
- The `scrape_job_urls` (in `navigation/job_search.py`) and `run_agent` loop (in `main.py`) were overhauled to iterate through *each* specific location rather than defaulting to "Worldwide", guaranteeing hyper-targeted job discovery.

### 3. Frontend & UI Overhaul
The application evolved from a simple terminal output page into a professional **6-Tab Command Center** (`index.html`, `style.css`, `app.js`):
1. **Dashboard Tab**: 
   - Introduced 6 real-time KPI cards (Evaluated, Eligible, Applied, Skipped, Errors, Current Job).
   - Integrated **Chart.js** via CDN to visualize the Application Funnel (Skipped vs Eligible vs Applied) in real time.
   - Preserved the live SSE terminal and streaming table.
2. **All Applications Tab**: A unified table for deep tracking of every job scanned.
3. **Applied Jobs Tab**: A filtered view of successful submits.
4. **Knowledge Ledger Tab**: Read-only view of the AI's cached memory.
5. **Settings Tab**: A brand new interface that directly modifies `settings.json`, allowing the user to toggle between `Simulation` and `Live` mode, adjust Match Thresholds, and view Preferred Locations and Blacklisted Companies without modifying Python code.
6. **Credentials Tab**: Read-only vault for company logins.

### 4. Safety & Execution
- The agent currently defaults to **Simulation/Mock Mode**, which means it executes the full pipeline (scraping, guardrails, LLM tailoring) but halts right before clicking the final 'Submit' button on live forms.
- This mode guarantees safety during development. The safety lock can be removed via the new Settings Tab when ready.

### Next Steps for Restart:
- The Flask backend is fully wired up but currently stopped. To resume development, run `python app.py`.
- Future features could include:
  1. Integrating actual Playwright form-filling for the `Live` mode.
  2. Expanding scraper logic to indeed/glassdoor.
  3. Expanding the row details in the UI (e.g., clicking a row in "All Applications" to open a modal with the full job description and resume variant).
