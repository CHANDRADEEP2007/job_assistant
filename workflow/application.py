from __future__ import annotations

import time
import json
import os
import re
import threading
import pandas as pd
from urllib.parse import quote_plus
from navigation.browser import BrowserManager
from navigation.vision_agent import VisionAgent
from intelligence.llm_bricking import generate_essay_response
from intelligence.knowledge_ledger import LEDGER_SOURCE_USER
from core.config import RESUME_PATH
from core.guardrails import load_settings_safe
from workflow.review_gate import request_final_review

# Injected by app.py: SSE for apply_paused, pause threading.Event, optional stop callback
_pause_sse_publish = None
_pause_event: threading.Event | None = None
_stop_agent_on_form_error = None


def configure_application(sse_fn, pause_event: threading.Event, stop_agent_callback=None):
    """Wire pause-for-human SSE and optional agent stop when stop_on_form_error is enabled."""
    global _pause_sse_publish, _pause_event, _stop_agent_on_form_error
    _pause_sse_publish = sse_fn
    _pause_event = pause_event
    _stop_agent_on_form_error = stop_agent_callback


def _maybe_stop_agent_on_form_error():
    settings = load_settings_safe()
    if settings.get("stop_on_form_error") and callable(_stop_agent_on_form_error):
        try:
            _stop_agent_on_form_error()
        except Exception:
            pass


def _form_error_return(reason: str):
    _maybe_stop_agent_on_form_error()
    return False, reason


USER_MEMORY_DEFAULTS = {
    "What country should I use on job applications?": "United States",
    "What is your current location?": "Plano, TX",
    "What city and state should I use for current location fields?": "Plano, TX",
    "What job location should the app search for?": "California",
    "How did you hear about us?": "LinkedIn",
    "Are you legally authorized to work in the United States?": "Yes",
}

SENSITIVE_FIELD_SIGNALS = [
    "authorized to work",
    "work authorization",
    "sponsorship",
    "visa",
    "immigration",
    "export control",
    "security clearance",
    "background check",
    "criminal",
    "felony",
    "disability",
    "veteran",
    "gender",
    "race",
    "ethnicity",
    "hispanic",
    "consent",
    "certify",
    "attest",
]


def _extract_years_of_experience(resume_text: str) -> str:
    """Extracts years of experience from resume professional summary using regex."""
    patterns = [
        r'(\d+)\+?\s*years?\s+of\s+(?:professional\s+)?experience',
        r'(\d+)\+?\s*years?\s+(?:of\s+)?(?:work|industry|product|pm|data|analytics)',
        r'over\s+(\d+)\s*years?',
        r'(\d+)\s*\+\s*years?\s+(?:building|leading|managing|driving)',
    ]
    text_lower = resume_text[:3000].lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            return match.group(1)
    return ""


def _extract_education_level(resume_text: str) -> str:
    """Extracts highest education level from resume text via keyword scanning."""
    text_lower = resume_text.lower()
    if any(x in text_lower for x in ["ph.d", "phd", "doctor of"]):
        return "PhD"
    if "mba" in text_lower:
        return "MBA"
    if any(x in text_lower for x in ["master of", "master's", "m.s.", " ms ", "m.eng"]):
        return "Master's Degree"
    if any(x in text_lower for x in ["bachelor", "b.tech", "b.s.", "b.a.", "b.e.", "b.sc"]):
        return "Bachelor's Degree"
    if "associate" in text_lower:
        return "Associate's Degree"
    return ""


def _lower_text(value):
    return str(value or "").strip().lower()


def _pick_profile_value(profile, *keys):
    for key in keys:
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _ensure_user_memory_defaults(ledger):
    """Seed stable user-provided facts so common Greenhouse fields do not stall."""
    for question, answer in USER_MEMORY_DEFAULTS.items():
        if not ledger.get_answer(question):
            ledger.cache_answer(question, answer, source=LEDGER_SOURCE_USER)


def _field_context(field_info: dict) -> str:
    return _lower_text(" ".join([
        field_info.get("group_text", ""),
        field_info.get("label", ""),
        field_info.get("name", ""),
        field_info.get("placeholder", ""),
    ]))


def _is_sensitive_field(field_info: dict) -> bool:
    context = _field_context(field_info)
    return any(signal in context for signal in SENSITIVE_FIELD_SIGNALS)


def _looks_like_option_label(value: str) -> bool:
    text = _lower_text(value)
    if not text:
        return True
    if re.search(r"\[[0-9a-f]{8}-[0-9a-f-]{27,}\]", text):
        return True
    if re.search(r"\[[^\]]*field\d+[^\]]*\]", text) or re.fullmatch(r"field\d+", text):
        return True
    if re.fullmatch(r"[a-z_]*\[[^\]]+\](\[[^\]]+\])+", text):
        return True
    if re.fullmatch(r"[a-z0-9_-]{20,}", text):
        return True
    simple_options = {
        "yes",
        "no",
        "y",
        "n",
        "true",
        "false",
        "agree",
        "disagree",
        "accept",
        "decline",
        "prefer not to say",
        "i do not wish to answer",
    }
    return text in simple_options


def _question_from_group_text(group_text: str) -> str:
    for line in str(group_text or "").splitlines():
        cleaned = line.strip()
        if cleaned and not _looks_like_option_label(cleaned):
            return cleaned[:180]
    return ""


def _field_question(field_info: dict) -> str:
    field_type = _lower_text(field_info.get("type"))
    group_text = str(field_info.get("group_text") or "").strip()
    if field_type in {"radio", "checkbox"}:
        group_question = _question_from_group_text(group_text)
        if group_question:
            return group_question

    for key in ("label", "placeholder", "name"):
        value = str(field_info.get(key) or "").strip()
        if (
            value
            and value.lower() not in {"text", "select", "radio", "checkbox", "input"}
            and not _looks_like_option_label(value)
        ):
            return value

    group_question = _question_from_group_text(group_text)
    if group_question:
        return group_question
    field_type_label = field_type or "field"
    return f"Required application {field_type_label}"


