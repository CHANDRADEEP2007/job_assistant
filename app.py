import os
import json
import queue
import threading
import time
import uuid
import pandas as pd
import sys
import re
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, Response, send_file

app = Flask(__name__)

# Import core components
from main import process_job_url, configure_main
from navigation.browser import BrowserManager
from navigation.vision_agent import VisionAgent
from navigation.job_search import scrape_job_urls
from intelligence.llm_bricking import extract_text_from_pdf, extract_job_details_from_text, evaluate_job_match
from intelligence.knowledge_ledger import KnowledgeLedger, configure_ledger
from workflow.backfill_applications import run_backfill
from workflow.review_gate import configure_review_gate
from workflow.application import configure_application
from workflow.tracking import classify_failure
from core.config import RESUME_PATH, PROFILE_PATH, CREDENTIALS_PATH, APPLIED_APPS_PATH, ALL_APPS_PATH, LEDGER_PATH
from core.guardrails import load_settings_safe

SETTINGS_PATH = "settings.json"

event_queue = queue.Queue()
agent_state = {
    "running": False,
    "mode": "live",
    "started_at": None,
    "current_job": None,
    "run_id": None
}


def sse_publish(event_type, payload):
    event_queue.put({"event": event_type, "data": payload})

# --- Review Gate bridge (Fix #3) ---
review_event = threading.Event()
review_decision = {"approved": None}

# --- Knowledge Ledger bridge (Fix #3) ---
ledger_event = threading.Event()
ledger_answer_store = {"answer": None}

# --- Pause-for-human (stuck form) bridge ---
pause_event = threading.Event()


def _stop_agent_from_application():
    """Called from application layer when stop_on_form_error is true."""
    agent_state["running"] = False
    agent_state["current_job"] = None
    sse_publish("agent_status", {"running": False, "mode": agent_state["mode"]})

DEFAULT_SETTINGS = {
    "mode": "live",
    "job_source": "greenhouse",
    "match_threshold": 65,
    "max_daily_applications": 50,
    "blacklisted_companies": [],
    "search_queries": ["Product Manager", "Platform Product Manager", "Senior Product Manager"],
    "preferred_locations": ["California"],
    "target_companies": [],
    "allow_easy_apply_only": False,
    "stop_on_form_error": False
}


def sanitize_settings(data):
    clean = dict(data or {})
    clean.pop("auto_tailor_resume", None)
    return clean

def ensure_settings():
    if not os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SETTINGS, f, indent=2)

def load_settings():
    ensure_settings()
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return sanitize_settings(data)

def save_settings(data):
    data = sanitize_settings(data)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# SSE Stream intercept
class StreamIntercept:
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout
    def write(self, message):
        self.original_stdout.write(message)
        if message.strip():
            try:
                sse_publish("terminal_log", {"message": message.strip()})
            except Exception:
                pass
    def flush(self):
        self.original_stdout.flush()

sys.stdout = StreamIntercept(sys.stdout)


def init_credentials():
    if not os.path.exists(CREDENTIALS_PATH):
        df = pd.DataFrame({"Company": ["Example Inc"], "User_ID": ["user@example.com"], "Password": ["password123"]})
        df.to_excel(CREDENTIALS_PATH, index=False)

init_credentials()

# --- Wire up all SSE bridges (Fix #3 & #4) ---
configure_review_gate(sse_publish, review_event, review_decision)
configure_ledger(sse_publish, ledger_event, ledger_answer_store)
configure_main(sse_publish)
configure_application(sse_publish, pause_event, _stop_agent_from_application)


