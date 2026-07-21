"""Spend accounting + budget governor (BRIEF §9).

Phase 1 ships the price table and the ledger (every API call is recorded).
Phase 4 adds the mode thresholds and /spend.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
    def __init__(self, conn: sqlite3.Connection, daily_budget_usd: float) -> None:
        self.conn = conn
        self.daily_budget_usd = daily_budget_usd

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
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM spend_log "
            "WHERE created_at >= date('now')"
        ).fetchone()
        return float(row["c"])

    def spent_month(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM spend_log "
            "WHERE created_at >= date('now', 'start of month')"
        ).fetchone()
        return float(row["c"])
