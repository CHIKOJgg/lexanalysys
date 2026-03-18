# openrouter.py -- OpenRouter client with free-router + fallback + retry on 429
#
# Primary:  openrouter/free  -- auto-selects any available free model
# Fallback: specific :free models in case primary is throttled
#
# Rate limits (no credits): 50 req/day
# Rate limits (with credits >= $10): 1000 req/day
# Retry on 429: exponential backoff  3->6->12 s

import json
import logging
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# openrouter/free is tried FIRST -- it picks any available free model automatically
# Specific models are fallback in case the router itself rate-limits
FREE_MODELS = [
    "openrouter/free"                   # auto-router (recommended
]

_TIMEOUT = 60           # seconds per request
_MAX_RETRIES = 3        # retries per model on 429/5xx
_RETRY_DELAYS = [3, 6, 12]   # fixed backoff steps (seconds)


def call_openrouter(
    api_key: str,
    system: str,
    user: str,
    model: str | None = None,
) -> tuple[str, str]:
    """
    Call OpenRouter. Returns (response_text, model_actually_used).
    Tries FREE_MODELS as fallback. Raises RuntimeError if all fail.
    """
    models_to_try: list[str] = []
    # User-specified model goes first if not already in list
    if model and model not in FREE_MODELS:
        models_to_try.append(model)
    for m in FREE_MODELS:
        if m not in models_to_try:
            models_to_try.append(m)

    last_error = "no attempt"

    for current_model in models_to_try:
        try:
            content, used_model = _with_retry(api_key, current_model, system, user)
            logger.info("OK model=%s used=%s len=%d", current_model, used_model, len(content))
            return content, used_model
        except Exception as exc:
            last_error = str(exc)
            logger.warning("FAIL model=%s: %s", current_model, exc)
            continue

    raise RuntimeError(f"All OpenRouter models failed. Last error: {last_error}")


def _with_retry(api_key: str, model: str, system: str, user: str) -> tuple[str, str]:
    """Single model with retry on 429 / 5xx."""
    last_exc: Exception = RuntimeError("no attempt")

    for attempt, delay in enumerate(_RETRY_DELAYS[:_MAX_RETRIES], start=1):
        try:
            return _do_request(api_key, model, system, user)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                # Respect Retry-After header if present
                retry_after = None
                try:
                    retry_after = float(exc.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    pass
                wait = retry_after if retry_after else delay
                logger.warning(
                    "429 model=%s attempt=%d/%d wait=%.0fs",
                    model, attempt, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
            elif exc.code >= 500:
                logger.warning("5xx code=%d model=%s attempt=%d", exc.code, model, attempt)
                time.sleep(delay)
            else:
                raise  # 4xx other than 429 -- don't retry
        except Exception as exc:
            last_exc = exc
            logger.warning("error model=%s attempt=%d: %s", model, attempt, exc)
            time.sleep(delay)

    raise last_exc


def _do_request(api_key: str, model: str, system: str, user: str) -> tuple[str, str]:
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://lexanaliz.by",
            "X-Title":       "LexAnaliz NPA",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")

    data = json.loads(raw)

    # OpenRouter sometimes returns 200 with error body
    if "error" in data:
        err  = data["error"]
        code = err.get("code", 0)
        msg  = err.get("message", str(err))
        if code == 429:
            raise urllib.error.HTTPError(OPENROUTER_URL, 429, msg, {}, None)
        raise RuntimeError(f"API error {code}: {msg}")

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"No choices in response: {raw[:200]}")

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("Empty content in response")

    # model field in response shows which free model was actually used
    used_model = data.get("model", model)
    return content, used_model