# --- Pre-flight check logic (Fix #1) ---
def run_preflight_checks() -> list[dict]:
    """Returns a list of {name, ok, fix} dicts for every startup requirement."""
    checks = []

    # 1. Gemini API key
    key = os.getenv("GEMINI_API_KEY", "")
    checks.append({
        "name": "Gemini API Key",
        "ok": bool(key and len(key) > 10),
        "fix": "Add GEMINI_API_KEY=your_key to your .env file"
    })

    # 2. Resume.pdf
    checks.append({
        "name": "Resume.pdf",
        "ok": os.path.exists(RESUME_PATH),
        "fix": f"Place your resume at: {os.path.abspath(RESUME_PATH)}"
    })

    # 3. profile.json
    from core.config import PROFILE_PATH
    checks.append({
        "name": "profile.json",
        "ok": os.path.exists(PROFILE_PATH),
        "fix": f"Profile not found at: {os.path.abspath(PROFILE_PATH)}"
    })

    # 4. settings.json (valid JSON)
    settings_ok = False
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH) as f:
                json.load(f)
            settings_ok = True
    except Exception:
        pass
    checks.append({
        "name": "settings.json",
        "ok": settings_ok,
        "fix": "settings.json is missing or has invalid JSON. Delete it to auto-recreate."
    })

    # 5. Knowledge Ledger Excel
    checks.append({
        "name": "Knowledge_Ledger.xlsx",
        "ok": os.path.exists(LEDGER_PATH),
        "fix": f"Place your Knowledge_Ledger.xlsx at: {os.path.abspath(LEDGER_PATH)}"
    })

    # 6. Chrome session profile
    profile_dir = os.path.join(os.getcwd(), "chrome_profile_v3")
    profile_exists = os.path.isdir(profile_dir) and len(os.listdir(profile_dir)) > 0
    checks.append({
        "name": "Chrome Session Profile",
        "ok": profile_exists,
        "fix": "Run: python workflow/manual_login.py - then log in to Greenhouse."
    })

    return checks


def _check_session_health(browser: BrowserManager, source: str) -> bool:
    """Returns True if the selected job source session is still active."""
    try:
        if source == "greenhouse":
            browser.navigate("https://my.greenhouse.io/jobs", timeout=15000)
            time.sleep(3)
            url = browser.page.url if browser.page else ""
            text = browser.get_page_text()[:500].lower()
            if "/users/sign_in" in url or "enter your email" in text:
                return False
        else:  # linkedin
            browser.navigate("https://www.linkedin.com/feed", timeout=15000)
            time.sleep(3)
            url = browser.page.url if browser.page else ""
            if "/login" in url or "/checkpoint" in url:
                return False
        return True
    except Exception:
        return False


def run_agent():
    """Main agent loop wrapped in a watchdog for crash recovery."""
    try:
        _run_agent_inner()
    except Exception as e:
        import traceback
        error_msg = f"AGENT CRASH: {str(e)}\n{traceback.format_exc()}"
        print(f"\n[Watchdog] {error_msg}")
        # Notify UI about the crash
        sse_publish("agent_crash", {
            "error": str(e),
            "message": "The agent encountered a fatal error and stopped. Please check the logs, close the browser, and rerun it."
        })
    finally:
        agent_state["running"] = False
        agent_state["current_job"] = None
        sse_publish("agent_status", {"running": False, "mode": agent_state["mode"]})


