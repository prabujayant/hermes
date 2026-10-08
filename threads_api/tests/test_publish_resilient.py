import pytest

from threads_api.cli import _publish_resilient
from threads_api.client import ThreadsAPIError


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
