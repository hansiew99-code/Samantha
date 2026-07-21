# Samantha — Build Brief

> A build specification for Claude Opus 4.8. Build the system described here phase by phase (§11), verifying each milestone before moving on. Everything in this document has been decided and approved by the owner — do not re-litigate architecture choices; do ask if a credential or account-level setup step is missing.

## Context & requirements

Samantha is a personal AI assistant modeled on Samantha from *Her* / Andy from *The Devil Wears Prada*: proactive, always on, never forgets, and connected to the owner's real tools. She lives as a 24/7 daemon and talks to the owner exclusively through Telegram.

**Confirmed decisions:**

- **Hosting:** free cloud VPS (Oracle Cloud Always Free tier or equivalent) — RM0/month, runs 24/7 as a systemd service.
- **v1 integrations:** Google Calendar, Gmail, Slack, ClickUp — plus Telegram as the chat interface.
- **Autonomy:** *confirm outbound only* — she freely manages the owner's own things (calendar events, reminders, task states); anything reaching another person (send email, Slack reply) is drafted and requires a Telegram tap-to-approve.
- **Usage volume:** moderate, 20–60 messages/day.
- **Token budget:** < RM25/month (≈ USD $5.30 at ~4.7 MYR/USD → **~$0.18/day**), enforced by a budget governor. Cost must not balloon as memory grows, and quality must not degrade over time.

## 1. Vision & non-negotiable principles

1. **She never forgets.** All memory lives in SQLite on disk — the LLM context window is never the store of record. Facts are superseded, never deleted; raw conversation logs are archived forever.
2. **Deterministic core, LLM at the edges.** Reminders, schedules, event filtering, free/busy math, and budget accounting run as plain code with **zero tokens**. The LLM is invoked only to interpret, decide, and compose.
3. **Cost is O(1) per interaction, regardless of memory size.** Context is assembled from a small fixed core block + top-k retrieved facts + a bounded conversation window. Ten thousand stored memories cost the same per call as ten. This is what keeps the budget from ballooning over time.
4. **Cheap by default, smart on demand.** Haiku handles the day-to-day; Sonnet handles multi-step reasoning; Opus is reserved for genuinely hard planning. Routing is automatic.
5. **Proactive, not just reactive.** She initiates: morning briefs, deadline nudges, inbox triage, calendar conflict warnings — driven by a scheduler and integration events, not by the user asking.
6. **User instructions change her behavior durably.** "Stop reminding me about ClickUp stuff" becomes a persisted rule that the proactivity engine applies deterministically forever after — not a soft preference that fades out of context.

## 2. Architecture overview

One Python asyncio daemon on the VPS (systemd service, auto-restart). Components:

```
┌─────────────────────────────────────────────────────────────┐
│                      samantha daemon                        │
│                                                             │
│  Telegram gateway (long-polling — no public URL needed)     │
│        │  user msgs / approval taps        ▲ push msgs      │
│        ▼                                   │                │
│  ┌──────────┐   ┌────────────────┐   ┌───────────────┐     │
│  │  Router  │──▶│  Agent brain   │──▶│ Action layer  │     │
│  │ (tiered) │   │ (Claude + tool │   │ confirm-gate, │     │
│  └──────────┘   │  loop)         │   │ integrations  │     │
│        ▲        └───────┬────────┘   └───────────────┘     │
│        │                │ tools                             │
│  ┌─────┴─────┐   ┌──────▼───────┐   ┌────────────────┐     │
│  │ Event bus │   │    Memory    │   │ Budget governor│     │
│  │ + rules   │   │ (SQLite+FTS5)│   │ (spend ledger) │     │
│  └─────▲─────┘   └──────────────┘   └────────────────┘     │
│        │                                                    │
│  Scheduler (APScheduler): reminders, digests, sweeps,       │
│  nightly consolidation, integration pollers                 │
└─────────────────────────────────────────────────────────────┘
```