def _run_agent_inner():
    """Main agent logic: Backfill -> New Search Loop."""
    print("[Agent] Starting ApexApply - Phase 1: Backfill + Phase 2: Live Search")
    settings = load_settings()

    print("[Agent] Extracting resume...")
    resume_text = extract_text_from_pdf(RESUME_PATH)
    if not resume_text:
        print("[Agent] ERROR: No resume text found. Aborting.")
        return

    print(f"[Agent] Resume loaded ({len(resume_text)} chars)")

    queries = settings.get("search_queries", ["Product Manager"])
    locations = settings.get("preferred_locations", ["California"])
    max_apps = settings.get("max_daily_applications", 50)
    job_source = settings.get("job_source", "greenhouse")

    ledger = KnowledgeLedger()
    browser = BrowserManager()
    vision = VisionAgent(browser)

    try:
        browser.start()

        # --- Fix #2: Session health check ---
        print(f"[Agent] Checking {job_source} session health...")
        session_ok = _check_session_health(browser, job_source)
        if not session_ok:
            msg = f"Session expired for {job_source}. Run: python workflow/manual_login.py"
            print(f"[Agent] Warning: {msg}")
            sse_publish("session_expired", {"platform": job_source, "message": msg})
            browser.close()
            return

        # --- Phase 1: Backfill ---
        print("\n[Agent] === PHASE 1: BACKFILL ELIGIBLE JOBS ===")
        run_backfill(browser=browser, vision=vision, ledger=ledger)

        # --- Phase 2: New Search ---
        print("\n[Agent] === PHASE 2: SEARCHING FOR NEW JOBS ===")
        jobs_processed = 0

        for query in queries:
            if not agent_state["running"]: break
            for loc in locations:
                if not agent_state["running"]: break

                print(f"\n[Agent] Searching: '{query}' in '{loc}'")
                urls = scrape_job_urls(browser, query, loc, job_source)

                if not urls: continue

                for url in urls:
                    if not agent_state["running"]: break
                    if jobs_processed >= max_apps:
                        print(f"[Agent] Reached limit of {max_apps} apps. Stopping.")
                        return

                    agent_state["current_job"] = url
                    sse_publish("agent_status", {"running": True, "mode": agent_state["mode"], "current_job": url})

                    process_job_url(url, browser, vision, ledger, agent_state["run_id"], agent_state["mode"])
                    jobs_processed += 1

    finally:
        browser.close()
        print("\n[Agent] Inner loop complete.")


def read_excel_safe(path):
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_excel(path).fillna("")
    except Exception:
        return pd.DataFrame()


def _coalesce_columns(df, columns):
    result = pd.Series([""] * len(df), index=df.index, dtype="object")
    for column in columns:
        if column not in df.columns:
            continue
        candidate = df[column].fillna("").astype(str).replace("nan", "")
        empty_mask = result.astype(str).str.strip() == ""
        result.loc[empty_mask] = candidate.loc[empty_mask]
    return result.fillna("")


def normalize_all_applications_df(df):
    if df.empty:
        return df

    normalized = pd.DataFrame(index=df.index)
    normalized["run_id"] = _coalesce_columns(df, ["run_id"])
    normalized["timestamp"] = _coalesce_columns(df, ["timestamp", "Date Evaluated"])
    normalized["company"] = _coalesce_columns(df, ["company", "Company"])
    normalized["title"] = _coalesce_columns(df, ["title", "Role"])
    normalized["url"] = _coalesce_columns(df, ["url", "URL"])
    normalized["decision_status"] = _coalesce_columns(df, ["decision_status", "Status"])
    normalized["decision_category"] = _coalesce_columns(df, ["decision_category"])
    normalized["decision_detail"] = _coalesce_columns(df, ["decision_detail", "reason", "Reason"])
    normalized["match_score"] = _coalesce_columns(df, ["match_score", "Match Score"])
    normalized["mode"] = _coalesce_columns(df, ["mode", "Mode"])

    status_lower = normalized["decision_status"].astype(str).str.strip().str.lower()
    detail_lower = normalized["decision_detail"].astype(str).str.strip().str.lower()

    normalized.loc[
        normalized["decision_category"].eq("") & detail_lower.str.contains("blacklist", na=False),
        "decision_category"
    ] = "blacklist"
    normalized.loc[
        normalized["decision_category"].eq("") & detail_lower.str.contains("threshold", na=False),
        "decision_category"
    ] = "score_too_low"
    normalized.loc[
        normalized["decision_category"].eq("") & status_lower.str.contains("error", na=False),
        "decision_category"
    ] = "error"
    normalized.loc[normalized["decision_category"].eq(""), "decision_category"] = "general"

    return normalized.fillna("")