def _remember_form_value(job_context: dict, field_info: dict, answer: str):
    if not answer:
        return
    form_data = job_context.setdefault("__form_data", {})
    form_data[_field_question(field_info)] = answer


def _flag_review(job_context: dict, reason: str):
    flags = job_context.setdefault("__review_flags", [])
    if reason and reason not in flags:
        flags.append(reason)


def _candidate_facts(ledger, profile: dict) -> dict:
    return {
        "country": (
            ledger.get_answer("What country should I use on job applications?")
            or ledger.get_answer("country")
            or "United States"
        ),
        "current_location": (
            ledger.get_answer("What city and state should I use for current location fields?")
            or ledger.get_answer("What is your current location?")
            or _pick_profile_value(profile, "Current Location", "Location")
            or "Plano, TX"
        ),
        "job_location": ledger.get_answer("What job location should the app search for?") or "California",
        "referral_source": ledger.get_answer("How did you hear about us?") or "LinkedIn",
        "work_authorized": ledger.get_answer("Are you legally authorized to work in the United States?") or "Yes",
    }


def _is_submission_confirmed(page_text: str, browser: BrowserManager) -> bool:
    text_lower = _lower_text(page_text)
    if not text_lower:
        return False

    strong_signals = [
        "application submitted",
        "your application has been submitted",
        "thanks for applying",
        "thank you for applying",
        "we've received your application",
        "we have received your application",
    ]
    if any(signal in text_lower for signal in strong_signals):
        return True

    try:
        if browser.page and browser.page.locator("input[name='name'], input[name='email'], input[name='phone']").count() > 0:
            return False
    except Exception:
        pass

    return False


def _select_radio_option(
    frame,
    group_name: str,
    target_value: str,
    fallback_label: str = "",
    fallback_group_text: str = "",
) -> bool:
    try:
        selector = f"input[type='radio'][name=\"{group_name}\"]" if group_name else "input[type='radio']"
        radios = frame.locator(selector)
        if radios.count() == 0:
            return False
        target_lower = _lower_text(target_value)
        targets = _option_aliases(target_value)
        fallback_label_lower = _lower_text(fallback_label)
        fallback_group_lower = _lower_text(fallback_group_text)
        best_index = None
        best_score = 0
        for i in range(radios.count()):
            radio = radios.nth(i)
            if not radio.is_visible():
                continue
            details = radio.evaluate("""el => {
                let label = el.getAttribute('aria-label') || "";
                if (!label && el.id) {
                    const linked = document.querySelector(`label[for="${el.id}"]`);
                    if (linked) label = linked.innerText || "";
                }
                if (!label) {
                    const wrapped = el.closest('label');
                    if (wrapped) label = wrapped.innerText || "";
                }
                let groupText = "";
                const section = el.closest('fieldset, .application-question, .application-field, .posting-category, .field');
                if (section) groupText = section.innerText || "";
                return {
                    value: el.value || "",
                    label,
                    group_text: groupText
                };
            }""")
            choice_text = _lower_text(" ".join([
                details.get("value", ""),
                details.get("label", ""),
                details.get("group_text", ""),
            ]))

            if not group_name and (fallback_label_lower or fallback_group_lower):
                group_text_lower = _lower_text(details.get("group_text", ""))
                label_lower = _lower_text(details.get("label", ""))
                if fallback_group_lower and fallback_group_lower not in group_text_lower:
                    continue
                if fallback_label_lower and fallback_label_lower not in group_text_lower and fallback_label_lower not in label_lower:
                    continue

            score = _score_option_text(choice_text, targets)

            if score > best_score:
                best_score = score
                best_index = i

        if best_index is not None and best_score > 0:
            radios.nth(best_index).check(force=True)
            time.sleep(0.5)
            return True
    except Exception:
        return False
    return False


def _select_option_by_text(field, target_value: str) -> bool:
    try:
        targets = _option_aliases(target_value)
        if not targets:
            return False

        options = field.evaluate("""el => Array.from(el.options || []).map(option => ({
            value: option.value || "",
            label: (option.label || option.textContent || "").trim()
        }))""")
        if not options:
            return False

        best = None
        best_score = 0
        for option in options:
            label_lower = _lower_text(option.get("label"))
            value_lower = _lower_text(option.get("value"))
            option_text = " ".join([label_lower, value_lower]).strip()
            score = _score_option_text(option_text, targets)
            if score > best_score:
                best_score = score
                best = option

        if not best or best_score == 0:
            return False

        if best.get("value"):
            field.select_option(value=best["value"])
        else:
            field.select_option(label=best["label"])
        time.sleep(0.5)
        return True
    except Exception:
        return False


def _option_aliases(target_value: str) -> list[str]:
    value = _lower_text(target_value)
    if not value:
        return []
    aliases = [value]
    alias_map = {
        "usa": ["united states", "united states of america", "us", "u.s.", "u.s.a."],
        "united states": ["usa", "united states of america", "us", "u.s.", "u.s.a."],
        "linkedin": ["linked in", "linkedin - job posting", "linkedin job posting", "linkedin jobs"],
        "plano, tx": ["plano", "plano tx", "plano texas"],
        "california": ["ca", "california, united states", "california, usa"],
    }
    aliases.extend(alias_map.get(value, []))
    if "," in value:
        aliases.append(value.replace(",", ""))
        aliases.append(value.split(",", 1)[0].strip())
    return list(dict.fromkeys(alias for alias in aliases if alias))


def _score_option_text(option_text: str, targets: list[str]) -> int:
    if not option_text:
        return 0
    best = 0
    for target in targets:
        if option_text == target:
            best = max(best, 5)
        elif target in option_text:
            best = max(best, 4)
        elif option_text in target:
            best = max(best, 3)
        else:
            target_tokens = [token for token in target.replace("/", " ").replace("-", " ").split() if token]
            if target_tokens and all(token in option_text for token in target_tokens):
                best = max(best, 2)
    return best


