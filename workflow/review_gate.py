import os
import threading

# Shared state injected by app.py at startup.
_sse_publish = None
_review_event = None
_review_decision = None


def configure_review_gate(sse_fn, event: threading.Event, decision: dict):
    """Called once by app.py to wire up the SSE bridge."""
    global _sse_publish, _review_event, _review_decision
    _sse_publish = sse_fn
    _review_event = event
    _review_decision = decision


def request_final_review(company: str, role: str, form_data: dict, match_score: int) -> bool:
    """
    Review gate for low-confidence or sensitive cases.
    User must approve within the window; otherwise the application is skipped.
    """
    review_path = "FINAL_REVIEW.md"
    timeout_seconds = 300

    markdown_content = f"# Final Review: {company} - {role}\n\n"
    markdown_content += f"**Match Score:** {match_score}%\n\n"
    markdown_content += "## Form Data to be Submitted\n"
    for key, value in form_data.items():
        markdown_content += f"- **{key}**: {value}\n"

    with open(review_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)

    if _sse_publish and _review_event and _review_decision is not None:
        _review_decision["approved"] = None
        _review_event.clear()

        _sse_publish("review_request", {
            "company": company,
            "role": role,
            "match_score": match_score,
            "form_data": form_data,
            "timeout_seconds": timeout_seconds,
        })

        print(f"[Review Gate] {timeout_seconds}s window - waiting for explicit approval.")
        fired = _review_event.wait(timeout=timeout_seconds)

        if not fired:
            print(f"[Review Gate] No action in {timeout_seconds}s - skipping application.")
            _sse_publish("review_expired", {
                "company": company,
                "role": role,
                "auto_approved": False,
            })
            return False

        approved = bool(_review_decision.get("approved", False))
        if approved:
            print("[Review Gate] Application approved via web UI.")
        else:
            print("[Review Gate] Application rejected by user.")
        return approved

    if os.getenv("WEB_UI_MODE") == "1":
        print("[Review Gate] WEB_UI_MODE fallback - skipping because explicit approval is required.")
        return False

    print(f"\n[Review Gate] Generated {review_path} for your review.")
    while True:
        command = input("Type 'Submit' to finalize the application, or 'Cancel' to abort: ").strip()
        if command.lower() == "submit":
            print("[Review Gate] Application approved by user.")
            return True
        if command.lower() == "cancel":
            print("[Review Gate] Application aborted by user.")
            return False
        print("Invalid command. Please type 'Submit' or 'Cancel'.")
