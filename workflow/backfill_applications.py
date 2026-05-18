import os
import pandas as pd
import json
import time
from navigation.browser import BrowserManager
from navigation.vision_agent import VisionAgent
from intelligence.llm_bricking import extract_text_from_pdf, extract_job_details_from_text
from intelligence.knowledge_ledger import KnowledgeLedger
from workflow.application import apply_to_job_workflow, apply_via_google_fallback
from workflow.tracking import log_applied_application, log_all_application, classify_failure
from core.config import ALL_APPS_PATH, RESUME_PATH, PROFILE_PATH
from core.guardrails import load_settings_safe

# Failure categories that should NOT be retried — they require manual intervention
NON_RETRYABLE = {"SESSION_EXPIRED", "CAPTCHA_DETECTED", "UNSUPPORTED_PORTAL"}

def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    return ""


def _pick_value(row, *columns):
    for column in columns:
        value = row.get(column, "")
        if pd.notna(value):
            text = str(value).strip()
            if text and text.lower() != "nan":
                return text
    return ""


def _normalized_status_series(df):
    status = pd.Series([""] * len(df), index=df.index, dtype="object")
    for column in ("decision_status", "Status"):
        if column not in df.columns:
            continue
        series = df[column].fillna("").astype(str).str.strip()
        empty_mask = status.astype(str).str.strip() == ""
        status.loc[empty_mask] = series.loc[empty_mask]
    return status.str.lower()


def _update_application_row(df, idx, company, role, url, status, reason):
    df.at[idx, "company"] = company
    df.at[idx, "title"] = role
    df.at[idx, "url"] = url
    df.at[idx, "decision_status"] = status
    df.at[idx, "decision_detail"] = reason
    df.at[idx, "reason"] = reason

    df.at[idx, "Company"] = company
    df.at[idx, "Role"] = role
    df.at[idx, "URL"] = url
    df.at[idx, "Status"] = status
    df.at[idx, "Reason"] = reason


def _normalize_base_url(url: str) -> str:
    return str(url or "").strip().split("?")[0]


def _update_matching_rows(df, base_url, company, role, status, reason):
    if not base_url:
        return

    modern_matches = df["url"].fillna("").astype(str).map(_normalize_base_url) == base_url if "url" in df.columns else pd.Series(False, index=df.index)
    legacy_matches = df["URL"].fillna("").astype(str).map(_normalize_base_url) == base_url if "URL" in df.columns else pd.Series(False, index=df.index)
    combined = modern_matches | legacy_matches

    for idx in df.index[combined]:
        row_url = _pick_value(df.loc[idx], "url", "URL")
        _update_application_row(df, idx, company, role, row_url, status, reason)

def run_backfill(browser=None, vision=None, ledger=None):
    settings = load_settings_safe()
    threshold = settings.get("match_threshold", 60)
    print(f"=== STARTING APPLICATION BACKFILL (Score >= {threshold}%) ===")
    
    if not os.path.exists(ALL_APPS_PATH):
        print("Error: All_Applications.xlsx not found.")
        return
    
    # Reload for freshest data
    df = pd.read_excel(ALL_APPS_PATH)
    if df.empty:
        print("All_Applications.xlsx is empty.")
        return

    # Clean up column names and handle missing match_score
    if 'match_score' not in df.columns:
        print("Error: No match_score column found in Excel.")
        return

    statuses = _normalized_status_series(df)

    # Filter: score >= threshold and not yet applied
    mask = (
        (df['match_score'] >= threshold) &
        (~statuses.isin(["applied"])) &
        (statuses.isin(["", "apply error"]))
    )
    to_apply = df[mask].copy()

    # --- Smart retry: skip non-retryable failure categories ---
    if "decision_category" in to_apply.columns:
        non_retryable_mask = to_apply["decision_category"].astype(str).str.upper().isin(NON_RETRYABLE)
        skipped_count = non_retryable_mask.sum()
        if skipped_count > 0:
            print(f"[Backfill] Skipping {skipped_count} non-retryable failures (SESSION_EXPIRED / CAPTCHA / UNSUPPORTED_PORTAL).")
        to_apply = to_apply[~non_retryable_mask]

    to_apply["base_url"] = to_apply.apply(lambda row: _normalize_base_url(_pick_value(row, "url", "URL")), axis=1)
    to_apply = to_apply.drop_duplicates(subset=["base_url"])

    print(f"Found {len(to_apply)} candidates for backfill application.")

    if len(to_apply) == 0:
        return

    # Reuse or create instances
    if browser is None:
        browser = BrowserManager()
        should_close_browser = True
    else:
        should_close_browser = False

    if vision is None:
        vision = VisionAgent(browser)
    if ledger is None:
        ledger = KnowledgeLedger()
    
    resume_text = extract_text_from_pdf(RESUME_PATH)
    profile_text = load_profile()

    try:
        if not browser.page:
            browser.start()
        
        for idx, row in to_apply.iterrows():
            url = _pick_value(row, 'url', 'URL')
            base_url = _normalize_base_url(url)
            company = _pick_value(row, 'company', 'Company')
            role = _pick_value(row, 'title', 'Role')
            score = row.get('match_score', 0)
            
            # Refresh missing metadata from the live page when needed.
            if not company or not role:
                if url and "linkedin.com" in url:
                    print(f"[Backfill] Missing info for URL {url}. Navigating to re-extract...")
                    browser.navigate(url)
                    time.sleep(3)
                    page_text = browser.get_page_text()
                    details = extract_job_details_from_text(page_text)
                    company = details.get("company_name", "Unknown Company")
                    role = details.get("role_name", "Unknown Role")
                    _update_application_row(df, idx, company, role, url, _pick_value(row, 'decision_status', 'Status'), _pick_value(row, 'decision_detail', 'reason', 'Reason'))
                else:
                    print(f"[Backfill] Skipping row {idx} due to missing data and invalid URL.")
                    continue

            print(f"\n--- Backfilling: {role} at {company} (Score: {score}%) ---")
            
            # Step 1: Try direct LinkedIn -> External
            success, reason = apply_to_job_workflow(
                url,
                browser,
                vision,
                ledger,
                resume_text,
                profile_text,
                job_context={"company": company, "role": role},
            )
            
            # Step 2: Fallback to Google if LinkedIn failed
            if not success:
                print(f"[Backfill] LinkedIn Apply failed: {reason}. Trying Google fallback...")
                success, reason = apply_via_google_fallback(
                    company,
                    role,
                    browser,
                    vision,
                    ledger,
                    resume_text,
                    profile_text,
                    job_context={"company": company, "role": role},
                )
            
            # Step 3: Log result
            if success:
                print(f"[Backfill] [OK] Successfully applied to {role} at {company}")
                log_applied_application(company, role, url, match_score=score, resume_used=RESUME_PATH, run_id="backfill", mode="live")
                _update_matching_rows(df, base_url, company, role, 'Applied', reason)
            else:
                print(f"[Backfill] [FAIL] Could not apply: {reason}")
                _update_matching_rows(df, base_url, company, role, 'Apply Error', reason)

            # Save progress
            df.to_excel(ALL_APPS_PATH, index=False)
            
    except Exception as e:
        print(f"Backfill fatal error: {e}")
    finally:
        if should_close_browser:
            browser.close()
        print("\n=== BACKFILL COMPLETE ===")

if __name__ == "__main__":
    run_backfill()
