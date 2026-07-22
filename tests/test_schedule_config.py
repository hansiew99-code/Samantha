"""Working-window parsing and the proactive/brief schedule defaults."""

from __future__ import annotations

import os

from samantha.app import build_app
from samantha.config import load_settings, parse_weekdays


def test_parse_weekdays_range_and_list():
    assert parse_weekdays("mon-fri") == {0, 1, 2, 3, 4}
    assert parse_weekdays("mon-sun") == {0, 1, 2, 3, 4, 5, 6}
    assert parse_weekdays("mon,wed,fri") == {0, 2, 4}
    assert parse_weekdays("SAT") == {5}


def test_schedule_defaults(monkeypatch):
    for var in (
        "SAMANTHA_MORNING_DIGEST", "SAMANTHA_AFTERNOON_DIGEST", "SAMANTHA_EVENING_DIGEST",
        "SAMANTHA_WORK_DAYS", "SAMANTHA_WORK_HOURS", "SAMANTHA_PROACTIVE_INTERVAL_MIN",
    ):
        monkeypatch.delenv(var, raising=False)
    s = load_settings(env_file=None)

    assert (s.morning_digest.hour, s.morning_digest.minute) == (9, 0)
    assert (s.afternoon_digest.hour, s.afternoon_digest.minute) == (14, 0)
    assert (s.evening_digest.hour, s.evening_digest.minute) == (21, 0)
    assert s.work_days == "mon-fri"
    assert (s.work_start_hour, s.work_end_hour) == (8, 19)
    assert s.proactive_interval_minutes == 30
    assert s.work_weekdays() == {0, 1, 2, 3, 4}


def test_schedule_overridable_from_env(monkeypatch):
    monkeypatch.setenv("SAMANTHA_WORK_DAYS", "mon-sat")
    monkeypatch.setenv("SAMANTHA_WORK_HOURS", "7-20")
    monkeypatch.setenv("SAMANTHA_PROACTIVE_INTERVAL_MIN", "15")
    monkeypatch.setenv("SAMANTHA_AFTERNOON_DIGEST", "13:30")
    s = load_settings(env_file=None)

    assert s.work_days == "mon-sat"
    assert (s.work_start_hour, s.work_end_hour) == (7, 20)
    assert s.proactive_interval_minutes == 15
    assert (s.afternoon_digest.hour, s.afternoon_digest.minute) == (13, 30)
    assert s.work_weekdays() == {0, 1, 2, 3, 4, 5}


def test_runtime_cron_triggers_use_owner_timezone(settings):
    """CronTrigger otherwise defaults to the VM clock (UTC), even when the
    scheduler itself was configured for the owner's timezone."""
    settings.dry_run = False
    app = build_app(settings)
    try:
        jobs = {job.id: job for job in app.scheduler.get_jobs()}
        for job_id in (
            "sweep",
            "proactive-scan",
            "digest-morning",
            "digest-afternoon",
            "digest-evening",
            "consolidation",
        ):
            assert str(jobs[job_id].trigger.timezone) == settings.timezone
    finally:
        app.conn.close()
