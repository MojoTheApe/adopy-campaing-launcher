"""Thin LLM wrapper used by all agents.

Supports OpenAI and Gemini while keeping agents testable with a mock.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.content_machine.config import LLMConfig

log = logging.getLogger(__name__)


def call_llm(
    config: LLMConfig,
    system: str,
    user: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict[str, str] | None = None,
) -> str:
    """Call the configured LLM and return the assistant message text.

    Raises RuntimeError on API failure so callers can handle retries.
    """
    provider = config.provider.lower().strip()
    if provider == "gemini":
        return _call_gemini(config, system, user, temperature, max_tokens, response_format)
    if provider != "openai":
        raise RuntimeError(f"Unsupported LLM provider: {config.provider}")

    return _call_openai(config, system, user, temperature, max_tokens, response_format)


def _call_openai(
    config: LLMConfig,
    system: str,
    user: str,
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict[str, str] | None,
) -> str:
    try:
        import openai
    except ImportError as exc:
        raise RuntimeError("pip install openai  is required for LLM agents") from exc

    client = openai.OpenAI(api_key=config.api_key)

    kwargs: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature if temperature is not None else config.temperature,
        "max_tokens": max_tokens or config.max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    log.debug("LLM call: model=%s tokens=%s", config.model, kwargs["max_tokens"])

    resp = client.chat.completions.create(**kwargs)
    text = resp.choices[0].message.content or ""
    log.debug("LLM response length: %d chars", len(text))
    return text.strip()


def _call_gemini(
    config: LLMConfig,
    system: str,
    user: str,
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict[str, str] | None,
) -> str:
    """Call Gemini via Google Generative Language REST API.

    Uses requests to avoid adding another dependency. Set:
      CM_LLM_PROVIDER=gemini
      GEMINI_API_KEY=...
      CM_LLM_MODEL=gemini-2.5-flash
    """
    import requests

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.model}:generateContent"
    )
    payload: dict[str, Any] = {
        "systemInstruction": {
            "parts": [{"text": system}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user}],
            }
        ],
        "generationConfig": {
            "temperature": temperature if temperature is not None else config.temperature,
            "maxOutputTokens": max_tokens or config.max_tokens,
        },
    }
    if response_format:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    log.debug("Gemini call: model=%s tokens=%s", config.model, max_tokens or config.max_tokens)
    resp = requests.post(
        url,
        params={"key": config.api_key},
        json=payload,
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text}")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini API response: {data}") from exc

    log.debug("Gemini response length: %d chars", len(text))
    return text.strip()


def call_llm_json(
    config: LLMConfig,
    system: str,
    user: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Call LLM expecting a JSON object response."""
    # Try primary generation
    raw = call_llm(
        config, system, user,
        response_format={"type": "json_object"},
        **kwargs,
    )
    return _load_json_safely(config, system, user, raw, **kwargs)


def _load_json_safely(
    config: LLMConfig,
    system: str,
    user: str,
    raw: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Parse JSON, with a repair pass if the model returns malformed JSON.

    This is more common on Gemini for large structured outputs.
    """
    def _try_parse(s: str) -> dict[str, Any]:
        s = s.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[-1] if s.count("```") >= 2 else s
        return json.loads(s)

    try:
        return _try_parse(raw)
    except Exception as first:  # noqa: BLE001
        log.warning("JSON parse failed, attempting repair: %s", first)

    repair_user = (
        "The following text should be a single valid JSON object but it is malformed.\n"
        "Return ONLY a corrected JSON object, no commentary, no markdown.\n\n"
        f"---BEGIN MALFORMED---\n{raw}\n---END MALFORMED---"
    )
    repair = call_llm(
        config,
        "You are a JSON repair tool. Output valid JSON only.",
        repair_user,
        response_format={"type": "json_object"},
        max_tokens=kwargs.get("max_tokens", min(config.max_tokens, 4096)),
        **{k: v for k, v in kwargs.items() if k not in ("max_tokens",)},
    )
    return _try_parse(repair)