**Key transport choices (all avoid needing a public HTTPS endpoint on the free VPS):**
- Telegram: **long polling** via `python-telegram-bot` (free API, no webhook needed).
- Slack: **Socket Mode** (`slack-sdk`) — real-time events over an outbound websocket.
- Gmail: poll every ~5 min using the **history API** (delta sync, near-zero quota cost).
- Google Calendar: poll with **syncToken** every ~5 min (delta sync).
- ClickUp: poll tasks/due-dates every ~10 min.

## 3. Tech stack

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.12, asyncio, single process |
| Telegram | `python-telegram-bot` (v21+, async) |
| Scheduling | `APScheduler` with SQLite jobstore (jobs survive restarts) |
| Storage | SQLite + FTS5 (built-in full-text search — free retrieval, no vector DB in v1) |
| LLM | `anthropic` Python SDK |
| Google | `google-api-python-client` + `google-auth-oauthlib` (Calendar + Gmail, one OAuth consent) |
| Slack | `slack-sdk` (Socket Mode) |
| ClickUp | plain `httpx` against ClickUp REST v2 |
| Deploy | `systemd` unit, `.env` for secrets, single VPS |

Models (exact IDs — do not add date suffixes):
- `claude-haiku-4-5` — default worker ($1/$5 per MTok). No extended thinking.
- `claude-sonnet-5` — escalation tier ($3/$15; **intro $2/$10 through 2026-08-31**). `thinking: {"type": "adaptive"}`, `output_config: {"effort": "low"}` for routine escalations, `"medium"` for drafting.
- `claude-opus-4-8` — rare, hard tasks ($5/$25). `thinking: {"type": "adaptive"}`, effort `high`.

## 4. Tiered model routing (the single biggest cost lever)

Routing happens **before** any API call, in this order:

1. **No-LLM shortcuts (0 tokens).** Approval taps, `/spend`, `/pause`, `/help`, reminder snooze buttons, and scheduler-fired reminder deliveries never touch the API.
2. **Haiku by default.** Every user message goes to `claude-haiku-4-5` with the standard context assembly (§5). Haiku executes the tool loop for: setting reminders, memory saves/queries, task lookups, calendar reads, quick answers, rule changes.
3. **Self-escalation tool.** Haiku's toolset includes `escalate(reason, model)` — when it judges a task needs deeper reasoning (multi-constraint scheduling across people, composing a sensitive email, weekly planning), it hands the *same context* to Sonnet or Opus. Also escalate automatically on: user says "think hard", drafting outbound email > trivial, morning digest composition (Sonnet), and any task Haiku failed at once already.
4. **Expected mix** at moderate usage: ~85% Haiku / ~13% Sonnet / ~2% Opus calls.

## 5. Memory system — "never forgets" without context bloat

### Storage (SQLite, ground truth)

| Table | Purpose |
|---|---|
| `core_memory` | The always-in-context block: identity, top standing facts, active preferences. Hard cap ~1,500 tokens, rendered deterministically (sorted) for cache stability. |
| `facts` | Subject–predicate–object rows with `created_at`, `source` (chat/email/etc.), `superseded_by` (nullable). Contradictions **supersede** — old row kept, flagged, excluded from retrieval. Temporal versioning, Zep-style. |
| `people` | Contacts: names, emails, relationship, timezone, notes, VIP flag. |
| `tasks` / `reminders` | Local task + reminder store; reminders carry APScheduler job IDs. |
| `rules` | Persisted behavior rules (see §7) — e.g. `{source: "clickup", action: "suppress_reminders", scope: "*"}`. |
| `messages` | Full raw conversation log, both directions, forever. Archived, never in full context. |
| `pending_actions` | Outbound drafts awaiting Telegram approval. |
| `spend_log` | Per-API-call token usage × price table (see §9). |
| `integration_state` | Sync tokens, cursors, last-seen IDs per service. |

### Context assembly per LLM call (bounded, ~2.5–4K input tokens typical)