def _choose_visible_suggestion(frame, target_value: str) -> bool:
    targets = _option_aliases(target_value)
    if not targets:
        return False
    selectors = [
        "[role='option']",
        "[role='listbox'] *",
        ".select2-results__option",
        ".dropdown-location",
        ".dropdown-menu li",
        ".pac-item",
        "li",
    ]
    try:
        best = None
        best_score = 0
        for selector in selectors:
            options = frame.locator(selector)
            count = min(options.count(), 40)
            for i in range(count):
                option = options.nth(i)
                if not option.is_visible():
                    continue
                text = _lower_text(option.inner_text(timeout=500))
                score = _score_option_text(text, targets)
                if score > best_score:
                    best = option
                    best_score = score
        if best and best_score > 0:
            best.scroll_into_view_if_needed(timeout=1000)
            best.click()
            time.sleep(0.5)
            return True
    except Exception:
        return False
    return False


def _fill_text_or_autocomplete(frame, field, value: str) -> bool:
    if not value:
        return False
    try:
        field.click()
        field.fill("")
        field.type(str(value), delay=60)
        time.sleep(0.8)
        if _choose_visible_suggestion(frame, value):
            return True
        for key in ("ArrowDown", "Enter"):
            field.press(key)
            time.sleep(0.3)
        return True
    except Exception:
        try:
            field.fill(str(value))
            return True
        except Exception:
            return False


def _extract_select_options(field) -> list[str]:
    try:
        options = field.evaluate("""el => Array.from(el.options || []).map(option => ({
            value: option.value || "",
            label: (option.label || option.textContent || "").trim()
        }))""")
    except Exception:
        return []

    normalized = []
    for option in options or []:
        text = str(option.get("label") or option.get("value") or "").strip()
        if not text:
            continue
        if text.lower() in {"select", "select one", "choose"}:
            continue
        if text not in normalized:
            normalized.append(text)
    return normalized


def _extract_radio_options(frame, group_name: str) -> list[str]:
    if not group_name:
        return []
    try:
        radios = frame.locator(f"input[type='radio'][name=\"{group_name}\"]")
        options = []
        for i in range(radios.count()):
            radio = radios.nth(i)
            details = radio.evaluate("""el => {
                let label = el.getAttribute('aria-label') || "";
                if (!label && el.id) {
                    const linked = document.querySelector(`label[for="${el.id}"]`);
                    if (linked) label = linked.innerText || "";
                }
                if (!label) {
                    const wrapped = el.closest('label');
                    if (wrapped) label = wrapped.innerText || "";
                }
                return {
                    value: el.value || "",
                    label
                };
            }""")
            text = str(details.get("label") or details.get("value") or "").strip()
            if text and text not in options:
                options.append(text)
        return options
    except Exception:
        return []


def _checkbox_answer_to_bool(answer: str):
    text = _lower_text(answer)
    if any(token in text for token in ["yes", "true", "agree", "accept", "consent", "acknowledge"]):
        return True
    if any(token in text for token in ["no", "false", "decline", "do not", "don't"]):
        return False
    return None


def _apply_dynamic_answer(frame, field, field_info: dict, answer: str) -> bool:
    if not answer:
        return False

    field_type = _lower_text(field_info.get("type"))

    try:
        if field_type in ["select", "select-one"]:
            return _select_option_by_text(field, answer)
        if field_type == "radio":
            return _select_radio_option(
                frame,
                field_info.get("name", ""),
                answer,
                fallback_label=field_info.get("label", ""),
                fallback_group_text=field_info.get("group_text", ""),
            )
        if field_type == "checkbox":
            bool_answer = _checkbox_answer_to_bool(answer)
            if bool_answer is None:
                return False
            if bool_answer:
                field.check(force=True)
            else:
                field.uncheck(force=True)
            time.sleep(0.5)
            return True
        if field_type == "number":
            numbers = re.findall(r"\d+", str(answer).replace(",", ""))
            if numbers:
                field.fill(numbers[0])
                return True
        field.fill(str(answer))
        return True
    except Exception:
        return False


def _ask_unknown_field_answer(field_info: dict, ledger, resume_text: str, job_context: dict, options=None) -> str:
    question = _field_question(field_info)
    if not question:
        return ""

    job_context = job_context or {}
    sensitive = _is_sensitive_field(field_info)
    if sensitive:
        _flag_review(job_context, f"Sensitive question answered by user: {question}")
    else:
        _flag_review(job_context, f"Unknown required question answered by user: {question}")

    answer = ledger.ask_user_and_cache(
        question,
        resume_text=resume_text,
        company=job_context.get("company", ""),
        role=job_context.get("role", ""),
        job_description=job_context.get("job_description", ""),
        field_type=field_info.get("type", "text"),
        options=options or [],
        timeout_seconds=300,
        allow_ai_fallback=not sensitive,
    )
    _remember_form_value(job_context, field_info, answer)
    return answer


def _lookup_exact_ledger_answer(ledger, *prompts) -> str:
    ledger_map = getattr(ledger, "ledger", {}) or {}
    normalized = {
        _lower_text(str(key).rstrip("?:")): str(value).strip()
        for key, value in ledger_map.items()
        if str(value).strip()
    }
    for prompt in prompts:
        prompt_key = _lower_text(str(prompt).strip().rstrip("?:"))
        if prompt_key and prompt_key in normalized:
            return normalized[prompt_key]
    return ""


