import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"


class Config:
    APP_ID = os.getenv("APP_ID")
    APP_SECRET = os.getenv("APP_SECRET")
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
    THREADS_USER_ID = os.getenv("THREADS_USER_ID")
    OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI") or DEFAULT_REDIRECT_URI

    BASE_URL = "https://graph.threads.net/v1.0"
    ROOT = PROJECT_ROOT
    ENV_PATH = PROJECT_ROOT / ".env"

    @classmethod
    def _missing(cls, names):
        return [name for name in names if not getattr(cls, name)]

    @classmethod
    def validate_app(cls):
        """Validate the credentials required to start the OAuth flow."""
        missing = cls._missing(["APP_ID", "APP_SECRET"])
        if missing:
            raise EnvironmentError(
                "Missing app credentials: " + ", ".join(missing) + cls._hint()
            )

    @classmethod
    def validate(cls):
        """Validate the credentials required to call the API."""
        missing = cls._missing(
            ["APP_ID", "APP_SECRET", "ACCESS_TOKEN", "THREADS_USER_ID"]
        )
        if missing:
            raise EnvironmentError(
                "Missing credentials: " + ", ".join(missing) + cls._hint()
            )

    @classmethod
    def _hint(cls):
        return (
            f"\nSet them in {cls.ENV_PATH}, then run:"
            "\n  python -m threads_api.cli auth"
        )