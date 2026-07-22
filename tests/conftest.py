"""Shared fixtures. Everything runs offline: no network, no API keys."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import time

import pytest

from samantha.config import Settings
from samantha.db import connect
from samantha.memory import Memory


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def memory(conn) -> Memory:
    return Memory(conn)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key="test-key",
        telegram_bot_token="",
        telegram_chat_id=0,
        db_path=tmp_path / "test.db",
        # Unit tests must not change behaviour when the wall clock crosses the
        # production quiet-hours boundary during an evening deployment.
        quiet_hours=(time(0, 0), time(0, 0)),
        dry_run=True,
    )
