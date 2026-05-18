import os
import json
import time
import PyPDF2
from google import genai
from core.config import GEMINI_API_KEY, RESUME_PATH


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extracts all text from a given PDF file."""
    if not os.path.exists(pdf_path):
        return ""
        
    text = ""
    try:
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        print(f"Error reading PDF {pdf_path}: {e}")
    return text


def _call_gemini_with_retry(prompt: str, max_retries: int = 3) -> str:
    """Call Gemini API with automatic retry on 503/429 errors."""
    if not GEMINI_API_KEY:
        return ""
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            error_str = str(e)
            if "503" in error_str or "429" in error_str or "UNAVAILABLE" in error_str:
                wait_time = (attempt + 1) * 5
                print(f"[Intelligence] API busy (attempt {attempt+1}/{max_retries}). Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                print(f"[Intelligence] API error: {e}")
                return ""
    
    print("[Intelligence] API failed after all retries.")
    return ""


def extract_job_details_from_text(page_text: str) -> dict:
    """
    Uses Gemini Flash to extract Company Name, Job Title, and Job Description from raw page text.
    """
    prompt = f"""
    Analyze the following raw text extracted from a job posting page.
    Extract these three fields:
    1. 'company_name' - The hiring company
    2. 'role_name' - The exact job title
    3. 'job_description' - The full job description including responsibilities and requirements
    
    Output ONLY a valid JSON object with these three keys. No markdown, no explanation.
    
    Raw Text:
    {page_text[:12000]}
    """
    
    result = _call_gemini_with_retry(prompt)
    if not result:
        return {"company_name": "Unknown", "role_name": "Unknown", "job_description": ""}
    
    try:
        text = result.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[Intelligence] JSON parse error: {e}")
        return {"company_name": "Unknown", "role_name": "Unknown", "job_description": ""}


def generate_essay_response(
    question: str,
    company: str,
    role: str,
    resume_text: str,
    ledger_context: str = "",
    profile: dict = None,
    job_description: str = "",
) -> str:
    """
    Generates a compelling, personalized response to subjective form questions.
    Enriched with the full Knowledge Ledger + profile so the model behaves as if
    it knows the candidate deeply — not just from the resume.
    """
    profile_summary = ""
    if profile:
        try:
            profile_summary = "\n".join(
                f"- {k}: {v}" for k, v in profile.items()
                if isinstance(v, str) and v.strip()
            )
        except Exception:
            pass

    prompt = f"""You are ghostwriting a job application response for a candidate applying to {role} at {company}.

The form asks: "{question}"

Write a strong, concise (3-5 sentences) response that:
- Directly and specifically answers the question
- Uses concrete, quantifiable achievements from the resume or profile where possible
- Sounds genuine, confident, and human — NOT generic or templated
- Is tailored to {company} and the {role} role
- Never mentions "I am very interested" or generic phrases

CANDIDATE PROFILE:
{profile_summary or "(see resume below)"}

KNOWLEDGE LEDGER (user's saved answers to common questions):
{ledger_context or "(not available)"}

JOB DESCRIPTION:
{job_description[:4000] or "(not available)"}

RESUME:
{resume_text[:5000]}

Output ONLY the answer text. No preamble, no quotes, no explanation."""

    result = _call_gemini_with_retry(prompt)
    return result if result else "My background directly aligns with this role's requirements, and I'm excited about the opportunity to contribute."


def evaluate_job_match(job_description: str, resume_text: str, company: str, role: str,
                       ledger_context: str = "", profile: dict = None) -> dict:
    """
    Uses Gemini to score a job description against the candidate's full profile.
    Returns {score: int, reasons: list[str], should_apply: bool}
    """
    profile_summary = ""
    if profile:
        try:
            profile_summary = "\n".join(f"- {k}: {v}" for k, v in profile.items() if isinstance(v, str))
        except Exception:
            pass

    prompt = f"""You are an expert job matching AI. Evaluate how well this candidate matches the job.

ROLE: {role} at {company}

JOB DESCRIPTION:
{job_description[:6000]}

CANDIDATE RESUME:
{resume_text[:4000]}

CANDIDATE PROFILE (additional facts):
{profile_summary or "(see resume)"}

KNOWLEDGE LEDGER (candidate's stated preferences/qualifications):
{ledger_context or "(not available)"}

Score the match from 0-100. Output ONLY valid JSON:
{{
  "score": <int 0-100>,
  "reasons": ["<specific reason 1>", "<specific reason 2>", "<specific reason 3>"],
  "should_apply": <true|false>,
  "summary": "<one sentence why or why not>"
}}"""

    result = _call_gemini_with_retry(prompt)
    if not result:
        return {"score": 0, "reasons": ["API unavailable"], "should_apply": False, "summary": "Could not evaluate."}
    try:
        text = result.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception:
        return {"score": 50, "reasons": ["Parse error"], "should_apply": True, "summary": "Partial evaluation."}
