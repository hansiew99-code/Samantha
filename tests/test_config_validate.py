"""Config validation catches the paste-corruption crash class before startup."""

from pathlib import Path

from samantha.config import Settings


def make(**over) -> Settings:
    base = dict(
        anthropic_api_key="sk-ant-api03-realish-ascii-key",
        telegram_bot_token="123456:ABC-def_ghi",
        telegram_chat_id=5372883041,
        db_path=Path("x.db"),
    )
    base.update(over)
    return Settings(**base)


def test_valid_config_has_no_problems():
    assert make().validate() == []


def test_non_ascii_api_key_flagged():
    # This is the exact crash the user hit: key body replaced by bullet chars.
    bad = "sk-ant-a" + "•" * 100
    problems = make(anthropic_api_key=bad).validate()
    assert any("non-text" in p for p in problems)


def test_missing_api_key_flagged():
    assert any("ANTHROPIC_API_KEY is missing" in p for p in make(anthropic_api_key="").validate())


def test_wrong_prefix_flagged():
    assert any("sk-ant-" in p for p in make(anthropic_api_key="sk-wrongprefix-123").validate())


def test_corrupt_telegram_token_flagged():
    assert any("TELEGRAM_BOT_TOKEN" in p for p in make(telegram_bot_token="no-colon-here").validate())


def test_missing_chat_id_flagged():
    assert any("TELEGRAM_CHAT_ID" in p for p in make(telegram_chat_id=0).validate())


def test_google_chat_requires_self_id_when_enabled():
    problems = make(gchat_enabled_flag=True, gchat_self_id="").validate()
    assert any("GCHAT_SELF_ID is required" in problem for problem in problems)


def test_google_chat_accepts_self_id_when_enabled():
    problems = make(
        gchat_enabled_flag=True,
        gchat_self_id="users/123456",
    ).validate()
    assert not any("GCHAT_SELF_ID" in problem for problem in problems)


def test_google_chat_rejects_malformed_self_id():
    problems = make(gchat_enabled_flag=True, gchat_self_id="123456").validate()
    assert any("users/<id>" in problem for problem in problems)
