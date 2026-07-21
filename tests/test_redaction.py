"""Secret redaction: pasted credentials never become durable history.

Sample "secrets" here are assembled at runtime from fragments so no complete
token literal ever exists in the source (keeps push-protection / secret
scanners happy while still exercising the real redaction patterns).
"""

from samantha.redaction import redact

# Built by concatenation on purpose — see module docstring.
_ANTHROPIC = "sk-ant-" + "api03-" + "A" * 90
_TELEGRAM = "1234567890" + ":" + "B" * 35
_SLACK = "xoxb-" + "123456789012-" + "c" * 20
_CLICKUP = "pk_" + "12345678_" + "D" * 16


def test_redacts_anthropic_key():
    out = redact("here is my key " + _ANTHROPIC)
    assert "sk-ant-" not in out
    assert "[redacted-secret]" in out


def test_redacts_telegram_token():
    out = redact("bot token " + _TELEGRAM)
    assert _TELEGRAM not in out
    assert "[redacted-secret]" in out


def test_redacts_slack_and_clickup():
    assert "[redacted-secret]" in redact("token: " + _SLACK)
    assert "[redacted-secret]" in redact("token: " + _CLICKUP)


def test_leaves_ordinary_prose_alone():
    s = "remind me to call the bank at 3pm about my account number question"
    assert redact(s) == s


def test_log_message_redacts_before_storage(memory, conn):
    memory.log_message("user", "save this: " + _ANTHROPIC)
    row = conn.execute("SELECT content FROM messages ORDER BY id DESC LIMIT 1").fetchone()
    assert "sk-ant-" not in row["content"]
    assert "[redacted-secret]" in row["content"]
