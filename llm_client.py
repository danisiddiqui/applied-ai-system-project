"""Thin wrapper around Gemini so the rest of the system never talks to the API directly.

If GEMINI_API_KEY is missing (or the API call fails), callers get a clear error and
fall back to offline mode instead of crashing.
"""

import logging
import os

log = logging.getLogger("musicrecs.llm")

GEMINI_MODEL_NAME = "gemini-flash-lite-latest"


class LLMUnavailable(RuntimeError):
    """Raised when no LLM can be used (missing key, missing library, or API error)."""


class GeminiClient:
    """Sends a prompt to Gemini and returns plain text."""

    name = "gemini"

    def __init__(self, model: str = GEMINI_MODEL_NAME):
        """Create the client from GEMINI_API_KEY (loaded from the environment or a .env file)."""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set")
        try:
            from google import genai
        except ImportError as exc:
            raise LLMUnavailable("google-genai is not installed") from exc
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def generate(self, prompt: str, temperature: float = 0.2) -> str:
        """Return the model's text response, or raise LLMUnavailable on any API error."""
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={"temperature": temperature},
            )
            return (response.text or "").strip()
        except Exception as exc:  # network errors, quota, bad key, etc.
            log.error("Gemini call failed: %s: %s", type(exc).__name__, exc)
            raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc


def get_llm():
    """Return a GeminiClient if possible, otherwise None (the system then runs in offline mode)."""
    try:
        client = GeminiClient()
        log.info("LLM mode: Gemini (%s)", client.model)
        return client
    except LLMUnavailable as exc:
        log.warning("LLM unavailable (%s); running in offline mode", exc)
        return None
