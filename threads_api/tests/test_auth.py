import socket
import threading
import time

import pytest
import requests

from threads_api import auth


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def base_url(monkeypatch):
    port = _free_port()
    uri = f"http://127.0.0.1:{port}/callback"
    monkeypatch.setattr("threads_api.auth.Config.OAUTH_REDIRECT_URI", uri)
    return uri


def _run_capture(state="expected-state"):
    """Start the callback server in a thread and return (thread, result)."""
    result = {}

    def run():
        try:
            result["code"] = auth._capture_redirect_code(state)
        except Exception as exc:  # noqa: BLE001 - surfaced to the test
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _get_with_retry(url, timeout=10, **kwargs):
    """The server may not be listening yet, so retry refused connections."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return requests.get(url, timeout=1, **kwargs)
        except requests.ConnectionError as exc:
            last = exc
            time.sleep(0.05)
    raise AssertionError(f"callback server never came up: {last}")


class TestAuthorizationUrl:
    def test_includes_required_parameters(self, monkeypatch):
        monkeypatch.setattr("threads_api.auth.Config.validate_app", classmethod(lambda cls: None))
        monkeypatch.setattr("threads_api.auth.Config.APP_ID", "123")
        monkeypatch.setattr(
            "threads_api.auth.Config.OAUTH_REDIRECT_URI", "http://localhost:8080/callback"
        )
        url = auth.get_authorization_url("xyz")

        assert url.startswith("https://threads.com/oauth/authorize?")
        assert "client_id=123" in url
        assert "response_type=code" in url
        assert "state=xyz" in url
        assert "threads_basic" in url
        assert "threads_content_publish" in url


class TestRedirectCapture:
    def test_captures_authorization_code(self, base_url):
        thread, result = _run_capture()
        response = _get_with_retry(f"{base_url}?code=THE_CODE&state=expected-state")
        thread.join(timeout=5)

        assert response.status_code == 200
        assert result["code"] == "THE_CODE"

    def test_strips_trailing_hash_underscore(self, base_url):
        thread, result = _run_capture()
        # Meta appends "#_" to the redirect; simulate it inside the code parameter.
        _get_with_retry(f"{base_url}?code=THE_CODE%23_&state=expected-state")
        thread.join(timeout=5)

        assert result["code"] == "THE_CODE"

    def test_rejects_state_mismatch(self, base_url):
        thread, result = _run_capture()
        response = _get_with_retry(f"{base_url}?code=THE_CODE&state=wrong")
        thread.join(timeout=5)

        assert response.status_code == 400
        assert "State mismatch" in str(result["error"])

    def test_surfaces_denied_authorization(self, base_url):
        thread, result = _run_capture()
        response = _get_with_retry(
            f"{base_url}?error=access_denied&error_description=User+denied"
        )
        thread.join(timeout=5)

        assert response.status_code == 400
        assert "User denied" in str(result["error"])

    def test_ignores_unrelated_paths(self, base_url):
        thread, result = _run_capture()
        host = base_url.rsplit("/callback", 1)[0]
        response = _get_with_retry(f"{host}/favicon.ico?code=SNEAKY&state=expected-state")
        assert response.status_code == 404
        # The server keeps waiting, so a real redirect still succeeds.
        response = _get_with_retry(f"{base_url}?code=REAL&state=expected-state")
        thread.join(timeout=5)

        assert response.status_code == 200
        assert result["code"] == "REAL"


class TestRedirectTarget:
    def test_parses_https_localhost(self, monkeypatch):
        monkeypatch.setattr(
            auth.Config, "OAUTH_REDIRECT_URI", "https://localhost:8080/callback"
        )
        assert auth._redirect_target() == ("https", "localhost", 8080, "/callback")

    def test_defaults_to_the_https_port(self, monkeypatch):
        monkeypatch.setattr(
            auth.Config, "OAUTH_REDIRECT_URI", "https://example.com/cb"
        )
        assert auth._redirect_target() == ("https", "example.com", 443, "/cb")


class TestListenTarget:
    def test_local_https_terminates_tls_itself(self):
        assert auth._listen_target("https", "localhost", 8080) == (
            "localhost",
            8080,
            True,
        )

    def test_local_http_does_not_use_tls(self):
        assert auth._listen_target("http", "127.0.0.1", 8080) == (
            "127.0.0.1",
            8080,
            False,
        )

    def test_remote_https_binds_locally_without_tls(self):
        # An https tunnel terminates TLS upstream and forwards plain HTTP.
        assert auth._listen_target("https", "abc.trycloudflare.com", 443) == (
            "localhost",
            443,
            False,
        )


class TestTlsCertificate:
    def test_generates_then_reuses_the_certificate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth.Config, "ROOT", tmp_path)

        cert_path, key_path = auth._ensure_self_signed_cert()
        assert cert_path.exists()
        assert key_path.exists()
        assert auth._tls_context() is not None

        first = cert_path.read_bytes()
        again, _ = auth._ensure_self_signed_cert()
        assert again.read_bytes() == first


class TestHttpsRedirect:
    def test_serves_tls_and_captures_the_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth.Config, "ROOT", tmp_path)
        port = _free_port()
        uri = f"https://localhost:{port}/callback"
        monkeypatch.setattr(auth.Config, "OAUTH_REDIRECT_URI", uri)

        thread, result = _run_capture()
        response = _get_with_retry(
            f"{uri}?code=TLS_CODE&state=expected-state", verify=False
        )
        thread.join(timeout=10)

        assert response.status_code == 200
        assert result["code"] == "TLS_CODE"
