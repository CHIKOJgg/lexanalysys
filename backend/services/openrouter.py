"""
openrouter.py — клиент OpenRouter с fallback по моделям

Бесплатные модели (приоритет):
    1. mistralai/mistral-7b-instruct
    2. meta-llama/llama-3-8b-instruct
"""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

FREE_MODELS = [
    "mistralai/mistral-small-3.1-24b-instruct:free"
]

_TIMEOUT = 30  # seconds


def call_openrouter(
    api_key: str,
    system: str,
    user: str,
    model: str | None = None,
) -> str:
    """
    Call OpenRouter, try FREE_MODELS as fallback if model fails.
    Returns raw text response (caller must parse JSON).
    Raises RuntimeError if all models fail.
    """
    models_to_try = [model] if model else []
    # Add free fallbacks (deduplicated)
    for m in FREE_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    last_error: str = "unknown error"

    for current_model in models_to_try:
        try:
            result = _do_request(api_key, current_model, system, user)
            logger.info("OpenRouter OK model=%s chars=%d",
                        current_model, len(result))
            return result
        except Exception as e:
            last_error = str(e)
            logger.warning("OpenRouter failed model=%s: %s", current_model, e)
            continue

    raise RuntimeError(
        f"All OpenRouter models failed. Last error: {last_error}")


def _do_request(api_key: str, model: str, system: str, user: str) -> str:
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://lexanaliz.by",
            "X-Title": "LexAnaliz NPA",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")

    data = json.loads(raw)

    # Extract text
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"No choices in response: {raw[:300]}")

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("Empty content in response")

    return content