def get_dashboard_summary():
    all_df = normalize_all_applications_df(read_excel_safe(ALL_APPS_PATH))
    applied_df = read_excel_safe(APPLIED_APPS_PATH)
    settings = load_settings()

    evaluated = len(all_df)
    applied = len(applied_df)
    skipped = 0
    eligible = 0
    errors = 0

    if not all_df.empty and "decision_status" in all_df.columns:
        statuses = all_df["decision_status"].astype(str).str.strip().str.lower()
        skipped = (statuses == "skipped").sum()
        eligible = statuses.isin(["eligible", "passed", "ready", "applied"]).sum()
        errors = statuses.str.contains("error", na=False).sum()

    return {
        "evaluated": int(evaluated),
        "eligible": int(eligible),
        "applied": int(applied),
        "skipped": int(skipped),
        "errors": int(errors),
        "running": agent_state["running"],
        "mode": settings.get("mode", "live"),
        "current_job": agent_state["current_job"],
        "started_at": agent_state["started_at"]
    }


# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route("/api/dashboard/summary")
def dashboard_summary():
    return jsonify(get_dashboard_summary())

@app.route("/api/applications")
def applications():
    df = normalize_all_applications_df(read_excel_safe(ALL_APPS_PATH))
    if df.empty: return jsonify([])
    return jsonify(df.iloc[::-1].to_dict(orient="records"))

@app.route("/api/applied")
def applied():
    df = read_excel_safe(APPLIED_APPS_PATH)
    if df.empty: return jsonify([])
    return jsonify(df.to_dict(orient="records"))

@app.route('/api/ledger')
def get_ledger():
    ledger = KnowledgeLedger()
    return jsonify(ledger.list_entries())


