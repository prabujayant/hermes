import pytest

from threads_api.diagnose import classify_token_probe


class TestClassifyTokenProbe:
    def test_invalid_client_id_is_explained(self):
        status, detail = classify_token_probe("Invalid client_id: 12345")

        assert status == "FAIL"
        assert "Threads use case" in detail
        assert "Threads App ID" in detail

    def test_invalid_client_id_case_insensitive(self):
        status, _ = classify_token_probe("INVALID CLIENT_ID: 1")
        assert status == "FAIL"

    def test_redirect_uri_rejection(self):
        status, detail = classify_token_probe("Invalid redirect_uri: http://x")
        assert status == "FAIL"
        assert "Redirect URI" in detail

    def test_invalid_secret(self):
        status, detail = classify_token_probe("Invalid client_secret")
        assert status == "FAIL"
        assert "APP_SECRET" in detail

    def test_code_error_means_app_id_accepted(self):
        status, detail = classify_token_probe(
            "Invalid verification code format"
        )
        assert status == "OK"
        assert "accepted" in detail

    def test_unknown_message_warns(self):
        status, detail = classify_token_probe("something unexpected")
        assert status == "WARN"

    @pytest.mark.parametrize("message", [None, ""])
    def test_empty_message_warns(self, message):
        status, _ = classify_token_probe(message)
        assert status == "WARN"
