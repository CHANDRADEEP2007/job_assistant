import os
import pandas as pd
from datetime import datetime
from core.config import APPLIED_APPS_PATH, ALL_APPS_PATH


# Failure classification buckets drive smart retry logic in backfill
FAILURE_BUCKETS = [
    ("SESSION_EXPIRED",       ["login", "sign in", "session", "logged out", "sign_in"]),
    ("CAPTCHA_DETECTED",      ["captcha", "challenge", "checkpoint", "robot", "security check"]),
    ("UNSUPPORTED_PORTAL",    ["workday", "lever", "ashby", "no apply button", "not found", "unsupported"]),
    ("MISSING_LEDGER_ANSWER", ["unknown question", "ledger", "missing answer"]),
    ("SITE_TIMEOUT",          ["timeout", "navigation error", "net::", "timed out"]),
]

def classify_failure(reason: str) -> str:
    """Maps a free-text failure reason to a structured retry bucket."""
    reason_lower = (reason or "").lower()
    for bucket, keywords in FAILURE_BUCKETS:
        if any(kw in reason_lower for kw in keywords):
            return bucket
    return "APPLY_ERROR"

def log_applied_application(company: str, role: str, url: str, match_score: int, resume_used: str, run_id: str = "test", mode: str = "simulation"):
    """
    Appends successful applications to Excel 2: Applied_Applications.xlsx
    """
    data = {
        "run_id": [run_id],
        "Date Applied": [datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        "Company": [company],
        "Role": [role],
        "URL": [url],
        "Match Score": [match_score],
        "Resume Variant": [resume_used],
        "Mode": [mode]
    }
    _append_to_excel(APPLIED_APPS_PATH, data, "Applied")

def log_all_application(company: str, role: str, url: str, decision_status: str, decision_reason: str, match_score: int = 0, run_id: str = "test", mode: str = "simulation"):
    """
    Appends all evaluated jobs (Applied or Skipped) to Excel 3: All_Applications.xlsx
    """
    # Smart failure classification
    category = "general"
    detail = decision_reason
    if "blacklist" in decision_reason.lower():
        category = "blacklist"
    elif "below" in decision_reason.lower() and "threshold" in decision_reason.lower():
        category = "score_too_low"
    elif decision_status.lower() in ("apply error", "error"):
        category = classify_failure(decision_reason)

    data = {
        "run_id": [run_id],
        "timestamp": [datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        "company": [company],
        "title": [role],
        "url": [url],
        "decision_status": [decision_status],
        "decision_category": [category],
        "decision_detail": [detail],
        "match_score": [match_score],
        "mode": [mode]
    }
    _append_to_excel(ALL_APPS_PATH, data, "All")

def _append_to_excel(path: str, data: dict, log_name: str):
    df_new = pd.DataFrame(data)
    if not os.path.exists(path):
        try:
            df_new.to_excel(path, index=False)
            print(f"[Tracking] Created {path} and logged entry.")
        except Exception as e:
            print(f"[Tracking] Error creating {path}: {e}")
    else:
        try:
            df_existing = pd.read_excel(path)
            df_existing = df_existing.dropna(how='all', axis=1)
            df_new = df_new.dropna(how='all', axis=1)
            df_combined = pd.concat([df_existing, df_new], ignore_index=True)
            df_combined.to_excel(path, index=False)
        except Exception as e:
            print(f"[Tracking] Error updating {path}: {e}")
