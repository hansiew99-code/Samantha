"""Google OAuth scope compatibility tests.  No provider calls are made."""

from __future__ import annotations

import json
import stat

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from samantha.integrations import google_auth


class FakeCredentials:
    def __init__(self, *, expired: bool, scopes: list[str]):
        self.expired = expired
        self.refresh_token = "test-refresh-token"
        self.scopes = scopes
        self.refresh_requests: list[object] = []

    def refresh(self, request: object) -> None:
        self.refresh_requests.append(request)
        self.expired = False

    def to_json(self) -> str:
        return json.dumps({"scopes": self.scopes, "refreshed": True})


def _write_token(path, scopes, *, string_form: bool = False) -> None:
    recorded = " ".join(scopes) if string_form else scopes
    path.write_text(json.dumps({"scopes": recorded}))
    path.chmod(0o644)


def _fake_loader(monkeypatch, credentials: FakeCredentials, calls: list[list[str]]) -> None:
    def load(_path: str, scopes: list[str]):
        calls.append(scopes)
        return credentials

    monkeypatch.setattr(Credentials, "from_authorized_user_file", staticmethod(load))


def test_current_token_loads_with_its_recorded_scopes_and_is_hardened(tmp_path, monkeypatch):
    token_path = tmp_path / "google_token.json"
    recorded_scopes = [*google_auth.SCOPES, "openid"]
    _write_token(token_path, recorded_scopes, string_form=True)
    credentials = FakeCredentials(expired=False, scopes=recorded_scopes)
    calls: list[list[str]] = []
    _fake_loader(monkeypatch, credentials, calls)

    loaded = google_auth.load_credentials(token_path)

    assert loaded is credentials
    assert calls == [recorded_scopes]
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_legacy_token_refreshes_with_legacy_scopes_not_new_scopes(tmp_path, monkeypatch):
    token_path = tmp_path / "google_token.json"
    _write_token(token_path, google_auth.LEGACY_SCOPES)
    credentials = FakeCredentials(expired=True, scopes=google_auth.LEGACY_SCOPES)
    calls: list[list[str]] = []
    _fake_loader(monkeypatch, credentials, calls)

    loaded = google_auth.load_credentials(token_path)

    assert loaded is credentials
    assert calls == [google_auth.LEGACY_SCOPES]
    assert calls[0] != google_auth.SCOPES
    assert len(credentials.refresh_requests) == 1
    assert json.loads(token_path.read_text())["refreshed"] is True
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_token_missing_a_required_capability_is_rejected_before_loading(
    tmp_path, monkeypatch, caplog
):
    token_path = tmp_path / "google_token.json"
    insufficient = [
        scope
        for scope in google_auth.SCOPES
        if scope != "https://www.googleapis.com/auth/gmail.send"
    ]
    _write_token(token_path, insufficient)

    def should_not_load(*_args, **_kwargs):
        raise AssertionError("invalid token must not reach Credentials loader")

    monkeypatch.setattr(
        Credentials,
        "from_authorized_user_file",
        staticmethod(should_not_load),
    )

    assert google_auth.load_credentials(token_path) is None
    assert "Gmail sending" in caplog.text
    assert "gmail.send" not in caplog.text
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_malformed_or_unscoped_token_is_rejected_without_secret_logging(
    tmp_path, caplog
):
    token_path = tmp_path / "google_token.json"
    secret = "do-not-log-this-refresh-token"
    token_path.write_text(json.dumps({"refresh_token": secret}))

    assert google_auth.load_credentials(token_path) is None
    assert secret not in caplog.text
    assert "ValueError" in caplog.text
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_fresh_consent_still_requests_only_current_least_privilege_scopes(
    tmp_path, monkeypatch
):
    credentials_path = tmp_path / "google_credentials.json"
    token_path = tmp_path / "google_token.json"
    requested: list[list[str]] = []

    class ConsentCredentials:
        def to_json(self) -> str:
            return json.dumps({"scopes": google_auth.SCOPES})

    class Flow:
        def run_local_server(self, *, port: int, open_browser: bool):
            assert port == 8123
            assert open_browser is False
            return ConsentCredentials()

    def make_flow(_path: str, scopes: list[str]):
        requested.append(scopes)
        return Flow()

    monkeypatch.setattr(
        InstalledAppFlow,
        "from_client_secrets_file",
        staticmethod(make_flow),
    )

    google_auth.run_consent_flow(credentials_path, token_path, port=8123)

    assert requested == [google_auth.SCOPES]
    assert requested[0] != google_auth.LEGACY_SCOPES
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
