import pytest

from threads_api.cli import _publish_resilient
from threads_api.client import DuplicatePostError, ThreadsAPIError


def test_refuses_when_hook_already_live(monkeypatch):
    from threads_api.strategist import hook_is_live

    monkeypatch.setattr("time.sleep", lambda s: None)

    class FakeClient:
        def list_posts(self, limit=10):
            return {"data": [{"text": "MARKET CRASH holdings here now"}]}

        def publish_thread(self, thread, reply_wait_seconds=60):
            raise AssertionError("must not publish a duplicate")

    with pytest.raises(DuplicatePostError, match="already live"):
        _publish_resilient(FakeClient(), ["MARKET CRASH holdings here now"])
    assert issubclass(DuplicatePostError, ThreadsAPIError)


def test_hook_is_live_matches_signatures():
    from threads_api.strategist import hook_is_live

    class FakeClient:
        def list_posts(self, limit=10):
            return {"data": [{"text": "AN AGENT told me the race was fixed today"}]}

    assert hook_is_live(FakeClient(), "AN AGENT told me the race was fixed today") is True
    assert hook_is_live(FakeClient(), "Something entirely different here") is False


def test_hook_is_live_fails_open_on_errors():
    from threads_api.strategist import hook_is_live

    class BrokenClient:
        def list_posts(self, limit=10):
            raise ThreadsAPIError("down (code 1)")

    assert hook_is_live(BrokenClient(), "anything at all") is False


def test_retries_same_thread_after_api_error(monkeypatch):
    calls = []
    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    def fake_publish(thread, reply_wait_seconds=60):
        calls.append(list(thread))
        if len(calls) == 1:
            raise ThreadsAPIError("nope (code 24)")
        return ["p1"]

    class FakeClient:
        def publish_thread(self, thread, reply_wait_seconds=60):
            return fake_publish(thread, reply_wait_seconds)

    assert _publish_resilient(FakeClient(), ["a", "b"]) == ["p1"]
    assert len(calls) == 2
    assert calls[0] == calls[1] == ["a", "b"]
    assert slept == [60]


def test_raises_when_retry_also_fails(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    class FakeClient:
        def publish_thread(self, thread, reply_wait_seconds=60):
            raise ThreadsAPIError("still bad (code 24)")

    with pytest.raises(ThreadsAPIError, match="still bad"):
        _publish_resilient(FakeClient(), ["a"])
