"""Pre-flight diagnostics for the Threads API connection."""

import socket
import urllib.parse

import requests

from .config import Config

TOKEN_URL = "https://graph.threads.net/oauth/access_token"

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def classify_token_probe(message):
    """Map a token-endpoint error message onto (status, explanation)."""
    low = (message or "").lower()

    if "invalid client_id" in low or "invalid client id" in low:
        return "FAIL", (
            "Meta rejects this APP_ID for the Threads API. Either the app has no "
            "Threads use case, or APP_ID is the Facebook App ID rather than the "
            "Threads App ID (App Dashboard > App settings > Basic > Threads App ID)."
        )
    if "redirect" in low:
        return "FAIL", f"Redirect URI was rejected: {message}"
    if "invalid client_secret" in low or "invalid app secret" in low:
        return "FAIL", f"APP_SECRET was rejected: {message}"
    if "code" in low:
        return "OK", f"APP_ID accepted (expected code error: {message[:80]})"
    return "WARN", message or "Unrecognised response"


def check_env():
    if not Config.ENV_PATH.exists():
        return "FAIL", f"{Config.ENV_PATH} not found (copy .env.example to .env)"
    missing = Config._missing(["APP_ID", "APP_SECRET"])
    if missing:
        return "FAIL", "missing " + ", ".join(missing)
    if not Config.ACCESS_TOKEN:
        return "WARN", "no ACCESS_TOKEN yet; run `threads-publish auth`"
    return "OK", f"app id {Config.APP_ID}"


def check_threads_credentials():
    """Exchange a bogus code so Meta validates APP_ID and APP_SECRET first.

    Note that the Threads App ID is distinct from the Facebook App ID and is
    not addressable through graph.facebook.com, so this is the only meaningful
    way to verify the pair.
    """
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": Config.APP_ID,
            "client_secret": Config.APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": Config.OAUTH_REDIRECT_URI,
            "code": "connection-doctor-probe",
        },
        timeout=20,
    )
    payload = response.json()
    message = (
        payload.get("error", {}).get("message")
        or payload.get("error_message")
        or response.text[:200]
    )
    return classify_token_probe(message)


def check_redirect_uri():
    parsed = urllib.parse.urlparse(Config.OAUTH_REDIRECT_URI)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if scheme == "https" else 80)
    is_local = host in LOCAL_HOSTS

    if scheme != "https":
        return "FAIL", (
            f"{Config.OAUTH_REDIRECT_URI} is not https. Meta blocks the login "
            'redirect with "Insecure login blocked", so use an https URI such as '
            "https://localhost:8080/callback."
        )

    bind_host = host if is_local else "localhost"
    try:
        with socket.socket() as sock:
            sock.bind((bind_host, port))
    except OSError as exc:
        return "FAIL", f"cannot listen on {bind_host}:{port} ({exc})"

    if is_local:
        return "OK", f"{Config.OAUTH_REDIRECT_URI} is free; TLS is served locally"

    return "OK", (
        f"{Config.OAUTH_REDIRECT_URI} is remote, so the server will listen on "
        f"localhost:{port} behind your tunnel; make sure the URI is registered"
    )


CHECKS = (
    ("Environment file", check_env),
    ("Threads credentials", check_threads_credentials),
    ("Redirect URI", check_redirect_uri),
)


def run_checks():
    results = []
    for name, fn in CHECKS:
        try:
            status, detail = fn()
        except requests.RequestException as exc:
            status, detail = "FAIL", f"network error: {exc}"
        except Exception as exc:  # noqa: BLE001 - surfaced in the report
            status, detail = "FAIL", repr(exc)
        results.append((name, status, detail))
    return results
