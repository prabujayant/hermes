"""OpenRouter client for analysis + thread writing (free-tier friendly).

Defaults to the ``openrouter/free`` router so requests resolve to a
working free model without hardcoding slugs that rot. Override with
OPENROUTER_MODEL (e.g. a specific ``:free`` variant) and
OPENROUTER_FALLBACKS (comma-separated).

Free-tier limits (no credits purchased): 20 req/min, 50 req/day.
Each CLI command below uses 1-3 requests — on-demand use only,
never inside the hourly loop.
"""

import json
import os
import time

import requests

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/free"
TIMEOUT = 90


class LLMError(Exception):
    """Raised for transport, auth, rate-limit, or bad-payload failures."""


def _models():
    primary = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    fallbacks = [
        m.strip()
        for m in os.getenv("OPENROUTER_FALLBACKS", "").split(",")
        if m.strip()
    ]
    seen = []
    for model in [primary, *fallbacks]:
        if model not in seen:
            seen.append(model)
    return seen


def _key():
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise LLMError(
            "OPENROUTER_API_KEY is not set. Add it to .env "
            "(never commit it) and, for Actions, as a repo secret."
        )
    return key


def chat(messages, max_tokens=1500, temperature=0.7, retries=2):
    """Send chat messages, trying each model in order. Returns text."""
    key = _key()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    last_error = "no models configured"
    for model in _models():
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        for attempt in range(retries + 1):
            try:
                response = requests.post(API_URL, headers=headers,
                                         json=body, timeout=TIMEOUT)
            except requests.RequestException as exc:
                last_error = f"{model}: network error ({exc})"
                break  # next model, network is unlikely to heal by retry
            if response.status_code == 429:
                wait = 20 * (attempt + 1)
                if attempt < retries:
                    time.sleep(wait)
                    continue
                last_error = f"{model}: rate limited (429, free tier caps)"
                break
            if response.status_code == 401:
                raise LLMError(
                    "OpenRouter rejected the API key (401). "
                    "Check OPENROUTER_API_KEY / repo secret."
                )
            if response.status_code == 402:
                raise LLMError(
                    "OpenRouter needs credits for this request (402). "
                    "Use a :free model or the openrouter/free router."
                )
            if response.status_code >= 400:
                last_error = (
                    f"{model}: HTTP {response.status_code} "
                    f"{response.text[:200]}"
                )
                break  # model-level error, try next model
            try:
                payload = response.json()
                message = payload["choices"][0]["message"]
                text = (
                    message.get("content")
                    or message.get("reasoning_content")
                    or message.get("reasoning")
                )
                if not text or not str(text).strip():
                    last_error = f"{model}: empty content"
                    break
                return text
            except (ValueError, KeyError, IndexError) as exc:
                last_error = f"{model}: bad response ({exc})"
                break
    raise LLMError(f"All OpenRouter models failed. Last error: {last_error}")
