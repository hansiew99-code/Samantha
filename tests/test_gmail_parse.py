"""Gmail payload parsing (pure functions, no API)."""

import base64

from samantha.integrations.gmail import _http_status, extract_body


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_extract_plain_body():
    payload = {"mimeType": "text/plain", "body": {"data": b64("hello world")}}
    assert extract_body(payload) == "hello world"


def test_extract_from_multipart():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<b>html</b>")}},
            {"mimeType": "text/plain", "body": {"data": b64("plain wins")}},
        ],
    }
    assert extract_body(payload) == "plain wins"


def test_extract_nested_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/plain", "body": {"data": b64("deep")}}],
            }
        ],
    }
    assert extract_body(payload) == "deep"


def test_extract_handles_missing_body():
    assert extract_body({"mimeType": "text/plain", "body": {}}) == ""
    assert extract_body({}) == ""


def test_only_an_actual_404_is_treated_as_expired_history():
    class Response:
        status = 404

    expired = RuntimeError("expired")
    expired.resp = Response()

    assert _http_status(expired) == 404
    assert _http_status(TimeoutError("network")) is None
