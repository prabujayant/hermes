"""Threads OAuth 2.0 authorization-code flow.

Endpoints follow https://developers.facebook.com/docs/threads/get-started/get-access-tokens-and-permissions
"""

import http.server
import os
import secrets
import ssl
import threading
import time
import urllib.parse
import webbrowser

import requests

from .config import Config

AUTHORIZE_URL = "https://threads.com/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
LONG_LIVED_TOKEN_URL = "https://graph.threads.net/access_token"
GRAPH_URL = "https://graph.threads.net/v1.0"

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

SCOPES = "threads_basic,threads_content_publish,threads_manage_insights,threads_manage_replies,threads_read_replies,threads_delete,threads_keyword_search"
REDIRECT_TIMEOUT_SECONDS = 300


class _CallbackServer(http.server.ThreadingHTTPServer):
    """Listener for the OAuth redirect.

    Windows honours SO_REUSEADDR by allowing several processes to bind the same
    port and then stealing connections between them, which surfaces as
    baffling "Invalid state" failures when a stale listener is still running.
    So keep it off there; a second bind should fail loudly instead.
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def get_authorization_url(state):
    Config.validate_app()
    query = urllib.parse.urlencode(
        {
            "client_id": Config.APP_ID,
            "redirect_uri": Config.OAUTH_REDIRECT_URI,
            "scope": SCOPES,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _unwrap(response):
    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError(
            f"Threads returned a non-JSON response (HTTP {response.status_code})"
        )

    if "access_token" in payload:
        return payload

    if "error" in payload:
        message = payload["error"].get("message", "Unknown error")
    else:
        message = payload.get("error_message") or response.text[:300]
    raise RuntimeError(f"Threads authentication failed: {message}")


def exchange_code_for_token(code):
    """Trade the authorization code for a short-lived Threads user token."""
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": Config.APP_ID,
            "client_secret": Config.APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": Config.OAUTH_REDIRECT_URI,
            "code": code,
        },
        timeout=30,
    )
    return _unwrap(response)


def exchange_for_long_lived_token(short_lived_token):
    """Exchange a short-lived token (~1 hour) for a long-lived one (~60 days)."""
    response = requests.get(
        LONG_LIVED_TOKEN_URL,
        params={
            "grant_type": "th_exchange_token",
            "client_secret": Config.APP_SECRET,
            "access_token": short_lived_token,
        },
        timeout=30,
    )
    return _unwrap(response)["access_token"]


def fetch_profile(access_token):
    response = requests.get(
        f"{GRAPH_URL}/me",
        params={
            "fields": "id,username,name,threads_profile_picture_url",
            "access_token": access_token,
        },
        timeout=30,
    )
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(
            f"Could not read profile: {payload['error'].get('message')}"
        )
    return payload


def start_oauth_flow(open_browser=True):
    """Run the full flow and return the resulting credentials."""
    state = secrets.token_urlsafe(16)
    url = get_authorization_url(state)

    print("Open this URL and approve access:\n")
    print(f"  {url}\n")
    if open_browser:
        webbrowser.open(url)

    code = _capture_redirect_code(state)
    print("Authorization code received; exchanging for a token...")

    short_lived = exchange_code_for_token(code)
    user_id = short_lived.get("user_id")

    token = short_lived["access_token"]
    try:
        token = exchange_for_long_lived_token(token)
    except RuntimeError as exc:
        print(f"  (long-lived exchange skipped: {exc})")

    profile = fetch_profile(token)
    return {
        "access_token": token,
        "user_id": profile.get("id") or user_id,
        "username": profile.get("username"),
    }


def save_credentials(access_token, user_id):
    """Persist the token into .env, preserving unrelated lines."""
    path = Config.ENV_PATH
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    updates = {"ACCESS_TOKEN": access_token, "THREADS_USER_ID": user_id}
    seen = set()
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                result.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        result.append(line)

    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")

    path.write_text("\n".join(result) + "\n", encoding="utf-8")
    return path


def _redirect_target():
    """Split the configured redirect URI into (scheme, host, port, path)."""
    parsed = urllib.parse.urlparse(Config.OAUTH_REDIRECT_URI)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port, parsed.path or "/"


def _listen_target(scheme, host, port):
    """Where to bind locally, and whether we must terminate TLS ourselves.

    Meta rejects http:// redirects ("Insecure login blocked"), so a local https
    redirect is served with a self-signed certificate. A remote redirect (for
    example an https tunnel) already terminates TLS upstream, so we listen for
    plain HTTP on the same port behind it.
    """
    if host in LOCAL_HOSTS:
        return host, port, scheme == "https"
    return "localhost", port, False


def _ensure_self_signed_cert():
    """Create a localhost certificate for the https callback, once."""
    cert_path = Config.ROOT / ".certs" / "callback-cert.pem"
    key_path = Config.ROOT / ".certs" / "callback-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    try:
        import datetime
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError(
            "An https redirect URI needs a local TLS certificate, which requires "
            "the 'cryptography' package (pip install cryptography). Alternatively "
            "point OAUTH_REDIRECT_URI at an https tunnel you run yourself."
        ) from exc

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _tls_context():
    cert_path, key_path = _ensure_self_signed_cert()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def _capture_redirect_code(expected_state):
    scheme, host, port, callback_path = _redirect_target()
    bind_host, listen_port, use_tls = _listen_target(scheme, host, port)
    captured = {}

    path = callback_path.rstrip("/") or "/"
    accepted_paths = {path, path + "/"}

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in accepted_paths:
                # Browsers request /favicon.ico and similar; ignore them.
                self.send_error(404)
                return

            params = urllib.parse.parse_qs(parsed.query)
            if params.get("error"):
                captured["error"] = params.get("error_description", params["error"])[0]
                self._respond(400, "Authorization denied")
                return

            if params.get("state", [None])[0] != expected_state:
                captured["error"] = (
                    "State mismatch: the authorization came from a different (older) "
                    "login attempt than the one currently listening, or the request "
                    "did not originate from this flow. Re-run auth and use the newest "
                    "authorization URL."
                )
                self._respond(400, "Invalid state - use the newest authorization link")
                return

            code = params.get("code", [None])[0]
            if not code:
                captured["error"] = "No authorization code in the redirect"
                self._respond(400, "Missing code")
                return

            captured["code"] = code.rstrip("#_")
            self._respond(200, "Authorization successful - you can close this tab")

        def _respond(self, status, message):
            body = f"<html><body><h1>{message}</h1></body></html>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        server = _CallbackServer((bind_host, listen_port), RedirectHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Could not listen on {bind_host}:{listen_port} for the OAuth callback "
            f"({exc}). Another login attempt may still be running - close it first."
        ) from exc

    if use_tls:
        server.socket = _tls_context().wrap_socket(server.socket, server_side=True)

    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    print(f"Waiting for the redirect on {Config.OAUTH_REDIRECT_URI} ...")
    if use_tls:
        print(
            "  (self-signed certificate - accept the browser warning if prompted)"
        )
    deadline = time.monotonic() + REDIRECT_TIMEOUT_SECONDS
    try:
        while not captured and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

    if "error" in captured:
        raise RuntimeError(captured["error"])
    if "code" not in captured:
        raise RuntimeError(
            "Timed out waiting for the authorization redirect. "
            f"Confirm that {Config.OAUTH_REDIRECT_URI} is listed under your app's "
            "valid OAuth redirect URIs."
        )
    return captured["code"]
