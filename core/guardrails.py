import json
import os

from core.config import GEMINI_API_KEY, MATCH_SCORE_THRESHOLD
from intelligence.llm_bricking import evaluate_job_match


def load_settings_safe():
    """Load settings.json safely."""
    if os.path.exists("settings.json"):
        with open("settings.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def check_blacklist(company_name: str) -> bool:
    """Returns True if the company is on the blacklist."""
    settings = load_settings_safe()
    blacklist = settings.get("blacklisted_companies", [])
    if not blacklist:
        return False
    company_lower = company_name.lower()
    for blocked in blacklist:
        if blocked.lower() in company_lower:
            return True
    return False


def check_duplicate(url: str) -> bool:
    """Returns True if this URL has already been processed."""

    def normalize_job_url(value: str) -> str:
        if not value:
            return ""
        return str(value).strip().split("?")[0]

    try:
        import pandas as pd

        from core.config import ALL_APPS_PATH

        if os.path.exists(ALL_APPS_PATH):
            df = pd.read_excel(ALL_APPS_PATH)
            normalized_target = normalize_job_url(url)
            for column in ("url", "URL"):
                if column not in df.columns:
                    continue
                normalized_values = df[column].fillna("").astype(str).map(normalize_job_url)
                if normalized_target in set(normalized_values):
                    return True
    except Exception:
        pass
    return False


def _parse_profile_data(profile_text, profile_data=None) -> dict:
    if isinstance(profile_data, dict):
        return profile_data
    if isinstance(profile_text, dict):
        return profile_text
    if not profile_text:
        return {}
    try:
        return json.loads(profile_text)
    except Exception:
        return {"raw_profile": str(profile_text)}


def _summarize_match_result(result: dict) -> str:
    summary = str(result.get("summary", "")).strip()
    if summary:
        return summary

    reasons = result.get("reasons", [])
    if isinstance(reasons, list):
        reasons = [str(reason).strip() for reason in reasons if str(reason).strip()]
        if reasons:
            return " ".join(reasons[:2])

    return "No reasoning returned."


def evaluate_efficiency(
    job_description: str,
    resume_text: str,
    profile_text: str,
    *,
    job_title: str = "Unknown",
    company_name: str = "Unknown",
    ledger_context: str = "",
    profile_data: dict | None = None,
) -> dict:
    """
    Evaluate the job description against the candidate using structured resume,
    profile, and knowledge-ledger context.
    """
    if not GEMINI_API_KEY:
        return {"score": 100, "reason": "No API key provided, bypassing efficiency scoring."}

    profile_payload = _parse_profile_data(profile_text, profile_data=profile_data)
    match_result = evaluate_job_match(
        job_description,
        resume_text,
        company_name,
        job_title,
        ledger_context=ledger_context,
        profile=profile_payload,
    )
    score = int(match_result.get("score", 0) or 0)
    reason = _summarize_match_result(match_result)
    return {"score": score, "reason": reason}


def should_apply(
    job_description: str,
    resume_text: str,
    profile_text: str,
    job_title: str = "Unknown",
    company_name: str = "Unknown",
    ledger_context: str = "",
    profile_data: dict | None = None,
) -> tuple:
    """
    Combines blacklist checks and structured match scoring.
    Returns (decision, reason, score).
    """

    def emit_decision(status, reason, match_score):
        print(f"[Guardrails] {status} - {job_title} at {company_name} ({reason})")
        print(json.dumps({
            "type": "job_decision",
            "job": f"{job_title} at {company_name}",
            "status": status,
            "reason": reason,
            "score": match_score,
        }))

    if check_blacklist(company_name):
        reason = f"Company '{company_name}' is on the blacklist."
        emit_decision("Skipped", reason, 0)
        return False, reason, 0

    settings = load_settings_safe()
    threshold = settings.get("match_threshold", MATCH_SCORE_THRESHOLD)

    eval_result = evaluate_efficiency(
        job_description,
        resume_text,
        profile_text,
        job_title=job_title,
        company_name=company_name,
        ledger_context=ledger_context,
        profile_data=profile_data,
    )
    score = eval_result.get("score", 0)

    if score <= threshold:
        reason = f"Match score {score}% is not greater than the {threshold}% threshold. {eval_result.get('reason')}"
        emit_decision("Skipped", reason, score)
        return False, reason, score

    reason = f"Match score {score}%. {eval_result.get('reason')}"
    emit_decision("Eligible", reason, score)
    return True, reason, score