@app.route("/api/ledger/update", methods=["POST"])
def ledger_update():
    payload = request.json or {}
    question = (payload.get("question") or "").strip()
    answer = (payload.get("answer") or "").strip()
    if not question or not answer:
        return jsonify({"success": False, "message": "question and answer are required"}), 400
    try:
        ledger = KnowledgeLedger()
        if not ledger.update_answer(question, answer):
            return jsonify({"success": False, "message": "question not found in ledger"}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    sse_publish("ledger_answered", {"question": question, "answer": answer, "action": "update"})
    return jsonify({"success": True})


@app.route("/api/ledger/delete", methods=["POST"])
def ledger_delete():
    payload = request.json or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"success": False, "message": "question is required"}), 400
    try:
        ledger = KnowledgeLedger()
        if not ledger.delete_answer(question):
            return jsonify({"success": False, "message": "question not found in ledger"}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
    sse_publish("ledger_answered", {"question": question, "answer": "", "action": "delete"})
    return jsonify({"success": True})

@app.route('/api/credentials')
def get_credentials():
    df = read_excel_safe(CREDENTIALS_PATH)
    if df.empty: return jsonify([])
    return jsonify(df.to_dict(orient="records"))

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def update_settings():
    current = load_settings()
    payload = request.json or {}
    current.update(payload)
    save_settings(current)
    agent_state["mode"] = current.get("mode", "live")
    sse_publish("settings_updated", current)
    return jsonify({"success": True, "settings": current})

@app.route('/api/resume_status')
def resume_status():
    exists = os.path.exists(RESUME_PATH)
    return jsonify({"exists": exists})

@app.route("/api/preflight")
def preflight():
    """Fix #1: Pre-flight system readiness check."""
    checks = run_preflight_checks()
    ready = all(c["ok"] for c in checks)
    return jsonify({"ready": ready, "checks": checks})


@app.route("/api/agent/start", methods=["POST"])
def start_agent():
    if agent_state["running"]:
        return jsonify({"success": False, "message": "Agent already running"}), 400

    # Gate on preflight readiness
    checks = run_preflight_checks()
    failed = [c for c in checks if not c["ok"]]
    if failed:
        names = ", ".join(c["name"] for c in failed)
        return jsonify({"success": False, "message": f"Pre-flight failed: {names}"}), 400

    settings = load_settings()
    agent_state["running"] = True
    agent_state["mode"] = settings.get("mode", "live")
    agent_state["started_at"] = datetime.now(timezone.utc).isoformat()
    agent_state["run_id"] = str(uuid.uuid4())[:8]
    agent_state["current_job"] = None
    pause_event.clear()

    sse_publish("agent_status", {
        "running": True,
        "mode": agent_state["mode"],
        "started_at": agent_state["started_at"]
    })

    threading.Thread(target=run_agent, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/agent/stop", methods=["POST"])
def stop_agent():
    agent_state["running"] = False
    agent_state["current_job"] = None
    pause_event.set()
    sse_publish("agent_status", {"running": False, "mode": agent_state["mode"]})
    return jsonify({"success": True})


@app.route("/api/agent/continue_after_pause", methods=["POST"])
def continue_after_pause():
    """Resume application flow after user completes stuck form fields manually."""
    pause_event.set()
    return jsonify({"success": True})


@app.route("/api/agent/resume", methods=["POST"])
def resume_agent():
    """Fix #4: Resume agent after CAPTCHA/bot challenge resolved manually."""
    if agent_state["running"]:
        return jsonify({"success": False, "message": "Agent is already running"}), 400
    settings = load_settings()
    agent_state["running"] = True
    agent_state["mode"] = settings.get("mode", "live")
    if not agent_state.get("run_id"):
        agent_state["run_id"] = str(uuid.uuid4())[:8]
    sse_publish("agent_status", {"running": True, "mode": agent_state["mode"], "resumed": True})
    threading.Thread(target=run_agent, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/review/respond", methods=["POST"])
def review_respond():
    """Fix #3: Human approves or rejects a pending application review."""
    payload = request.json or {}
    approved = bool(payload.get("approved", False))
    review_decision["approved"] = approved
    review_event.set()
    action = "Approved" if approved else "Rejected"
    sse_publish("review_resolved", {"approved": approved, "action": action})
    return jsonify({"success": True, "approved": approved})


@app.route("/api/ledger/pending")
def ledger_pending():
    """Fix #6: Returns questions the agent couldn't answer that need user input."""
    try:
        pending = KnowledgeLedger.get_pending_questions()
        return jsonify({"pending": pending})
    except Exception as e:
        return jsonify({"pending": [], "error": str(e)})


@app.route("/api/ledger/answer", methods=["POST"])
def ledger_answer():
    """Fix #3 & #6: Accept a user-provided answer for a pending ledger question."""
    payload = request.json or {}
    question = payload.get("question", "").strip()
    answer = payload.get("answer", "").strip()
    if not question or not answer:
        return jsonify({"success": False, "message": "question and answer are required"}), 400

    # Cache to Excel
    try:
        ledger = KnowledgeLedger()
        ledger.cache_answer(question, answer)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

    # Unblock waiting agent thread if it's waiting for this answer
    ledger_answer_store["answer"] = answer
    ledger_event.set()
    sse_publish("ledger_answered", {"question": question, "answer": answer})
    return jsonify({"success": True})


@app.route("/screenshots/<path:name>")
def serve_screenshot(name):
    """Serve debug screenshots from the project root (stuck form captures only)."""
    if not re.fullmatch(r"(stuck_required_step_\d+|stuck_step_\d+)\.png", name, re.IGNORECASE):
        return jsonify({"error": "Invalid screenshot name"}), 400
    path = os.path.join(os.getcwd(), name)
    if not os.path.isfile(path):
        return jsonify({"error": "Not found"}), 404
    return send_file(path, mimetype="image/png")


@app.route("/api/ledger/clear_pending", methods=["POST"])
def ledger_clear_pending():
    """Emergency recovery endpoint for malformed or stale pending prompts."""
    KnowledgeLedger._runtime_pending_questions.clear()
    ledger_answer_store["answer"] = ""
    ledger_event.set()
    sse_publish("ledger_answered", {"question": "", "answer": ""})
    return jsonify({"success": True})


@app.route("/stream")
def stream():
    def event_stream():
        while True:
            try:
                item = event_queue.get(timeout=1.0)
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == '__main__':
    ensure_settings()
    app.run(debug=True, use_reloader=False, threaded=True)
