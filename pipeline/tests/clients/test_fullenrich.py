import httpx
import pytest

from discovery.clients.fullenrich import (
    ERROR_BODY_LOG_LIMIT,
    describe_failure,
    error_body_excerpt,
    redact_credentials,
)

API_KEY = "synthetic-fullenrich-key-0123456789"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FULLENRICH_API_KEY", API_KEY)


def _response(status_code: int, text: str) -> httpx.Response:
    request = httpx.Request("GET", "https://app.fullenrich.com/api/v2/contact/enrich/bulk/job-1")
    return httpx.Response(status_code, text=text, request=request)


class TestRedactCredentials:
    def test_removes_the_configured_key_value(self) -> None:
        assert API_KEY not in redact_credentials(f"key was {API_KEY} here")

    def test_removes_any_bearer_token_even_when_not_our_key(self) -> None:
        redacted = redact_credentials("Authorization: Bearer some.other-token_123==")
        assert "some.other-token_123" not in redacted
        assert redacted == "Authorization: Bearer [REDACTED]"

    def test_keeps_the_bare_word_from_fullenrichs_documented_auth_error(self) -> None:
        message = "Authorization headers do not have prefix 'bearer'"
        assert redact_credentials(message) == message

    def test_without_a_configured_key_still_strips_bearer_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FULLENRICH_API_KEY")
        assert redact_credentials("bearer abc.def") == "Bearer [REDACTED]"


class TestErrorBodyExcerpt:
    def test_redacts_before_truncating(self) -> None:
        # The key straddles the log limit: truncating first would leak its first half.
        padding = "x" * (ERROR_BODY_LOG_LIMIT - 10)
        excerpt = error_body_excerpt(_response(500, padding + API_KEY))
        assert API_KEY[:10] not in excerpt
        assert len(excerpt) <= ERROR_BODY_LOG_LIMIT


class TestDescribeFailure:
    def test_http_status_error_includes_status_and_redacted_body(self) -> None:
        response = _response(500, f'{{"message": "echoed Bearer {API_KEY}"}}')
        exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
        described = describe_failure(exc)
        assert described.startswith("HTTP 500 ")
        assert API_KEY not in described

    def test_transport_error_has_no_response_and_still_describes(self) -> None:
        request = httpx.Request("GET", "https://app.fullenrich.com/api/v2/contact/enrich/bulk/job-1")
        described = describe_failure(httpx.ReadTimeout("timed out", request=request))
        assert described == "ReadTimeout: timed out"

    def test_malformed_json_error_describes(self) -> None:
        with pytest.raises(ValueError) as info:
            httpx.Response(200, content=b"not-json").json()
        assert describe_failure(info.value).startswith("JSONDecodeError: ")
