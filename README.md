# Job Assistant

AI-powered job application agent with a Flask web dashboard. It searches job boards, scores listings against your profile, automates form filling with Playwright, and tracks applications in Excel ledgers.

## Features

- **Web UI** — Start/stop runs, review applications, answer knowledge-ledger prompts, and stream live logs (SSE)
- **Job matching** — Gemini-based scoring against your resume and profile
- **Browser automation** — Playwright with optional persistent Chrome profile
- **Application tracking** — Excel workbooks for applied jobs, all jobs, credentials, and Q&A ledger

## Setup

1. **Clone and install**

   ```bash
   git clone https://github.com/CHANDRADEEP2007/job_assistant.git
   cd job_assistant
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Configure**

   - Copy `.env.example` to `.env` and set `GEMINI_API_KEY`
   - Copy `profile.example.json` to `profile.json` and fill in your details
   - Place your resume as `Resume.pdf` in the project root
   - Adjust `settings.json` for search queries, locations, and thresholds

3. **Run the dashboard**

   ```bash
   python app.py
   ```

   Open `http://127.0.0.1:5000` in your browser.

## Project layout

| Path | Description |
|------|-------------|
| `app.py` | Flask server and API routes |
| `templates/`, `static/` | Web UI |
| `main.py` | Core job-processing pipeline |
| `navigation/` | Browser and job-search automation |
| `intelligence/` | LLM matching and knowledge ledger |
| `workflow/` | Application flow, review gate, tracking |
| `core/` | Config and guardrails |

## Notes

- Excel tracker files (`*.xlsx`) are created at runtime and are not committed (see `.gitignore`).
- Do not commit `.env`, `profile.json`, or browser profile directories.
