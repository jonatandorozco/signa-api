"""Cliente LLM: Azure OpenAI Responses API o OpenAI estándar."""

from __future__ import annotations

import os
from typing import Literal

import app.core.env  # noqa: F401 — asegura .env si se importa sin main
from openai import AzureOpenAI, OpenAI

Provider = Literal["azure", "openai"]


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def get_llm_provider() -> Provider:
    if _strip_or_none(os.getenv("AZURE_OPENAI_ENDPOINT")):
        return "azure"
    return "openai"


def get_llm_client() -> OpenAI | AzureOpenAI:
    azure_endpoint = _strip_or_none(os.getenv("AZURE_OPENAI_ENDPOINT"))
    if azure_endpoint:
        api_key = _strip_or_none(os.getenv("AZURE_OPENAI_API_KEY")) or _strip_or_none(
            os.getenv("OPENAI_API_KEY")
        )
        if not api_key:
            raise ValueError(
                "Configura AZURE_OPENAI_API_KEY (o OPENAI_API_KEY) en .env para Azure OpenAI."
            )
        # Responses API en este recurso usa 2025-04-01-preview (chat/completions no está soportado)
        api_version = (
            _strip_or_none(os.getenv("AZURE_OPENAI_API_VERSION")) or "2025-04-01-preview"
        )
        endpoint = azure_endpoint.rstrip("/") + "/"
        return AzureOpenAI(
            api_version=api_version,
            azure_endpoint=endpoint,
            api_key=api_key,
        )

    api_key = _strip_or_none(os.getenv("OPENAI_API_KEY"))
    if not api_key:
        raise ValueError(
            "Configura AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY "
            "o OPENAI_API_KEY en .env"
        )
    return OpenAI(api_key=api_key)


def get_deployment_name(model: str | None = None) -> str:
    if model:
        return model.strip()
    azure_dep = _strip_or_none(os.getenv("AZURE_OPENAI_DEPLOYMENT"))
    if azure_dep:
        return azure_dep
    return _strip_or_none(os.getenv("OPENAI_MODEL")) or "gpt-4o-mini"


def _max_output_tokens() -> int:
    raw = _strip_or_none(os.getenv("AZURE_OPENAI_MAX_COMPLETION_TOKENS"))
    if raw and raw.isdigit():
        return int(raw)
    return 16384


def _extract_responses_text(response: object) -> str:
    """Extrae texto de client.responses.create (Azure gpt-5.x)."""
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            text = getattr(part, "text", None)
            if text:
                chunks.append(text)
    return "".join(chunks)


def _azure_responses_json(
    client: AzureOpenAI,
    *,
    deployment: str,
    system_prompt: str,
    user_content: str,
) -> str:
    input_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    try:
        response = client.responses.create(
            model=deployment,
            input=input_messages,
            max_output_tokens=_max_output_tokens(),
            text={"format": {"type": "json_object"}},
        )
    except Exception:
        response = client.responses.create(
            model=deployment,
            input=input_messages,
            max_output_tokens=_max_output_tokens(),
        )
    return _extract_responses_text(response)


def _openai_chat_json(
    client: OpenAI,
    *,
    deployment: str,
    system_prompt: str,
    user_content: str,
) -> str:
    completion = client.chat.completions.create(
        model=deployment,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return completion.choices[0].message.content or ""


def chat_json_completion(
    client: OpenAI | AzureOpenAI,
    *,
    deployment: str,
    system_prompt: str,
    user_content: str,
) -> str:
    if isinstance(client, AzureOpenAI):
        return _azure_responses_json(
            client,
            deployment=deployment,
            system_prompt=system_prompt,
            user_content=user_content,
        )
    return _openai_chat_json(
        client,
        deployment=deployment,
        system_prompt=system_prompt,
        user_content=user_content,
    )