def _resolve_custom_field(field_info: dict, ledger, profile: dict):
    context = _field_context(field_info)
    field_type = field_info.get("type", "")
    facts = _candidate_facts(ledger, profile)

    if not context:
        return None

    if "country" in context:
        value = facts["country"]
        return {"kind": "select" if field_type.startswith("select") else "text", "value": value}

    if "current location" in context or "location (city" in context or "location city" in context:
        return {"kind": "autocomplete", "value": facts["current_location"]}

    if "job location" in context or "preferred location" in context:
        return {"kind": "autocomplete", "value": facts["job_location"]}

    if "hear about" in context or "how did you find" in context or "referral source" in context or "where did you hear" in context or "how did you learn" in context:
        value = facts["referral_source"]
        return {"kind": "select" if field_type.startswith("select") else "text", "value": value}

    if "reasonable accommodation" in context:
        return {"kind": "radio", "value": "No"}

    if "authorized to work" in context or "work authorization" in context:
        answer = facts["work_authorized"]
        value = "Yes" if "yes" in _lower_text(answer) else "No"
        if field_type.startswith("select"):
            return {"kind": "select", "value": value}
        if field_type == "radio":
            return {"kind": "radio", "value": value}
        return {"kind": "text", "value": value}

    if "require our company to file a petition" in context or "require sponsorship" in context or "immigration status" in context:
        answer = ledger.get_answer("Will you now or in the future require sponsorship for employment visa status?") or "Yes"
        value = "Yes" if "yes" in _lower_text(answer) else "No"
        if field_type.startswith("select"):
            return {"kind": "select", "value": value}
        return {"kind": "radio", "value": value}

    if "where did you first hear about tinder or this job" in context:
        return {"kind": "select", "value": "LinkedIn - Job Posting"}

    if "what are your pronouns" in context:
        return {"kind": "skip"}

    if "hybrid position" in context and "west hollywood" in context and "preferred option" in context:
        onsite = ledger.get_answer("Are you willing to go into the office 3 times a week?") or ""
        relocate = ledger.get_answer("Are you willing to relocate?") or ""
        if "yes" in _lower_text(onsite):
            return {"kind": "radio", "value": "Willing to go into the office 3 times a week"}
        if "yes" in _lower_text(relocate):
            return {"kind": "radio", "value": "Willing to relocate"}
        return {"kind": "radio", "value": "Looking for 100% remote"}

    exact_answer = _lookup_exact_ledger_answer(
        ledger,
        field_info.get("label", ""),
        field_info.get("name", ""),
        field_info.get("placeholder", ""),
    )
    if exact_answer:
        if field_type.startswith("select"):
            return {"kind": "select", "value": exact_answer}
        if field_type == "radio":
            return {"kind": "radio", "value": exact_answer}
        if field_type == "checkbox":
            return {"kind": "checkbox", "value": exact_answer}
        return {"kind": "text", "value": exact_answer}

    return None


def _extract_field_info(field):
    return field.evaluate("""el => {
        let label = "";
        if (el.id) {
            const linked = document.querySelector(`label[for="${el.id}"]`);
            if (linked) label = linked.innerText || "";
        }
        if (!label) {
            let parent = el.parentElement;
            while (parent && !label) {
                if (parent.tagName === 'LABEL') label = parent.innerText || "";
                else parent = parent.parentElement;
            }
        }
        let groupText = "";
        const section = el.closest('fieldset, .application-question, .application-field, .posting-category, .field');
        if (section) groupText = section.innerText || "";
        return {
            name: el.name || "",
            placeholder: el.placeholder || "",
            label: label || el.getAttribute('aria-label') || "",
            type: el.type || el.tagName.toLowerCase() || "text",
            value: el.value || "",
            checked: !!el.checked,
            required: !!el.required || el.getAttribute('aria-required') === 'true',
            group_text: groupText
        };
    }""")


def _collect_pending_required_fields(frame):
    try:
        pending = frame.evaluate("""() => {
            const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style &&
                    style.visibility !== 'hidden' &&
                    style.display !== 'none' &&
                    rect.width > 0 &&
                    rect.height > 0;
            };

            const getLabel = (el) => {
                let label = "";
                if (el.id) {
                    const linked = document.querySelector(`label[for="${el.id}"]`);
                    if (linked) label = linked.innerText || "";
                }
                if (!label) {
                    const wrapped = el.closest('label');
                    if (wrapped) label = wrapped.innerText || "";
                }
                if (!label) label = el.getAttribute('aria-label') || el.placeholder || el.name || el.type || el.tagName;
                return (label || "").replace(/\\s+/g, " ").trim();
            };

            const fields = Array.from(document.querySelectorAll(
                "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='reset']), textarea, select"
            ));
            const pending = [];
            const handledRadioNames = new Set();

            for (const el of fields) {
                if (!isVisible(el) || el.disabled) continue;

                const type = (el.type || el.tagName || "").toLowerCase();
                const section = el.closest('fieldset, .application-question, .application-field, .posting-category, .field');
                const sectionText = section ? (section.innerText || "") : "";
                const label = getLabel(el);
                const required = !!el.required || el.getAttribute('aria-required') === 'true' || label.includes('*') || sectionText.includes('*');
                if (!required) continue;

                let unanswered = false;
                if (type === 'radio') {
                    const groupName = el.name || `radio-${label}`;
                    if (handledRadioNames.has(groupName)) continue;
                    handledRadioNames.add(groupName);
                    const group = fields.filter(candidate =>
                        (candidate.type || "").toLowerCase() === 'radio' &&
                        (candidate.name || `radio-${getLabel(candidate)}`) === groupName &&
                        isVisible(candidate)
                    );
                    unanswered = group.length > 0 && !group.some(candidate => candidate.checked);
                } else if (type === 'checkbox') {
                    unanswered = !el.checked;
                } else if (el.tagName === 'SELECT') {
                    unanswered = !(el.value || "").trim();
                } else {
                    unanswered = !(el.value || "").trim();
                }

                if (unanswered && !pending.includes(label)) pending.push(label);
            }

            return pending.slice(0, 10);
        }""")
        return pending or []
    except Exception:
        return []


def _current_frames(browser: BrowserManager):
    try:
        return [browser.page.main_frame] + browser.page.main_frame.child_frames
    except Exception:
        return []


def _extract_file_field_context(file_input) -> str:
    try:
        details = file_input.evaluate("""el => {
            let label = "";
            if (el.id) {
                const linked = document.querySelector(`label[for="${el.id}"]`);
                if (linked) label = linked.innerText || "";
            }
            if (!label) {
                const wrapped = el.closest('label');
                if (wrapped) label = wrapped.innerText || "";
            }
            let groupText = "";
            const section = el.closest('fieldset, .application-question, .application-field, .posting-category, .field');
            if (section) groupText = section.innerText || "";
            return {
                name: el.name || "",
                id: el.id || "",
                accept: el.accept || "",
                aria_label: el.getAttribute('aria-label') || "",
                placeholder: el.placeholder || "",
                label,
                group_text: groupText
            };
        }""")
        return _lower_text(" ".join([
            details.get("name", ""),
            details.get("id", ""),
            details.get("accept", ""),
            details.get("aria_label", ""),
            details.get("placeholder", ""),
            details.get("label", ""),
            details.get("group_text", ""),
        ]))
    except Exception:
        return ""


