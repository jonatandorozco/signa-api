"""Cliente LLM: Google Gemini generateContent API."""

from __future__ import annotations

import os

from google import genai
from google.genai import types

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def get_llm_client() -> genai.Client:
    api_key = _strip_or_none(os.getenv("GEMINI_API_KEY"))
    if not api_key:
        raise ValueError("GEMINI_API_KEY no configurada en .env")
    return genai.Client(api_key=api_key)


def get_model_name(override: str | None = None) -> str:
    if override:
        return override.strip()
    return _strip_or_none(os.getenv("GEMINI_MODEL")) or DEFAULT_GEMINI_MODEL


def _max_output_tokens() -> int:
    raw = _strip_or_none(os.getenv("GEMINI_MAX_OUTPUT_TOKENS"))
    if raw and raw.isdigit():
        return int(raw)
    return 16384


def chat_json_completion(
    client: genai.Client,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
) -> str:
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.2,
        max_output_tokens=_max_output_tokens(),
        response_mime_type="application/json",
    )
    response = client.models.generate_content(
        model=model,
        contents=user_content,
        config=config,
    )
    return response.text or ""
