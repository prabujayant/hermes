import pytest

from threads_api.client import ThreadsAPIError, ThreadsClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ThreadsClient, "_auth_fields", lambda self: {})
    monkeypatch.setattr("threads_api.client.Config.validate", classmethod(lambda cls: None))
    monkeypatch.setattr("threads_api.client.Config.APP_SECRET", "secret")
    monkeypatch.setattr("threads_api.client.Config.ACCESS_TOKEN", "token")
    monkeypatch.setattr("threads_api.client.Config.THREADS_USER_ID", "uid")
    return ThreadsClient()


def _response(payload, status_code=200):
    class FakeResponse:
        def __init__(self, payload, status_code):
            self._payload = payload
            self.status_code = status_code
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError("raise_for_status should not be reached")

    return FakeResponse(payload, status_code)


class TestContainers:
    def test_create_text_container(self, client, monkeypatch):
        sent = {}

        def fake_post(url, data):
            sent["url"] = url
            sent["data"] = data
            return _response({"id": "123"})

        monkeypatch.setattr(client.session, "post", fake_post)
        result = client.create_text_container("Hello")

        assert result["id"] == "123"
        assert sent["data"]["media_type"] == "TEXT"
        assert sent["data"]["text"] == "Hello"
        assert sent["url"].endswith("/uid/threads")

    def test_rejects_text_over_500_chars(self, client):
        with pytest.raises(ValueError, match="Threads allows 500"):
            client.create_text_container("x" * 501)

    def test_carousel_rejects_more_than_20_items(self, client):
        urls = [f"https://example.com/{i}.jpg" for i in range(21)]
        with pytest.raises(ValueError, match="between 2 and 20"):
            client.create_carousel_container("test", urls)

    def test_carousel_rejects_single_item(self, client):
        with pytest.raises(ValueError, match="between 2 and 20"):
            client.create_carousel_container("test", ["https://example.com/1.jpg"])


class TestErrorHandling:
    def test_surfaces_api_error_message(self, client, monkeypatch):
        monkeypatch.setattr(
            client.session,
            "post",
            lambda url, data: _response(
                {"error": {"message": "Invalid OAuth access token", "code": 190}}
            ),
        )
        with pytest.raises(ThreadsAPIError, match="Invalid OAuth access token"):
            client.create_text_container("hi")

    def test_handles_non_json_response(self, client, monkeypatch):
        class BadResponse:
            status_code = 500
            text = "<html>gateway</html>"

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(client.session, "post", lambda url, data: BadResponse())
        with pytest.raises(ThreadsAPIError, match="non-JSON"):
            client.create_text_container("hi")

    def test_handles_http_error_without_error_body(self, client, monkeypatch):
        monkeypatch.setattr(
            client.session, "post", lambda url, data: _response({}, status_code=503)
        )
        with pytest.raises(ThreadsAPIError, match="HTTP 503"):
            client.create_text_container("hi")


