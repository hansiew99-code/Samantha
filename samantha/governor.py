"""Spend accounting + budget governor (BRIEF §9).

Every API call is priced into the ledger; the daily budget drives three modes:

  normal        < 70%   full routing, Opus allowed
  degraded      70-100% Haiku-only, sweeps hourly, digests shortened
  deterministic ≥ 100%  no LLM at all — reminders still fire (they're free),
                        events queue for tomorrow, chat gets a resting note

The governor is a hard ceiling: the failure mode is a quieter Samantha,
never a surprise bill.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .router import HAIKU, OPUS

NORMAL = "normal"
DEGRADED = "degraded"
DETERMINISTIC = "deterministic"

SOFT_THRESHOLD = 0.70
HARD_THRESHOLD = 1.00

USD_TO_MYR = 4.7  # display only

# USD per million tokens: input, output, cache_read, cache_write (5m TTL).
# Conservative sticker prices — Sonnet 5 intro pricing is cheaper until
# 2026-08-31; we budget at the post-intro rate on purpose.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-sonnet-5": (3.00, 15.00, 0.30, 3.75),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
}

BATCH_DISCOUNT = 0.5  # Batch API: 50% off input+output


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def from_api(cls, usage: object) -> "Usage":
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )


def price_usage(model: str, usage: Usage, batch: bool = False) -> float:
    if model not in PRICES:
        raise ValueError(f"no price entry for model {model!r}")
    in_p, out_p, read_p, write_p = (p / 1_000_000 for p in PRICES[model])
    cost = usage.input_tokens * in_p + usage.output_tokens * out_p
    if batch:
        cost *= BATCH_DISCOUNT
    # Cache economics apply to non-batch traffic.
    cost += usage.cache_read_input_tokens * read_p
    cost += usage.cache_creation_input_tokens * write_p
    return cost


class Governor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        daily_budget_usd: float,
        timezone_name: str = "UTC",
    ) -> None:
        self.conn = conn
        self.daily_budget_usd = daily_budget_usd
        self.timezone_name = timezone_name

    def record(self, model: str, purpose: str, usage: Usage, batch: bool = False) -> float:
        cost = price_usage(model, usage, batch=batch)
        self.conn.execute(
            """INSERT INTO spend_log(model, purpose, input_tokens, output_tokens,
                                     cache_read_tokens, cache_write_tokens, batch, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                model,
                purpose,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_input_tokens,
                usage.cache_creation_input_tokens,
                int(batch),
                cost,
            ),
        )
        self.conn.commit()
        return cost

    def spent_today(self) -> float:
        now = datetime.now(ZoneInfo(self.timezone_name))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM spend_log "
            "WHERE created_at >= ?",
            (self._utc_sql_timestamp(start),),
        ).fetchone()
        return float(row["c"])

    def spent_month(self) -> float:
        now = datetime.now(ZoneInfo(self.timezone_name))
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM spend_log "
            "WHERE created_at >= ?",
            (self._utc_sql_timestamp(start),),
        ).fetchone()
        return float(row["c"])

    @staticmethod
    def _utc_sql_timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # -- budget modes (BRIEF §9) ---------------------------------------------

    def mode(self) -> str:
        ratio = self.spent_today() / self.daily_budget_usd if self.daily_budget_usd else 0
        if ratio >= HARD_THRESHOLD:
            return DETERMINISTIC
        if ratio >= SOFT_THRESHOLD:
            return DEGRADED
        return NORMAL

    def max_tier(self) -> str:
        """Highest model tier currently allowed. In deterministic mode the
        caller shouldn't be making LLM calls at all — Haiku is the floor if
        something must run (e.g. an already-in-flight loop)."""
        return OPUS if self.mode() == NORMAL else HAIKU

    def spend_report(self) -> str:
        today = self.spent_today()
        month = self.spent_month()
        pct = (today / self.daily_budget_usd * 100) if self.daily_budget_usd else 0
        lines = [
            f"Today: ${today:.4f} of ${self.daily_budget_usd:.3f} ({pct:.0f}%) — mode: {self.mode()}",
            f"This month: ${month:.2f} (≈ RM{month * USD_TO_MYR:.2f})",
        ]
        rows = self.conn.execute(
            "SELECT model, COUNT(*) AS calls, SUM(cost_usd) AS cost, "
            "SUM(cache_read_tokens) AS cached "
            "FROM spend_log WHERE created_at >= ? "
            "GROUP BY model ORDER BY cost DESC",
            (
                self._utc_sql_timestamp(
                    datetime.now(ZoneInfo(self.timezone_name)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                ),
            ),
        ).fetchall()
        if rows:
            lines.append("Today by model:")
            for r in rows:
                short = r["model"].replace("claude-", "")
                lines.append(
                    f"  {short}: {r['calls']} calls, ${r['cost']:.4f}"
                    + (f", {r['cached'] // 1000}K cached reads" if r["cached"] else "")
                )
        return "\n".join(lines)