def _should_upload_resume(file_input) -> bool:
    context = _extract_file_field_context(file_input)
    if not context:
        return False

    cover_letter_signals = [
        "cover letter",
        "cover-letter",
        "motivation letter",
        "supporting statement",
        "additional document",
        "additional attachment",
        "portfolio",
        "writing sample",
    ]
    if any(signal in context for signal in cover_letter_signals):
        return False

    resume_signals = [
        "resume",
        "cv",
        "curriculum vitae",
    ]
    return any(signal in context for signal in resume_signals)


def _fill_visible_fields(frame, ledger, resume_text, profile: dict, values: dict, job_context: dict) -> int:
    filled_count = 0
    handled_radio_groups = set()
    facts = _candidate_facts(ledger, profile)

    try:
        inputs = frame.locator("input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='reset']), textarea, select")
        input_count = inputs.count()
    except Exception:
        return 0

    for i in range(input_count):
        try:
            field = inputs.nth(i)
            if not field.is_visible() or not field.is_enabled():
                continue

            try:
                field.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass

            field_info = _extract_field_info(field)
            field_type = _lower_text(field_info.get("type"))
            context = _field_context(field_info)

            if field_type not in ["radio", "checkbox", "file"] and str(field_info.get("value", "")).strip():
                continue

            if field_type == "checkbox" and field_info.get("checked"):
                continue

            if field_type == "radio":
                group_name = field_info.get("name", "")
                if group_name in handled_radio_groups:
                    continue
                if group_name:
                    try:
                        if frame.locator(f"input[type='radio'][name=\"{group_name}\"]:checked").count() > 0:
                            handled_radio_groups.add(group_name)
                            continue
                    except Exception:
                        pass

            changed = False

            if "preferred name" in context and values["preferred_name"]:
                field.fill(values["preferred_name"])
                changed = True
            elif "full name" in context and values["full_name"]:
                field.fill(values["full_name"])
                changed = True
            elif "first name" in context and values["first_name"]:
                field.fill(values["first_name"])
                changed = True
            elif "last name" in context and values["last_name"]:
                field.fill(values["last_name"])
                changed = True
            elif "email" in context and values["email"]:
                field.fill(values["email"])
                changed = True
            elif "phone" in context and values["phone"]:
                field.fill(values["phone"])
                changed = True
            elif "current company" in context and values["company"]:
                field.fill(values["company"])
                changed = True
            elif ("current title" in context or "job title" in context) and values.get("current_title"):
                field.fill(values["current_title"])
                changed = True
            elif "linkedin" in context and values["linkedin"]:
                field.fill(values["linkedin"])
                changed = True
            elif "twitter" in context or "github" in context or "portfolio" in context or "other website" in context:
                continue
            elif "current location" in context or "location (city" in context or "location city" in context:
                changed = _fill_text_or_autocomplete(frame, field, facts["current_location"])
                if changed:
                    _remember_form_value(job_context, field_info, facts["current_location"])
            elif "job location" in context or "preferred location" in context:
                changed = _fill_text_or_autocomplete(frame, field, facts["job_location"])
                if changed:
                    _remember_form_value(job_context, field_info, facts["job_location"])
            elif "address" in context:
                field.fill(facts["current_location"])
                changed = True
            elif "city" in context:
                changed = _fill_text_or_autocomplete(frame, field, facts["current_location"])
                if changed:
                    _remember_form_value(job_context, field_info, facts["current_location"])
            elif "state" in context and "united states" not in context:
                state_value = facts["current_location"].split(",")[-1].strip() if "," in facts["current_location"] else "TX"
                if field_type in ["select", "select-one"]:
                    changed = _select_option_by_text(field, state_value)
                else:
                    field.fill(state_value)
                    changed = True
                if changed:
                    _remember_form_value(job_context, field_info, state_value)
            elif "country" in context:
                if field_type in ["select", "select-one"]:
                    changed = _select_option_by_text(field, facts["country"]) or _select_option_by_text(field, "USA")
                else:
                    field.fill(facts["country"])
                    changed = True
                if changed:
                    _remember_form_value(job_context, field_info, facts["country"])
            elif "authorized to work" in context or "work authorization" in context:
                value = "Yes" if "yes" in _lower_text(facts["work_authorized"]) else "No"
                if field_type == "radio":
                    changed = _select_radio_option(
                        frame,
                        field_info.get("name", ""),
                        value,
                        fallback_label=field_info.get("label", ""),
                        fallback_group_text=field_info.get("group_text", ""),
                    )
                    if changed and field_info.get("name"):
                        handled_radio_groups.add(field_info["name"])
                elif field_type in ["select", "select-one"]:
                    changed = _select_option_by_text(field, value)
                else:
                    field.fill(value)
                    changed = True
                if changed:
                    _remember_form_value(job_context, field_info, value)
            elif "hear about" in context or "how did you find" in context or "referral source" in context or "where did you hear" in context or "how did you learn" in context:
                source = facts["referral_source"]
                if field_type in ["select", "select-one"]:
                    changed = (_select_option_by_text(field, source) or
                               _select_option_by_text(field, "LinkedIn - Job Posting") or
                               _select_option_by_text(field, "Job Board") or
                               _select_option_by_text(field, "Online"))
                else:
                    changed = _fill_text_or_autocomplete(frame, field, source)
                if changed:
                    _remember_form_value(job_context, field_info, source)
            elif "salary" in context or "compensation" in context or "desired pay" in context or "expected salary" in context or "pay range" in context:
                salary = ledger.get_answer("What is your desired salary?") or ledger.get_answer("What is your desired salary for this specific tier?")
                if not salary:
                    salary = ledger.ask_user_and_cache(
                        "What is your desired salary range? (e.g., 130000-160000 or 150000)",
                        resume_text=resume_text,
                        company=(job_context or {}).get("company", ""),
                        role=(job_context or {}).get("role", ""),
                        job_description=(job_context or {}).get("job_description", ""),
                        field_type=field_type,
                    )
                # For numeric fields extract first number from range like "130000-160000"
                nums = re.findall(r'\d+', str(salary).replace(",", ""))
                fill_val = nums[0] if nums else str(salary)
                if field_type in ["number"]:
                    field.fill(fill_val)
                else:
                    field.fill(str(salary))
                changed = True

            elif ("years" in context and ("experience" in context or "work" in context)) or "how many years" in context:
                years = ledger.get_answer("How many years of experience do you have?") or ledger.get_answer("How many years of work experience do you have?")
                if not years:
                    years = _extract_years_of_experience(resume_text)
                    if years:
                        ledger.cache_answer("How many years of experience do you have?", years, source=LEDGER_SOURCE_USER)
                        print(f"[Application] Extracted years of experience from resume: {years}")
                    else:
                        years = ledger.ask_user_and_cache(
                            "How many years of professional experience do you have? (number only, e.g., 6)",
                            resume_text=resume_text,
                            company=(job_context or {}).get("company", ""),
                            role=(job_context or {}).get("role", ""),
                            job_description=(job_context or {}).get("job_description", ""),
                            field_type="number",
                        )
                nums = re.findall(r'\d+', str(years or "5"))
                field.fill(nums[0] if nums else "5")
                changed = True

            elif "education" in context or "degree" in context or "highest level" in context or "highest education" in context:
                edu = ledger.get_answer("What is your highest level of education?")
                if not edu:
                    edu = _extract_education_level(resume_text)
                    if edu:
                        ledger.cache_answer("What is your highest level of education?", edu, source=LEDGER_SOURCE_USER)
                        print(f"[Application] Extracted education level from resume: {edu}")
                    else:
                        edu = ledger.ask_user_and_cache(
                            "What is your highest level of education? (e.g., Bachelor's Degree, Master's Degree, MBA, PhD)",
                            resume_text=resume_text,
                            company=(job_context or {}).get("company", ""),
                            role=(job_context or {}).get("role", ""),
                            job_description=(job_context or {}).get("job_description", ""),
                            field_type=field_type,
                        )
                if field_type in ["select", "select-one"]:
                    changed = _select_option_by_text(field, edu) or _select_option_by_text(field, edu.split("'")[0])
                else:
                    field.fill(edu)
                    changed = True

            elif ("graduation" in context or "graduated" in context) and "year" in context:
                grad_year = ledger.get_answer("What year did you graduate?")
                if not grad_year:
                    if field_info.get("required"):
                        grad_year = ledger.ask_user_and_cache(
                            "What year did you graduate from your most recent degree? (e.g., 2018)",
                            resume_text=resume_text,
                            company=(job_context or {}).get("company", ""),
                            role=(job_context or {}).get("role", ""),
                            job_description=(job_context or {}).get("job_description", ""),
                            field_type="number",
                        )
                    else:
                        continue  # Skip optional graduation year
                if grad_year:
                    nums = re.findall(r'\d{4}', str(grad_year))
                    field.fill(nums[0] if nums else str(grad_year))
                    changed = True

            elif ((field_type == "textarea" and field_info.get("label", "").strip() and "what are your pronouns" not in context) or "why" in context or "tell us" in context):
                label = field_info.get("label", "") or field_info.get("name", "") or field_info.get("placeholder", "")
                print(f"[Application] Generating personalized response for: {label}")
                ledger_ctx = ledger.build_context(max_chars=3000) if hasattr(ledger, "build_context") else ""
                response = generate_essay_response(
                    label,
                    (job_context or {}).get("company", "Target Company"),
                    (job_context or {}).get("role", "Product Manager"),
                    resume_text,
                    ledger_context=ledger_ctx,
                    profile=profile,
                    job_description=(job_context or {}).get("job_description", ""),
                )
                field.fill(response)
                changed = True
            else:
                resolution = _resolve_custom_field(field_info, ledger, profile)
                if resolution:
                    if resolution["kind"] == "radio" and field_type == "radio":
                        changed = _select_radio_option(
                            frame,
                            field_info.get("name", ""),
                            resolution["value"],
                            fallback_label=field_info.get("label", ""),
                            fallback_group_text=field_info.get("group_text", ""),
                        )
                        if changed and field_info.get("name"):
                            handled_radio_groups.add(field_info["name"])
                    elif resolution["kind"] == "select" and field_type in ["select", "select-one"]:
                        changed = _select_option_by_text(field, resolution["value"])
                    elif resolution["kind"] == "autocomplete" and field_type not in ["radio", "checkbox", "file"]:
                        changed = _fill_text_or_autocomplete(frame, field, resolution["value"])
                    elif resolution["kind"] == "checkbox" and field_type == "checkbox":
                        changed = _apply_dynamic_answer(frame, field, field_info, resolution["value"])
                    elif resolution["kind"] == "text" and field_type not in ["radio", "checkbox", "file"]:
                        field.fill(str(resolution["value"]))
                        changed = True
                    elif resolution["kind"] == "skip":
                        continue
                elif field_info.get("required") and field_type != "file":
                    # Unknown required field — ask user via ledger toast and save to memory
                    q = _field_question(field_info)
                    if q:
                        options = []
                        if field_type in ["select", "select-one"]:
                            options = _extract_select_options(field)
                        elif field_type == "radio":
                            options = _extract_radio_options(frame, field_info.get("name", ""))
                        print(f"[Application] Unknown required field: '{q}' — asking user.")
                        answer = _ask_unknown_field_answer(field_info, ledger, resume_text, job_context, options=options)
                        changed = _apply_dynamic_answer(frame, field, field_info, answer)
                        if changed and field_type == "radio" and field_info.get("name"):
                            handled_radio_groups.add(field_info["name"])

            if changed:
                filled_count += 1
                time.sleep(0.5)
        except Exception:
            continue

    return filled_count