1. **System prompt + core memory** (~2–3K tokens, byte-stable, `cache_control` breakpoint on the last block). Volatile data (current time, today's date) goes *after* the breakpoint, never inside it.
2. **Retrieved facts**: FTS5 search over `facts` + `people` using the user message; inject **top 5–8 rows only** (research shows retrieval-based injection cuts memory tokens ~70% vs. dumping history).
3. **Conversation window**: last ~10 turns verbatim + one running summary line for anything older (summary refreshed by the nightly job, not per-call).
4. The user message + tool results.

### Nightly consolidation (the anti-bloat mechanism)

A 03:00 job submits the day's transcript to the **Batch API** (50% discount, `claude-haiku-4-5`):
- Extract new facts → insert into `facts` (single-pass, ADD-only extraction; dedupe/conflict-resolution happens at write time by superseding, not by extra LLM passes).
- Refresh the `core_memory` block (promote/demote facts by recency + reference frequency; enforce the 1,500-token cap).
- Refresh each conversation's running summary.

Result: memory grows unboundedly **on disk**, while per-call context — and therefore cost — stays flat. Quality also stays flat: retrieval quality depends on FTS relevance, not context size. (If FTS recall ever feels weak, phase-5 upgrade path is local embeddings via `sqlite-vec` + a small local model — still $0 in API tokens.)

## 6. Proactivity engine

**Event-driven, batched, rule-filtered:**

1. **Sources** enqueue events onto an internal bus: new Gmail thread, Slack mention/DM, ClickUp due-date approaching, calendar event created/moved/conflicting, scheduler ticks.
2. **Rules filter deterministically** (0 tokens): every event passes the `rules` table first. A suppressed source/scope is dropped before any LLM sees it.
3. **Sweep, don't drip**: surviving events accumulate and are processed in **batched sweeps** (every ~30 min during waking hours) — *one* Haiku call per sweep covering all pending events, deciding: notify now / fold into next digest / ignore. Never one LLM call per event.
4. **Digests**: morning brief 07:30 (today's calendar, top tasks, overnight inbox triage — composed by Sonnet) and a short evening review 21:30 (Haiku). Quiet hours 23:00–07:00: nothing pushes except explicitly-set reminders.
5. **Reminders**: firing is pure APScheduler → Telegram `sendMessage` with the pre-composed text plus inline `Done / Snooze 1h / Snooze to tomorrow` buttons. **Zero tokens per fire.** Message text is composed once, at creation time.

## 7. "Directly affect change" — the rules mechanism

When the user says *"stop reminding me about stuff from ClickUp"*, Haiku calls the `rules.add` tool → a row is persisted → the event filter in §6 enforces it forever, deterministically. Same mechanism for: mute a Slack channel, change digest times, mark a sender as VIP/ignore, change quiet hours. `rules.list` / `rules.remove` let the user audit and undo by chatting. Rules are also echoed into `core_memory` so she *knows* her own standing orders.

## 8. Integrations & tool surface

All tools are Python functions exposed to Claude via the SDK tool loop. **Prescriptive descriptions** stating *when* to call each tool (this measurably improves routing on current models). Fixed, sorted tool list — never varies per request (cache stability).

| Tool | Notes |
|---|---|
| `reminders.set / cancel / list` | Writes APScheduler jobs + `reminders` rows. |
| `memory.save / search` | Explicit saves ("remember that…") + retrieval beyond the auto-injected top-k. |
| `rules.add / remove / list` | §7. |
| `calendar.list_events / create_event / update_event / delete_event` | User's own calendar — auto-allowed. |
| `calendar.find_slots(attendees, duration, window)` | Calls Google **freeBusy** for other people's calendars; slot intersection computed **in code**; LLM only phrases the result. |
| `gmail.search / read_thread / draft_reply` | Drafting is free-form; `draft_reply` stores to `pending_actions`. |
| `gmail.send(pending_action_id)` | **Gated**: only executable after a Telegram approval tap. |
| `slack.read_mentions / draft_reply` / `slack.send` | Same gate on send. |
| `clickup.list_tasks / update_task / complete_task` | Own-task mutations auto-allowed. |
| `escalate(reason, model)` | §4. |
| `telegram.notify(text)` | For proactive pushes from sweeps. |

**Confirmation flow (outbound only):** draft → `pending_actions` row → Telegram message with the full draft + inline `Send / Edit / Discard` keyboard → tap `Send` executes with no further LLM call; `Edit` reopens a short Haiku exchange. Everything touching only the user's own data executes immediately.

**Auth setup (one-time, documented in README):** Google Cloud project with Calendar + Gmail scopes, OAuth desktop flow run once on the VPS (refresh token persisted); Slack app with Socket Mode + `app_mentions:read`, `im:history`, `chat:write`; ClickUp personal API token; Telegram bot via BotFather. **Security:** secrets in `.env` (never in code or memory tables); Telegram handler hard-checks the user's chat ID — messages from anyone else are ignored.

## 9. Token-budget engineering & the governor

**Techniques (in order of impact):**
1. Deterministic shortcuts + rule pre-filtering (most "activity" costs 0 tokens).
2. Haiku-first routing (§4).
3. Bounded context assembly (§5) — cost independent of memory size.
4. Batched sweeps + Batch API for the nightly job (50% off).
5. **Prompt caching**: `cache_control: {"type": "ephemeral"}` on the last system block. Note the minimum cacheable prefix: **4,096 tokens on Haiku 4.5 / Opus 4.8, 2,048 on Sonnet**. Keep the system+core block ~2–3K: it will cache on Sonnet/Opus calls; on Haiku it silently won't — that's fine, Haiku input is cheap, and *leanness beats caching* at this scale. Verify with `usage.cache_read_input_tokens` in the spend log.
6. `max_tokens` caps per route (Haiku 1024, Sonnet 2048, Opus 4096); tool results truncated to what's needed (e.g. email bodies clipped to first ~1,500 chars for triage).

**Budget governor:** every API response's `usage` block is priced (including cache read/write rates and batch discounts) into `spend_log`. Daily budget **$0.177** (= $5.30/30):
- **< 70%**: normal operation.
- **70–100%**: degrade — Haiku-only (no escalation), sweeps hourly, digests shortened.
- **≥ 100%**: deterministic-only mode — reminders still fire (free), events queue for tomorrow, chat replies with a one-line "budget resting" note. Never silently overspends.
- `/spend` in Telegram shows today/month totals and the model mix. Monthly rollover resets counters.

**Realistic spend estimate:**

| Scenario | Assumptions | Est./month |
|---|---|---|
| Light (~15 msgs/day) | mostly Haiku, 2 digests, nightly batch | **RM6–10** |
| **Moderate (~30 msgs/day)** | ~85/13/2 model mix, ~1.5 calls/msg, sweeps + digests + batch | **RM12–20** |
| Upper-moderate (~60 msgs/day) | governor actively throttling Opus/Sonnet | **capped at RM25** |

Line items inside the moderate estimate: interactive chat ≈ RM8–13, digests ≈ RM2–3, sweeps ≈ RM1–2, nightly consolidation (batch) ≈ RM1–2. Sonnet-5 intro pricing (through Aug 2026) sits inside these ranges; expect the top of the range after it lapses. **The budget cannot balloon over time** because per-call context is bounded (§5) and the governor is a hard ceiling — the failure mode is graceful degradation, never a surprise bill.

## 10. Personality

System prompt defines her voice: warm, wry, anticipatory, radically competent — Andy-from-*Devil-Wears-Prada* energy. Telegram-native brevity (2–4 sentences unless asked), no corporate hedging, remembers and references personal context naturally, admits uncertainty plainly. She refers to her own standing rules when relevant ("I've kept ClickUp muted like you asked — one thing did look urgent, though"). The prompt text is **frozen** (byte-stable for caching); persona tweaks go through `core_memory`, not prompt edits.

## 11. Build phases (execute in order; each ends with a working milestone)

**Phase 0 — Skeleton.** Repo layout, `pyproject.toml`, config loader, SQLite schema + migrations, Telegram gateway echoing messages, chat-ID allowlist, systemd unit + deploy notes for Oracle Always Free. *Milestone: message her, she echoes, survives reboot.*

**Phase 1 — Brain + memory + reminders.** Anthropic tool loop, router with Haiku default + `escalate`, context assembly (§5), `memory.*`, `reminders.*`, spend logging. *Milestone: "remind me to call mum tomorrow at 6pm" → fires on time, zero tokens at fire; "remember I'm allergic to peanuts" → recalled next day.*

**Phase 2 — Google.** OAuth setup flow, Calendar tools (incl. `find_slots` with freeBusy), Gmail polling + triage events, `draft_reply` + the pending-action approval flow end-to-end. *Milestone: "when are Sarah and I both free Thursday?" answered; email draft approved via tap and actually sent.*

**Phase 3 — Slack + ClickUp + proactivity.** Socket Mode events, ClickUp poller, event bus + rules engine, batched sweeps, morning/evening digests, quiet hours. *Milestone: "stop reminding me about ClickUp" persists across restarts; morning brief arrives at 07:30.*

**Phase 4 — Consolidation + governor.** Nightly Batch API job, core-memory refresh, conversation summaries, budget governor thresholds + `/spend`, cache-hit verification in spend log. *Milestone: a week of use with flat per-day context sizes and spend inside budget.*

**Phase 5 — Beyond the floor (backlog, pick opportunistically).** Voice notes (Telegram voice → local Whisper transcription, $0 tokens); calendar-conflict auto-detection; VIP-sender learning from reply behavior; weekly Sunday planning session (Opus); birthday/anniversary tracking from email signatures & chat; travel-time buffers before meetings; `sqlite-vec` semantic retrieval upgrade; `/pause N hours` mode.

## 12. Verification (applies throughout)

- **Unit**: rules filtering, slot intersection, context-assembly token bounds (assert assembled prompt < 6K tokens with 10K synthetic facts on disk), price-table math vs. known `usage` fixtures.
- **Integration**: a `--dry-run` flag that stubs outbound sends; replay fixture events through the bus and assert sweep batching (N events → 1 LLM call).
- **Live smoke test per phase**: the milestone lines above, run on the VPS.
- **Cost regression**: `/spend` after 3 days of real use; assert cache reads > 0 on Sonnet calls and daily spend ≤ $0.18; simulate governor thresholds by lowering the cap temporarily.
- **The never-forget test**: tell her a fact, wait past one nightly consolidation, restart the daemon, ask about it obliquely — she must retrieve it.

## 13. Research grounding

- Memory-framework landscape (Letta/MemGPT core-recall-archival model, Zep temporal supersession, Mem0 cost profile): [Agent Memory in 2026: Mem0 vs Zep vs Letta](https://maidul-haque.vercel.app/blog/agent-memory-architectures-2026/), [Mem0 vs Letta vs Zep comparison](https://aiworkflowlab.dev/article/agent-memory-mem0-vs-zep-vs-letta-2026), [Best AI Agent Memory Frameworks 2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- Token-cost tactics (retrieval-based injection ≈ 72% token cut; single-pass extraction; model routing as top lever): [The 2026 Token Optimization Playbook](https://mem0.ai/blog/the-2026-token-optimization-playbook-cut-ai-agent-memory-costs-3%E2%80%934x), [AI Cost Optimization](https://www.growthaccelerationpartners.com/blog/ai-cost-optimization-and-the-problem-of-runaway-token-costs)
- Scheduled-agent cost discipline: [AI Agent Job Scheduling Patterns 2026](https://fast.io/resources/ai-agent-job-scheduling/)
- Pricing, caching minimums, Batch API, model IDs: Anthropic API documentation (verified current as of this session).

We deliberately **build the memory layer ourselves on SQLite** rather than adopting Letta/Mem0/Zep: at single-user scale their hosted tiers add cost and their self-hosted stacks add heavy infrastructure to a free VPS, while the mechanisms that matter (core/recall/archival tiers, temporal supersession, retrieval-based injection) are straightforward to implement directly — and we keep full control of the token profile.
