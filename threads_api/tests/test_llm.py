import pytest

from threads_api import llm
from threads_api.llm import LLMError, chat


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


def _ok(monkeypatch, content="hello", seen=None):
    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update({"url": url, "model": (json or {}).get("model")})
        auth = (headers or {}).get("Authorization", "")
        assert auth.startswith("Bearer "), "must send bearer key"
        return _Resp(200, {"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)


def test_chat_returns_text_with_default_router(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACKS", raising=False)
    seen = {}
    _ok(monkeypatch, "hi", seen)
    assert chat([{"role": "user", "content": "x"}]) == "hi"
    assert seen["model"] == "openrouter/free"
    assert seen["url"].endswith("/chat/completions")


def test_missing_key_raises_before_network(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def fail(url, headers=None, json=None, timeout=None):
        raise AssertionError("no request should be sent")

    monkeypatch.setattr(llm.requests, "post", fail)
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        chat([{"role": "user", "content": "x"}])


def test_reasoning_fallback_when_content_null(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(
        llm.requests, "post",
        lambda url, headers=None, json=None, timeout=None: _Resp(
            200, {"choices": [{"message": {
                "content": None,
                "reasoning_content": "fallback text",
            }}]},
        ),
    )
    assert chat([{"role": "user", "content": "x"}]) == "fallback text"


def test_falls_back_to_next_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "first-model")
    monkeypatch.setenv("OPENROUTER_FALLBACKS", "second-model")
    tried = []

    def fake_post(url, headers=None, json=None, timeout=None):
        tried.append(json["model"])
        if json["model"] == "first-model":
            return _Resp(500, {}, "boom")
        return _Resp(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert chat([{"role": "user", "content": "x"}]) == "ok"
    assert tried == ["first-model", "second-model"]


def test_rate_limit_retries_then_moves_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_FALLBACKS", raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _Resp(429, {}, "rate limited")

    monkeypatch.setattr(llm.requests, "post", fake_post)
    with pytest.raises(LLMError, match="rate limited"):
        chat([{"role": "user", "content": "x"}, ], retries=1)
    assert len(calls) == 2  # initial + 1 retry


def test_401_and_402_raise_immediately(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for code, match in ((401, "API key"), (402, "credits")):
        monkeypatch.setattr(
            llm.requests, "post",
            lambda url, headers=None, json=None, timeout=None, c=code: _Resp(c, {}, "e"),
        )
        with pytest.raises(LLMError, match=match):
            chat([{"role": "user", "content": "x"}])
