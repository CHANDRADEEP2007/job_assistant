from __future__ import annotations

import json
import os

import pandas as pd

from core.config import LEDGER_PATH, PROFILE_PATH

# SSE bridge injected by app.py
_sse_publish = None
_ledger_event = None
_ledger_answer_store = None  # dict with key "answer"

LEDGER_SOURCE_USER = "user"
LEDGER_SOURCE_AI = "ai"


def configure_ledger(sse_fn, event, answer_store: dict):
    """Called once by app.py to wire up the SSE bridge."""
    global _sse_publish, _ledger_event, _ledger_answer_store
    _sse_publish = sse_fn
    _ledger_event = event
    _ledger_answer_store = answer_store


class KnowledgeLedger:
    _runtime_pending_questions: set[str] = set()

    def __init__(self):
        self.profile = self._load_profile()
        self.ledger, self.ledger_source = self._load_ledger()
        self.pending_questions: list[str] = []

    @classmethod
    def get_pending_questions(cls) -> list[str]:
        return sorted(cls._runtime_pending_questions)

    def _load_profile(self) -> dict:
        if not os.path.exists(PROFILE_PATH):
            return {}
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_ledger(self) -> tuple[dict, dict]:
        """Load Question/Answer/(Source) from Excel. Missing Source defaults to user."""
        ledger: dict[str, str] = {}
        sources: dict[str, str] = {}
        if not os.path.exists(LEDGER_PATH):
            return ledger, sources
        try:
            df = pd.read_excel(LEDGER_PATH)
            has_source = "Source" in df.columns
            for _, row in df.iterrows():
                if "Question" not in row or "Answer" not in row:
                    continue
                q = str(row["Question"])
                a = str(row["Answer"])
                if not q.strip():
                    continue
                ledger[q] = a
                if has_source and pd.notna(row.get("Source")):
                    src = str(row["Source"]).strip().lower()
                    sources[q] = LEDGER_SOURCE_AI if src == LEDGER_SOURCE_AI else LEDGER_SOURCE_USER
                else:
                    sources[q] = LEDGER_SOURCE_USER
        except Exception as e:
            print(f"[Ledger] Error loading Excel ledger: {e}")
        return ledger, sources

    def _save_ledger(self):
        try:
            questions = list(self.ledger.keys())
            answers = [self.ledger[q] for q in questions]
            sources = [
                self.ledger_source.get(q, LEDGER_SOURCE_USER) for q in questions
            ]
            df = pd.DataFrame({
                "Question": questions,
                "Answer": answers,
                "Source": sources,
            })
            df.to_excel(LEDGER_PATH, index=False)
        except Exception as e:
            print(f"[Ledger] Error saving Excel ledger: {e}")

    def build_context(self, max_chars: int = 3000) -> str:
        snippets = []
        total = 0
        for question, answer in self.ledger.items():
            snippet = f"Q: {question}\nA: {answer}\n"
            if total + len(snippet) > max_chars:
                break
            snippets.append(snippet)
            total += len(snippet)
        return "".join(snippets).strip()

    def get_answer(self, question: str) -> str | None:
        """
        Attempts to get the answer from the profile or the cached ledger using fuzzy matching.
        """
        if question in self.ledger:
            return self.ledger[question]

        q_norm = str(question).lower().rstrip("?:")
        q_tokens = set(q_norm.split())

        best_match = None
        best_score = 0.0

        for key in self.ledger:
            key_norm = str(key).lower().rstrip("?:")
            key_tokens = set(key_norm.split())
            overlap = len(q_tokens & key_tokens)
            score = overlap / max(len(q_tokens), 1)
            if score > 0.7 and score > best_score:
                best_score = score
                best_match = key

        if best_match:
            print(f"[Ledger] Fuzzy match found: '{question}' -> '{best_match}' (score: {best_score:.2f})")
            return self.ledger[best_match]

        for key, value in self.profile.items():
            if key.lower() in q_norm and isinstance(value, str):
                return value

        return None

    def get_source(self, question: str) -> str:
        """Return 'user' or 'ai' for an exact question key; default user."""
        return self.ledger_source.get(question, LEDGER_SOURCE_USER)

    def cache_answer(self, question: str, answer: str, source: str = LEDGER_SOURCE_USER):
        """Cache an answer and persist to Excel. source is 'user' or 'ai'."""
        src = LEDGER_SOURCE_AI if str(source).lower() == LEDGER_SOURCE_AI else LEDGER_SOURCE_USER
        self.ledger[question] = answer
        self.ledger_source[question] = src
        self._save_ledger()
        if question in self.pending_questions:
            self.pending_questions.remove(question)
        self._runtime_pending_questions.discard(question)

    def delete_answer(self, question: str) -> bool:
        """Remove a question from the ledger. Returns True if a row was removed."""
        if question not in self.ledger:
            return False
        del self.ledger[question]
        self.ledger_source.pop(question, None)
        self._save_ledger()
        self._runtime_pending_questions.discard(question)
        if question in self.pending_questions:
            self.pending_questions.remove(question)
        return True

    def update_answer(self, question: str, answer: str) -> bool:
        """Update answer for existing question; marks source as user (user edit / promotion)."""
        if question not in self.ledger:
            return False
        self.ledger[question] = answer
        self.ledger_source[question] = LEDGER_SOURCE_USER
        self._save_ledger()
        return True

    def list_entries(self) -> list[dict]:
        """For API: [{question, answer, source}, ...]"""
        return [
            {
                "question": q,
                "answer": self.ledger[q],
                "source": self.ledger_source.get(q, LEDGER_SOURCE_USER),
            }
            for q in self.ledger
        ]

    def ask_user_and_cache(
        self,
        question: str,
        *,
        resume_text: str = "",
        company: str = "",
        role: str = "",
        job_description: str = "",
        field_type: str = "text",
        options: list[str] | None = None,
        timeout_seconds: int = 120,
        allow_ai_fallback: bool = True,
    ) -> str:
        """
        Ask the user if the UI is available, then fall back quickly to an autonomous answer.
        """
        known_answer = self.get_answer(question)
        if known_answer:
            return known_answer

        print(f"\n[Knowledge Ledger] Unknown question: '{question}'")

        if question not in self.pending_questions:
            self.pending_questions.append(question)
        self._runtime_pending_questions.add(question)

        if _sse_publish and _ledger_event and _ledger_answer_store is not None:
            _ledger_answer_store["answer"] = None
            _ledger_event.clear()

            _sse_publish("ledger_question", {
                "question": question,
                "timeout_seconds": timeout_seconds,
                "company": company,
                "role": role,
            })

            print(f"[Ledger] Waiting up to {timeout_seconds}s for web UI answer...")
            fired = _ledger_event.wait(timeout=timeout_seconds)

            if fired and _ledger_answer_store.get("answer"):
                answer = _ledger_answer_store["answer"]
                print(f"[Ledger] Got answer from web UI: {answer[:60]}")
                self.cache_answer(question, answer, source=LEDGER_SOURCE_USER)
                return answer

            if not allow_ai_fallback:
                print("[Ledger] No UI response. Leaving question pending for user answer.")
                return ""

            print("[Ledger] No UI response, generating answer with Gemini.")
            answer = self._generate_fallback_answer(
                question,
                resume_text=resume_text,
                company=company,
                role=role,
                job_description=job_description,
                field_type=field_type,
                options=options,
            )
            self.cache_answer(question, answer, source=LEDGER_SOURCE_AI)
            return answer

        if os.getenv("WEB_UI_MODE") == "1":
            if not allow_ai_fallback:
                return ""
            answer = self._generate_fallback_answer(
                question,
                resume_text=resume_text,
                company=company,
                role=role,
                job_description=job_description,
                field_type=field_type,
                options=options,
            )
            self.cache_answer(question, answer, source=LEDGER_SOURCE_AI)
            return answer

        answer = input("Please provide the answer (this will be cached): ")
        self.cache_answer(question, answer, source=LEDGER_SOURCE_USER)
        return answer

    def _generate_fallback_answer(
        self,
        question: str,
        *,
        resume_text: str = "",
        company: str = "",
        role: str = "",
        job_description: str = "",
        field_type: str = "text",
        options: list[str] | None = None,
    ) -> str:
        """Use Gemini to generate a reasonable answer from profile, ledger, resume, and job context."""
        normalized_question = question.lower()
        options = [str(option).strip() for option in (options or []) if str(option).strip()]

        if "authorized to work in the united states" in normalized_question:
            known = self.get_answer("Are you legally authorized to work in the United States?")
            if known:
                return "Yes" if "yes" in known.lower() else "No"

        if "require sponsorship" in normalized_question or "visa" in normalized_question:
            known = self.get_answer("Will you now or in the future require sponsorship for employment visa status?")
            if known:
                return "Yes" if "yes" in known.lower() else "No"

        if options and len(options) == 1:
            return options[0]

        try:
            from google import genai
            from core.config import GEMINI_API_KEY

            if not GEMINI_API_KEY:
                return options[0] if options else "Please see my resume for details."

            profile_summary = json.dumps(self.profile, indent=2)
            ledger_context = self.build_context(max_chars=2500) or "(no saved answers yet)"
            options_block = ""
            answer_instruction = "Output only the answer text, nothing else."

            if options:
                options_block = "Available options:\n" + "\n".join(f"- {option}" for option in options[:25]) + "\n"
                answer_instruction = (
                    "Choose the single best option from the list and output exactly that option text, verbatim."
                )
            elif str(field_type).lower() in {"number", "tel"}:
                answer_instruction = "Output only the most appropriate numeric answer."

            prompt = (
                "You are helping a candidate complete a job application.\n"
                f'The form asks: "{question}"\n'
                f"Field type: {field_type or 'text'}\n"
                f"Company: {company or 'Unknown'}\n"
                f"Role: {role or 'Unknown'}\n"
                f"{options_block}"
                f"Candidate profile:\n{profile_summary}\n\n"
                f"Saved knowledge ledger answers:\n{ledger_context}\n\n"
                f"Resume excerpt:\n{resume_text[:5000] or '(not provided)'}\n\n"
                f"Job description excerpt:\n{job_description[:5000] or '(not provided)'}\n\n"
                f"{answer_instruction}"
            )

            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            answer = (response.text or "").strip()
            if answer:
                return answer
        except Exception as e:
            print(f"[Ledger] Gemini fallback error: {e}")

        return options[0] if options else "Please see my resume for details."