def _page_looks_like_application_form(browser: BrowserManager) -> bool:
    try:
        if not browser.page:
            return False
        if browser.page.locator("button:has-text('SUBMIT APPLICATION'), button:has-text('Submit Application')").count() > 0:
            return True
        if browser.page.locator("input[name='name'], input[name='email'], input[name='phone']").count() >= 2:
            return True
    except Exception:
        return False
    return False


def _is_final_submit_selector(selector: str) -> bool:
    selector_lower = _lower_text(selector)
    return "submit" in selector_lower or "confirm" in selector_lower


def _maybe_request_review_before_submit(job_context: dict) -> bool:
    flags = (job_context or {}).get("__review_flags", [])
    if not flags:
        return True

    company = (job_context or {}).get("company", "Unknown Company")
    role = (job_context or {}).get("role", "Unknown Role")
    score = int((job_context or {}).get("match_score", 0) or 0)
    form_data = dict((job_context or {}).get("__form_data", {}))
    form_data["Review reason"] = "; ".join(flags)
    print(f"[Review Gate] Review needed before submit: {'; '.join(flags)}")
    return request_final_review(company, role, form_data, score)

def apply_to_job_workflow(url, browser: BrowserManager, vision: VisionAgent, ledger, resume_text, profile_json, job_context=None):
    """
    Orchestrates the application process on an external site.
    """
    print(f"[Application] Starting application flow for {url[:50]}...")
    
    try:
        # 1. Navigate to the URL
        browser.navigate(url)
        time.sleep(3)
        browser.sync_page()

        if _page_looks_like_application_form(browser):
            print("[Application] Direct application form detected. Proceeding to form filling.")
            return _handle_application_form(browser, vision, ledger, resume_text, profile_json, job_context=job_context)

        # 2. Look for the initial 'Apply' button on LinkedIn
        settings = load_settings_safe()
        allow_easy_apply_only = settings.get("allow_easy_apply_only", False)

        if allow_easy_apply_only:
            apply_selectors = [
                "button:has-text('Easy Apply')",
                "a:has-text('Easy Apply')",
            ]
        else:
            apply_selectors = [
                "button.jobs-apply-button:not(:has-text('Easy'))",
                "a.jobs-apply-button:not(:has-text('Easy'))",
                "button:has-text('Apply Now')",
                "a:has-text('Apply Now')",
                "button:has-text('Apply')",
                "a:has-text('Apply')",
            ]
        
        clicked = False
        for selector in apply_selectors:
            if browser.page.locator(selector).count() > 0:
                # Check if visible
                btn = browser.page.locator(selector).first
                if btn.is_visible():
                    print(f"[Application] Found apply button: {selector}")
                    btn.click()
                    clicked = True
                    break
        
        if not clicked:
            # Fallback: check if we are already on an external site or if we can click ANY apply text
            if "linkedin.com" not in browser.page.url:
                print("[Application] Already on external site. Proceeding to form filling.")
                clicked = True
            else:
                print("[Application] No direct Apply button found. Searching visible actions...")
                try:
                    fallback_result = browser.page.evaluate(
                        """(easyOnly) => {
                            const isVisible = (el) => {
                                const style = window.getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return style &&
                                    style.visibility !== 'hidden' &&
                                    style.display !== 'none' &&
                                    rect.width > 0 &&
                                    rect.height > 0;
                            };

                            const elements = Array.from(document.querySelectorAll('a, button, [role="button"]'));
                            const match = elements.find((el) => {
                                const text = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
                                if (!isVisible(el) || !text.includes('apply')) return false;
                                if (easyOnly) return text.includes('easy apply');
                                return !text.includes('easy apply');
                            });

                            if (!match) return null;

                            if (match.tagName === 'A' && match.href) {
                                return { action: 'navigate', href: match.href };
                            }

                            match.click();
                            return { action: 'clicked' };
                        }""",
                        allow_easy_apply_only,
                    )
                    if fallback_result:
                        if fallback_result.get("action") == "navigate" and fallback_result.get("href"):
                            browser.navigate(fallback_result["href"])
                        clicked = True
                except Exception:
                    pass
        
        if not clicked:
            return False, "Could not trigger apply button"
            
        # 3. Wait for redirect
        time.sleep(5) 
        browser.sync_page()
        
        # 4. Handle external portal
        return _handle_application_form(browser, vision, ledger, resume_text, profile_json, job_context=job_context)

    except Exception as e:
        return False, f"Application error: {e}"

