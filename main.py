import os
import json
from navigation.browser import BrowserManager
from navigation.vision_agent import VisionAgent
from core.guardrails import should_apply, check_duplicate, load_settings_safe
from intelligence.llm_bricking import extract_text_from_pdf, extract_job_details_from_text
from intelligence.knowledge_ledger import KnowledgeLedger
from workflow.tracking import log_applied_application, log_all_application
from workflow.application import apply_to_job_workflow, apply_via_google_fallback
from core.config import RESUME_PATH, PROFILE_PATH

# SSE publisher wired in by app.py
_sse_publish = None

def configure_main(sse_fn):
    global _sse_publish
    _sse_publish = sse_fn


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    return ""


def is_target_company(company_name: str, target_companies: list[str]) -> bool:
    company_lower = company_name.lower()
    return any(
        target and (target.lower() in company_lower or company_lower in target.lower())
        for target in target_companies
    )


def is_greenhouse_url(url: str) -> bool:
    url_lower = (url or "").lower()
    return any(token in url_lower for token in [
        "greenhouse.io",
        "gh_jid=",
        "gh_src=",
        "my.greenhouse.io",
    ])


def process_job_url(url: str, browser: BrowserManager, vision: VisionAgent, ledger: KnowledgeLedger, run_id: str = "test", mode: str = "live"):
    """Process a single job URL: navigate, extract, evaluate, log."""
    print(f"\n{'='*60}\nProcessing: {url[:80]}\n{'='*60}")
    
    # 0. Duplicate check
    if check_duplicate(url):
        print(f"[Agent] Already processed this URL. Skipping.")
        return
    
    try:
        settings = load_settings_safe()
        if settings.get("job_source", "greenhouse") == "greenhouse" and not is_greenhouse_url(url):
            reason = f"Skipping non-Greenhouse URL in greenhouse mode: {url}"
            print(f"[Agent] {reason}")
            log_all_application("Unknown Company", "Unknown Role", url, "Skipped", reason, run_id=run_id, mode=mode)
            return

        # 1. Navigate to the job page
        browser.navigate(url)
        vision.biological_delay(2.0, 3.5)

        # 1a. CAPTCHA / bot-detection check
        if browser.detect_challenge():
            reason = f"Bot challenge / CAPTCHA detected at {url[:80]}"
            print(f"[Agent] Blocked: {reason}")
            if _sse_publish:
                _sse_publish("bot_challenge", {"url": url, "reason": reason})
            log_all_application("Unknown Company", "Unknown Role", url, "Apply Error", reason, run_id=run_id, mode=mode)
            return
        
        # 2. Extract page text
        page_text = browser.get_page_text()
        if not page_text or len(page_text) < 100:
            print(f"[Agent] Page text too short ({len(page_text)} chars). Skipping.")
            return
            
        # 3. Extract job details via LLM
        print("[Agent] Extracting job details via Gemini...")
        details = extract_job_details_from_text(page_text)
        company_name = details.get("company_name", "Unknown Company")
        role_name = details.get("role_name", "Unknown Role")
        job_description_text = details.get("job_description", "")
        target_companies = settings.get("target_companies", [])
        
        if not job_description_text or len(job_description_text) < 50:
            print(f"[Agent] Could not extract valid JD. Skipping.")
            return
        
        print(f"[Agent] Found: {role_name} at {company_name}")

        if target_companies and not is_target_company(company_name, target_companies):
            reason = f"Company '{company_name}' not in target company filter."
            log_all_application(company_name, role_name, url, "Skipped", reason, run_id=run_id, mode=mode)
            print(f"[Agent] Skipping non-target company: {company_name}")
            return
        
        # 4. Guardrails evaluation
        resume_text = extract_text_from_pdf(RESUME_PATH)
        profile_text = load_profile()
        ledger_context = ledger.build_context(max_chars=3000) if hasattr(ledger, "build_context") else ""
        
        approved, reason, score = should_apply(
            job_description_text,
            resume_text,
            profile_text,
            role_name,
            company_name,
            ledger_context=ledger_context,
            profile_data=getattr(ledger, "profile", None),
        )
        
        if not approved:
            log_all_application(company_name, role_name, url, "Skipped", reason, run_id=run_id, mode=mode, match_score=score)
            return
            
        # 5. Log as eligible
        log_all_application(company_name, role_name, url, "Eligible", reason, run_id=run_id, mode=mode, match_score=score)
        print(f"[Agent] [OK] ELIGIBLE: {role_name} at {company_name} (Score: {score}%)")

        # 6. Apply if in live mode
        if mode == "live":
            job_context = {
                "company": company_name,
                "role": role_name,
                "job_description": job_description_text,
                "match_score": score,
            }
            success, apply_reason = apply_to_job_workflow(
                url,
                browser,
                vision,
                ledger,
                resume_text,
                profile_text,
                job_context=job_context,
            )
            if not success:
                print("[Agent] Primary apply flow failed. Trying Google fallback...")
                success, apply_reason = apply_via_google_fallback(
                    company_name,
                    role_name,
                    browser,
                    vision,
                    ledger,
                    resume_text,
                    profile_text,
                    job_context=job_context,
                )
            if success:
                log_applied_application(company_name, role_name, url, match_score=score, resume_used=RESUME_PATH, run_id=run_id, mode=mode)
                print(f"[Agent] [OK] APPLIED: {role_name} at {company_name}")
            else:
                print(f"[Agent] [FAIL] APPLY FAILED: {apply_reason}")
                log_all_application(company_name, role_name, url, "Apply Error", apply_reason, run_id=run_id, mode=mode, match_score=score)
        else:
            # In simulation mode, just log to Applied_Applications to simulate success
            log_applied_application(company_name, role_name, url, match_score=score, resume_used=RESUME_PATH, run_id=run_id, mode=mode)
            print(f"[Agent] [Simulation] Would have applied to {role_name} at {company_name}")
        
    except Exception as e:
        print(f"[Agent] Error processing {url[:60]}: {e}")


def main():
    print("Starting ApexApply - The Ultimate Unified Career Agent")
    
    ledger = KnowledgeLedger()
    browser = BrowserManager()
    vision = VisionAgent(browser)
    
    try:
        browser.start()
        # Main is only for standalone testing
        process_job_url("https://example.com/job/123", browser, vision, ledger)
    except KeyboardInterrupt:
        print("\nAgent stopped by user.")
    finally:
        browser.close()
        print("Browser closed. Exiting.")

if __name__ == "__main__":
    main()
