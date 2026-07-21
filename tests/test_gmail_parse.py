"""Gmail payload parsing (pure functions, no API)."""

import base64

from samantha.integrations.gmail import extract_body


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