class TestPublishing:
    def test_publish_text_creates_then_publishes(self, client, monkeypatch):
        calls = []

        def fake_post(url, data):
            calls.append(url)
            if url.endswith("threads_publish"):
                return _response({"id": "published_1"})
            return _response({"id": "container_1"})

        monkeypatch.setattr(client.session, "post", fake_post)
        monkeypatch.setattr(
            client.session,
            "get",
            lambda url, params: _response(
                {"id": "container_1", "status": "FINISHED"}
            ),
        )
        result = client.publish_text("Hello world")

        assert result["id"] == "published_1"
        assert len(calls) == 2
        assert calls[0].endswith("/threads")

    def test_publish_waits_for_container_ready(self, client, monkeypatch):
        statuses = ["IN_PROGRESS", "FINISHED"]
        monkeypatch.setattr(
            client.session,
            "get",
            lambda url, params: _response({"status": statuses.pop(0)}),
        )
        monkeypatch.setattr(
            client.session, "post", lambda url, data: _response({"id": "c1"})
        )
        monkeypatch.setattr("threads_api.client.time.sleep", lambda s: None)
        assert client.wait_for_container("c1", timeout=60, interval=5) == "FINISHED"

    def test_publish_fails_on_container_error(self, client, monkeypatch):
        monkeypatch.setattr(
            client.session,
            "get",
            lambda url, params: _response({"status": "ERROR"}),
        )
        with pytest.raises(ThreadsAPIError, match="failed processing"):
            client.wait_for_container("c1", timeout=60, interval=5)

    def test_media_publish_waits_before_publishing(self, client, monkeypatch):
        monkeypatch.setattr(
            client.session,
            "get",
            lambda url, params: _response({"status": "FINISHED"}),
        )
        monkeypatch.setattr(
            client.session, "post", lambda url, data: _response({"id": "c1"})
        )
        client.create_image_container = lambda text, url: {"id": "c1"}
        client.publish_image("text", "https://example.com/a.jpg", wait_seconds=30)

    def test_thread_rollback_deletes_prefix_on_failure(self, client, monkeypatch):
        calls = {"publishes": 0}
        deleted = []

        def fake_post(url, data):
            if url.endswith("threads_publish"):
                calls["publishes"] += 1
                if calls["publishes"] == 1:
                    return _response({"id": "hook_live_id"})
                return _response(
                    {"error": {"message": "gone", "code": 24, "type": ""}},
                    status_code=400,
                )
            return _response({"id": "container_x"})

        monkeypatch.setattr(client.session, "post", fake_post)
        monkeypatch.setattr(
            client.session,
            "get",
            lambda url, params: _response({"status": "FINISHED"}),
        )
        monkeypatch.setattr(
            client.session,
            "delete",
            lambda url, params: deleted.append(url) or _response({"ok": True}),
        )
        with pytest.raises(ThreadsAPIError):
            client.publish_thread(["hook", "reply"])

        assert any("hook_live_id" in url for url in deleted)

    def test_thread_rollback_survives_delete_errors(self, client, monkeypatch):
        calls = {"publishes": 0}

        def fake_post(url, data):
            if url.endswith("threads_publish"):
                calls["publishes"] += 1
                if calls["publishes"] == 1:
                    return _response({"id": "hook_live_id"})
                raise ThreadsAPIError("boom (code 24)")
            return _response({"id": "container_x"})

        monkeypatch.setattr(client.session, "post", fake_post)
        monkeypatch.setattr(
            client.session,
            "get",
            lambda url, params: _response({"status": "FINISHED"}),
        )
        monkeypatch.setattr(
            client.session,
            "delete",
            lambda url, params: (_ for _ in ()).throw(
                ThreadsAPIError("delete down (code 1)")),
        )
        with pytest.raises(ThreadsAPIError, match="boom"):
            client.publish_thread(["hook", "reply"])


class TestProfile:
    def test_get_profile_uses_me_endpoint(self, client, monkeypatch):
        captured = {}

        def fake_get(url, params):
            captured["url"] = url
            captured["params"] = params
            return _response({"id": "1409131831350692", "username": "me"})

        monkeypatch.setattr(client.session, "get", fake_get)
        profile = client.get_profile()

        assert profile["username"] == "me"
        assert captured["url"].endswith("/me")
        assert "username" in captured["params"]["fields"]


class TestKeywordSearch:
    def test_sends_scope_params(self, client, monkeypatch):
        seen = {}

        def fake_get(url, params):
            seen["url"] = url
            seen.update(params)
            return _response({"data": []})

        monkeypatch.setattr(client.session, "get", fake_get)
        client.keyword_search("backend", author_username="@primeagen",
                             limit=5)

        assert seen["url"].endswith("/keyword_search")
        assert seen["q"] == "backend"
        assert seen["search_type"] == "TOP"
        assert seen["author_username"] == "primeagen"
        assert seen["media_type"] == "TEXT"
        assert seen["limit"] == 5