def apply_via_google_fallback(company, title, browser: BrowserManager, vision: VisionAgent, ledger, resume_text, profile_json, job_context=None):
    """
    Searches Google for the job and tries to apply via the first relevant result.
    """
    print(f"[Application] [Google Fallback] Searching for '{title}' at '{company}'...")
    settings = load_settings_safe()
    if settings.get("job_source", "greenhouse") == "greenhouse":
        query = quote_plus(f'"{company}" "{title}" greenhouse apply')
    else:
        query = quote_plus(f'"{company}" "{title}" careers apply')
    google_url = f"https://www.google.com/search?q={query}"
    
    try:
        browser.navigate(google_url)
        time.sleep(3)
        
        # Find first non-sponsored result that looks like a career site
        # Avoid LinkedIn if we are failing there
        best_link = browser.page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a'));
                const target = links.find(l => {
                    const h = l.href.toLowerCase();
                    const text = l.innerText.toLowerCase();
                    if (h.includes('google.com') || h.includes('linkedin.com')) return false;
                    return h.includes('workday') || h.includes('lever.co') || h.includes('greenhouse.io') || h.includes('gh_jid=') ||
                           h.includes('careers') || h.includes('jobs') || text.includes('apply');
                });
                return target ? target.href : null;
            }
        """)
        
        if not best_link:
            # Try any first result if no portal detected
            best_link = browser.page.evaluate("""
                () => {
                    const firstResult = document.querySelector('h3');
                    return firstResult ? firstResult.closest('a').href : null;
                }
            """)

        if best_link:
            print(f"[Application] [Google Fallback] Found link: {best_link}")
            fallback_context = dict(job_context or {})
            fallback_context.setdefault("company", company)
            fallback_context.setdefault("role", title)
            return apply_to_job_workflow(
                best_link,
                browser,
                vision,
                ledger,
                resume_text,
                profile_json,
                job_context=fallback_context,
            )
        else:
            return False, "No relevant links found on Google"
            
    except Exception as e:
        return False, f"Google fallback error: {e}"

def _handle_application_form(browser, vision, ledger, resume_text, profile_json, job_context=None):
    """
    Iteratively fills a multi-page application form.
    """
    max_steps = 15
    profile = json.loads(profile_json) if isinstance(profile_json, str) else profile_json
    job_context = job_context or {}
    if _pause_event is not None:
        _pause_event.clear()
    _ensure_user_memory_defaults(ledger)
    full_name = _pick_profile_value(profile, "Full Name")
    preferred_name = _pick_profile_value(profile, "Preferred Name")
    first_name = full_name.split()[0] if full_name else ""
    last_name = full_name.split()[-1] if full_name else ""
    email = _pick_profile_value(profile, "Email")
    phone = _pick_profile_value(profile, "Phone")
    linkedin = _pick_profile_value(profile, "LinkedIn")
    company = _pick_profile_value(profile, "Current Company")
    current_title = _pick_profile_value(profile, "Current Title")
    form_values = {
        "full_name": full_name,
        "preferred_name": preferred_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "linkedin": linkedin,
        "company": company,
        "current_title": current_title,
    }
    stalled_signature = ""
    stalled_count = 0
    
    for step in range(max_steps):
        print(f"[Application] Step {step+1}: Analyzing page...")
        
        # Switch to latest page if multiple tabs open
        browser.sync_page()

        # Keep the page stable instead of bouncing top/bottom each cycle.
        try:
            browser.page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        
        page_text = browser.get_page_text()
        
        if _is_submission_confirmed(page_text, browser):
            print("[Application] [OK] Submission confirmed!")
            return True, "Submitted"

        # Check for resume upload
        file_inputs = browser.page.locator("input[type='file']")
        if file_inputs.count() > 0:
            for i in range(file_inputs.count()):
                try:
                    file_input = file_inputs.nth(i)
                    if _should_upload_resume(file_input):
                        print("[Application] Uploading resume...")
                        file_input.set_input_files(RESUME_PATH)
                        time.sleep(2)
                    else:
                        context = _extract_file_field_context(file_input)
                        if "cover letter" in context:
                            print("[Application] Skipping cover letter upload field.")
                except Exception:
                    continue

        total_filled = 0
        pending_required = []
        for fill_pass in range(3):
            pass_filled = 0
            frames = _current_frames(browser)
            for frame in frames:
                pass_filled += _fill_visible_fields(frame, ledger, resume_text, profile, form_values, job_context)

            total_filled += pass_filled

            pending_required = []
            for frame in _current_frames(browser):
                pending_required.extend(_collect_pending_required_fields(frame))
            pending_required = list(dict.fromkeys([item for item in pending_required if item]))

            if pending_required and pass_filled > 0:
                print(f"[Application] New required fields appeared: {', '.join(pending_required[:5])}")
                time.sleep(1)
                continue
            break

        if pending_required:
            signature = "|".join(pending_required[:5])
            if signature == stalled_signature and total_filled == 0:
                stalled_count += 1
            else:
                stalled_signature = signature
                stalled_count = 1

            print(f"[Application] Required fields still pending: {', '.join(pending_required[:5])}")
            if stalled_count >= 2:
                shot_name = f"stuck_required_step_{step+1}.png"
                browser.take_screenshot(shot_name)
                detail = f"Unanswered required fields: {', '.join(pending_required[:5])}"
                if _pause_sse_publish and _pause_event is not None:
                    _pause_event.clear()
                    company = (job_context or {}).get("company", "Unknown Company")
                    role = (job_context or {}).get("role", "Unknown Role")
                    page_url = ""
                    try:
                        page_url = browser.page.url if browser.page else ""
                    except Exception:
                        pass
                    _pause_sse_publish("apply_paused", {
                        "company": company,
                        "role": role,
                        "pending_fields": pending_required[:10],
                        "screenshot_url": f"/screenshots/{shot_name}",
                        "url": page_url,
                        "message": detail,
                    })
                    print("[Application] Paused for human takeover (complete the form in the browser, then click Continue).")
                    resumed = _pause_event.wait(timeout=600)
                    _pause_sse_publish("apply_resumed", {"resumed": bool(resumed), "timeout": not resumed})
                    if not resumed:
                        return _form_error_return(detail)
                    stalled_signature = ""
                    stalled_count = 0
                    time.sleep(1)
                    continue
                return _form_error_return(detail)
            time.sleep(2)
            continue
        else:
            stalled_signature = ""
            stalled_count = 0

        # Navigation
        # Priority: Submit > Review > Next > Continue
        nav_selectors = [
            "button:has-text('Submit')", "button:has-text('Review')", "button:has-text('Next')", "button:has-text('Continue')",
            "button:has-text('Confirm')", "button:has-text('Acknowledge')", "button:has-text('Accept')", "button:has-text('Agree')",
            "input[type='submit']", "input[value*='Submit']", "input[value*='Next']", "input[value*='Confirm']",
            "a[role='button']:has-text('Next')", "div[role='button']:has-text('Next')",
            "button:has-text('Apply Manually')", "a:has-text('Apply Manually')",
            "button:has-text('Apply'):not(:has-text('LinkedIn'))", "a:has-text('Apply Now'):not(:has-text('LinkedIn'))"
        ]
        
        clicked_next = False
        for selector in nav_selectors:
            for frame in _current_frames(browser):
                btn = frame.locator(selector)
                if btn.count() > 0 and btn.first.is_visible() and btn.first.is_enabled():
                    if _is_final_submit_selector(selector) and not _maybe_request_review_before_submit(job_context):
                        return _form_error_return("Application rejected in review gate")
                    print(f"[Application] Clicking '{selector}'...")
                    try:
                        btn.first.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    btn.first.click()
                    clicked_next = True
                    time.sleep(4)
                    break
            if clicked_next: break
        
        if not clicked_next:
            print("[Application] No navigation button found. Taking debug screenshot...")
            browser.take_screenshot(f"stuck_step_{step+1}.png")
            time.sleep(10)

    return _form_error_return("Timed out on form")
