# Samantha v0.2.0 — Attention & Follow-Through

**Status:** deployed to production

**Prepared:** 22 July 2026

**Delivery surface:** Telegram (not WhatsApp)

This patch changes Samantha from a mostly reactive chatbot with scheduled summaries into a more dependable assistant loop: observe, decide what deserves attention, keep commitments open, verify the source, and close the loop without making the owner repeat themselves.

The running Telegram bot was upgraded on 22 July 2026. The exact Ubuntu release environment passed **231 tests**, live read-only checks succeeded against Gmail, Calendar, and Google Chat, the memory migration passed SQLite integrity checks, and the service reached a stable `active/running` state with zero restarts and no error-level startup logs.

## The visible difference

Before:

> “Want me to search your inbox?”
>
> “Want me to set that reminder?”
>
> “Hmm — I came back empty. Try me again?”

After:

> “Checked—nothing from Shyan yet. I’m watching for it; if it’s still missing tomorrow at 9, I’ll tell you.”

The owner asked for the outcome once. Samantha performs the safe read, creates persistent private state, watches incoming Gmail, verifies again before interrupting, and sends one useful Telegram update.

## Added

### Conditional Gmail watches

- Added a persistent `watchers` store and scheduler for “if,” “unless,” “by the deadline,” and “tell me only if it is still missing” requests.
- A watch records the sender/subject match, when the message is expected, when to notify, the fallback text, and whether lateness itself matters.
- Samantha searches Gmail immediately when the watch is created.
- Incoming Gmail messages resolve matching watches before ordinary notification rules run.
- At the deadline and notification time, Samantha performs a live Gmail search instead of trusting a stale poll result.
- On-time messages close the watch silently. Late messages can produce a specific late-arrival update. Missing messages produce the requested fallback.
- Gmail unavailability is never treated as evidence that mail is missing. Samantha says she could not verify, leaves the watch open, and retries after 15 minutes.
- Active watches rehydrate after a restart. Duplicate model retries return the existing watch instead of scheduling multiple alerts.
- Watch creation, matching, deadline checks, and delivery use no additional LLM calls after the initial interpretation.

This first implementation is Gmail-only. Google Chat, Slack, calendar commitments, and general “waiting on” loops still need typed watcher adapters.

### Source freshness

- Gmail, Google Chat, and ClickUp polling now record their latest successful or failed sync.
- Google Chat keeps an independent cursor for each space, follows pagination, and advances healthy spaces without skipping a failed one. A partial read is recorded as a freshness failure rather than a false all-clear.
- Digests include Calendar, Gmail, Google Chat, and ClickUp freshness status in the briefing input.
- Digest instructions require Samantha to name a source she could not verify instead of saying or implying she checked everything.
- Active conditional watches are included in digest context as open loops.
- Background triage now receives the current time/timezone plus source and receipt timestamps, preserves email subjects, and bundles multiple urgent items into one considered Telegram interruption instead of a burst of pings.

### Safer autonomy boundaries

- The assistant prompt now tells Samantha to perform read-only checks and private reversible actions without asking for redundant permission.
- One owner request should produce one coherent reply; intermediate lookups and tool chaining stay silent.
- Email and Slack sends remain pending actions requiring a Telegram tap.
- Creating a calendar event with attendees now creates an approval draft; invitations are not sent before approval.
- Editing an attendee-free event remains immediate and private; editing an existing event with attendees now creates an approval draft before guests are notified.
- Calendar deletion now creates an approval draft because it is destructive and can affect attendees.
- Identical pending actions are deduplicated, reducing double-send risk from model retries.
- Email, Chat, task, calendar, attachment, and web content is explicitly labeled untrusted in the owner, sweep, and digest prompts. Only the authorized Telegram owner can create instructions or standing rules.

### Quiet hours

- Background event sweeps, proactive calendar scans, and scheduled digests all check `SAMANTHA_QUIET_HOURS`.
- Explicit reminders and conditional watches are intentionally exempt: they are commitments the owner asked Samantha to deliver.
- Work hours remain a separate gate for unsolicited proactive scans.
- Digest backlog items are terminally marked `digested` after a brief, so the same event is not resurfaced for the next 26 hours.
- Morning, afternoon, and evening jobs serialize with event sweeps, consume only the backlog IDs the composed message actually represented, and catch up at most the latest missed brief after a restart—never a burst of stale briefs.
- Suppression rules are re-applied immediately before both sweep and digest composition, including to provider events staged transactionally before the rule existed.

### Telegram delivery and continuity

- Long Telegram replies are split safely below Telegram's message limit; approval buttons remain on the final chunk.
- Telegram output is escaped as safe HTML, while balanced `**bold**`, `__italic__`, and inline-code markers render naturally instead of appearing literally as in the supplied screenshot.
- Proactive pushes and approval previews are persisted as assistant conversation turns only after Telegram accepts them. A reply such as “yes” therefore has the message it refers to in durable context.
- Reactive replies and button outcomes are also persisted only after every Telegram chunk succeeds; failed/partial sends no longer create a phantom reply in memory. Button taps are recorded as owner actions, so “Sent ✓”, “Snoozed”, and “Done” remain part of the next turn's continuity.
- Unsolicited pushes have a separate, capped context lane. Only the latest two unacknowledged pushes can re-enter a prompt, they cannot evict owner/reactive dialogue, and they disappear from working context after the next owner/reply turn.
- Reminder and watcher delivery now use explicit in-flight states, retry after transport failure, and recover interrupted delivery after restart instead of marking a commitment complete before it reaches Telegram.
- Recurring reminders retain their recurrence after Done-for-now or Snooze, persist the next occurrence after every successful fire, and catch up one missed occurrence after an outage rather than replaying every missed tick.
- Recurrence expressions are validated before persistence. A malformed legacy row is quarantined as `invalid` during restart instead of aborting rehydration for every healthy commitment.
- Reminder buttons carry an occurrence version. Once Done, Snooze, or Cancel handles an occurrence, an old Telegram button cannot revive it; a failed cancel race also leaves the live recurring job intact.
- A successful Telegram push is not retried merely because the subsequent history write fails, preventing duplicate reminders/digests during a transient SQLite lock.
- A Slack websocket failure records degraded source health and leaves Telegram, reminders, and the rest of Samantha running.

## Changed

### Voice and behavior

- Reworked the persona around continuity, judgment, and follow-through rather than forced slang or decorative warmth.
- Added explicit examples of a complete assistant move: check the source, understand the consequence, do the private prerequisite, then present the decision or approval.
- Added a direct ban on “want me to check?” when the check is read-only and the owner already asked for the outcome.
- Added an honesty rule for unavailable or stale integrations.
- Clarified that Telegram is where Samantha talks, pushes alerts, and asks for approval.

The prompt should improve behavior, but “sounds human” is not considered solved by prose alone. It still needs transcript-level evaluation across real owner scenarios; see the research note.

### Memory correctness

- Saving a fact no longer hides every prior fact with the same subject and predicate by default. Multi-value facts such as projects, preferences, and relationships can coexist.
- Exact duplicate facts are idempotent across whitespace/case variants; an explicit replacement also converges any older active value onto the existing desired fact.
- Supersession is now opt-in with `replace_existing=true` and is reserved for genuine corrections such as a changed timezone or deadline.
- Nightly extraction receives the same replacement rule.
- Consolidation no longer drops the beginning of an oversized transcript and then advances past it. It processes the oldest bounded chunk and checkpoints only the last included message.
- A partial, empty, or invalid consolidation result does not advance the cursor.
- An explicit `NONE` can clear a stale conversation summary; empty core sections are written as `(none)` instead of silently retaining old content.
- A timed-out provider batch is resumed on the next run instead of paying for a duplicate submission.
- Consumed batch payloads are deleted from the provider when the API allows it.

### Context and token control

- Recent conversation history now has a 1,600-token estimated budget rather than only a 20-row limit.
- Retrieved facts have an 800-token estimated budget.
- The running summary has a 220-token estimated budget.
- Very large replayed history rows and retrieved facts are clipped before prompt assembly; the current owner message is preserved.
- Tool definitions use strict schemas with `additionalProperties: false` to reduce malformed calls and repair turns.
- Tool results remain capped before re-entering context, with a 12,000-character aggregate ceiling across all tools and rounds in one turn.
- Durable raw Telegram history is searchable immediately, before the nightly consolidation, through a bounded memory-search tool.
- Individual core-memory values and the complete always-present core block now have hard size ceilings, including standing rules.
- Recent dialogue is budgeted as coherent user→assistant turn groups, so one long answer cannot consume the entire history budget and then lose the owner question needed for “continue.”

These are local character-based estimates, not exact preflight counts. Tool schemas and provider-added tokens are not included in that estimate. Exact token counting remains follow-up work.

### Tool-loop reliability

- If a tool succeeds but the model returns an empty final message, Samantha returns the deterministic tool receipt instead of “came back empty.”
- If the follow-up model call fails after a successful tool mutation, Samantha still returns that deterministic receipt instead of asking the owner to repeat the action.
- The spending mode is checked between tool rounds. Once recorded spend reaches deterministic mode, the loop stops starting further LLM calls.
- Model API calls are serialized inside the process and the governor is re-checked inside that lock, preventing simultaneous chat/digest/sweep calls from all spending the same remaining allowance.
- Successful private operations keep their receipt even if the budget boundary is crossed mid-turn.
- Reminder creation and pending actions are idempotent for identical retries.
- A runtime provenance policy labels Gmail, Chat, Slack, Calendar, ClickUp, stored raw-memory excerpts, attachments, and web/tool reads as untrusted. After one of those reads, new private mutation authority is removed and independently blocked at execution time. A mutation class remains available only when the original Telegram turn contains a direct owner request for that outcome; external approval drafts remain available because the final effect still requires the owner's tap.
- The provenance rule lives in the registry rather than only in prompt prose, and new tools must be classified there before receiving mutation authority.
- Natural compound requests remain possible—“find a slot and book it,” “read Sarah's email and remind me,” and “list the task then mark the first one done”—while quoted or informational phrases such as “how to update a task” grant no mutation authority.
- Proactive push context activates the same untrusted-data gate from the first model call. A visible, exact reminder offer can still be accepted naturally with “yes”; ambiguous calendar conflicts now ask which item should move instead of treating a bare affirmation as authority for an unspecified mutation.
- Completed mutation receipts outrank later read receipts on model/budget failure, so “Created event…” cannot be hidden by a subsequent calendar list and accidentally repeated.
- Identical mutation signatures execute at most once per tool loop. A second attempt after an error/uncertain outcome is blocked pending verification rather than blindly repeated.

### Gmail and Google Chat polling correctness

- The Gmail history cursor advances only after all message metadata in that poll is fetched successfully. A transient metadata failure no longer skips mail permanently.
- Only Gmail's real 404 “history expired” response triggers re-priming. Other network/auth/server errors preserve the cursor; an expired cursor performs a bounded inbox reconciliation from the last successful sync before it advances.
- Gmail search and poll results include the provider arrival timestamp used to distinguish on-time from late messages.
- Added a live Gmail matcher for sender, optional subject, and a lower-bound timestamp.
- Google Chat polling paginates per space and stores per-space cursors, so one failed room retains its checkpoint for retry while healthy rooms continue.
- Partial Google Chat results are not recorded as a successful full-source sync.
- Gmail and Google Chat stage each source event in the same SQLite transaction that advances its provider cursor. A crash can commit both the event and checkpoint, or neither, but cannot silently advance past an unseen item.
- Gmail reconciliation follows every page after an expired history cursor and keeps the old cursor on partial metadata reads. Messages deleted between list/history and metadata fetch are skipped without wedging the source.
- Google Chat enumerates every accessible space and page, parses fractional RFC3339 timestamps, filters the bot's own messages, and requires an explicit `GCHAT_SELF_ID` when enabled.
- Calendar and ClickUp list operations follow all provider pages. Free/busy fails closed on per-calendar provider errors instead of fabricating availability.
- Private Google Calendar inserts use a stable provider event ID derived from the exact event payload. Repeating after a timeout or owner/model retry resolves to the existing event rather than creating a duplicate.

### Budget accounting

- Daily and monthly spend boundaries now use the configured Samantha timezone rather than the server's UTC date.
- The governor is checked between multi-tool LLM rounds.

The local governor is not a provider-side prepaid limit. The cost of one request cannot be known exactly before it completes, so an in-flight call can cross `SAMANTHA_DAILY_BUDGET_USD`. Configure an Anthropic workspace limit/alert separately if a contractual ceiling is required.

### Google access and token handling

- Replaced the broad `calendar` and `gmail.modify` scopes with `calendar.events`, `calendar.freebusy`, `gmail.readonly`, and `gmail.send`; Google Chat remains read-only.
- Fresh consent uses the narrower grants, while tokens previously minted with `calendar` and `gmail.modify` remain compatible and refresh with their exact recorded grants. Tokens missing any required capability fail closed instead of producing a misleading partial connection.
- New, loaded, and refreshed `google_token.json` files are restricted to owner read/write (`0600`), and credential failures log only a safe error class rather than token or provider-response contents.
- The daemon sets an owner-only process umask; the SQLite database and `.env` are restricted to `0600`, and the systemd unit adds `UMask=0077`, `NoNewPrivileges`, and a private temporary directory.

### Approval integrity

- Pending actions claim an `executing` state before provider calls and become `sent` only after success. A timeout or interrupted execution becomes `uncertain` instead of being blindly retried and risking a duplicate send.
- Restart recovery also quarantines any action left `executing` as `uncertain` for manual verification.
- Tapping Edit locks the old draft before opening a revision turn, so a stale Send button cannot execute the superseded payload.
- Calendar deletion previews now show the fetched event title, time, location, guests, and provider ID before approval; Slack replies preserve their thread.

### Consolidation security and progress

- Nightly transcript rows are serialized as source-tagged JSONL. Facts and core memory receive only direct owner-channel rows; assistant/integration pushes remain available to the open-loop summary as untrusted context, never authority.
- Every Batch request now carries explicit prompt-injection/provenance instructions. Extracted facts must be grounded in direct owner text, and instruction-shaped facts, summaries, or core values are rejected before durable application.
- A repeatedly unsafe or malformed oldest chunk retries three times, then moves to an auditable raw quarantine while the cursor advances. The raw messages remain searchable, but one adversarial item can no longer poison core memory or starve every later legitimate memory forever.
- Resumed Batch jobs rebuild their validation transcript only through the original stored checkpoint; messages arriving while the batch was pending cannot retroactively authorize its output.

## Verification

- **231 tests passed locally and 231 tests passed in the exact Ubuntu x86_64 release environment.** The host run included all memory, watcher, reminder, approval, polling, security, and OAuth regression tests.
- Live read-only smoke checks passed for Gmail, Calendar, and Google Chat with the production OAuth token. The compatibility fix avoided re-consent and preserved the original grants.
- Production configuration validation passed for Anthropic, Telegram, Google, and Google Chat. Slack and ClickUp remain intentionally disabled because no credentials are configured.
- The production SQLite migration completed with `PRAGMA integrity_check=ok`; the new reminder-delivery and watcher schema is present.
- The systemd service is `active/running`, uses the new isolated virtual environment and hardened unit, has zero restarts, and produced no error-level log entries during the post-deploy observation window.
- A normal owner message in Telegram remains the final end-to-end conversational acceptance test; no synthetic message was sent from the deployment shell.

## Database migration

Startup applies the idempotent schema and compatibility migration, including the `watchers` table, active-watch index, reminder delivery versions, provider-event deduplication index, and consolidation quarantine. Existing facts, messages, reminders, rules, and pending actions are preserved.

The production rollout completed with a protected pre-upgrade database snapshot, environment/token backups, a disposable migration rehearsal, a fresh release virtual environment, host-side tests, live provider reads, and a short post-start stability observation. The previous code, virtual environment, service definition, and database snapshot remain on the host for rollback.

Rollback is code-level: stop the service, restore the previous build, and restore the database backup if the older build cannot tolerate the added schema. Do not delete the new watcher rows as a first response; they contain active commitments.

## Access required

No additional access is required for the currently enabled production sources: Telegram, Gmail, Calendar, and Google Chat are configured on the host. Slack and ClickUp remain disabled; enabling either later requires entering its credentials directly on the VM and running the source-specific smoke tests. Do not send passwords, API keys, OAuth refresh tokens, `.env`, or `samantha.db` over chat or commit them to GitHub.

## Not solved yet

- **Polling, not push, for Gmail/Chat/Calendar.** This creates minutes of latency and depends on healthy cursors; event-driven subscriptions plus periodic reconciliation are the intended next step.
- **Only Gmail has conditional watches.** Google Chat, Slack, calendar, ClickUp, approvals, and arbitrary commitments need a common open-loop state machine.
- **Universal semantic working memory.** Raw conversation remains searchable the same day, but generic commitments are not yet promoted automatically into typed open loops/facts on every turn. Gmail watches solve the named conditional-mail case, not universal commitment extraction.
- **Context accounting.** The assistant does not yet call the provider token-count endpoint before each request, and prompt caching does not remove cached tokens from the context window.
- **One-call budget overshoot.** Serialization prevents concurrent callers racing the balance, but a single in-flight response can still cross the local daily target.
- **Digest delivery durability.** Reminders and watches have claim/retry/recovery states, but composed digests still lack a transactional outbox and provider receipt. A Telegram failure preserves event backlog, yet the exact composed brief waits for a later scheduled/catch-up run.
- **Ambiguous external sends.** Provider timeouts are quarantined as `uncertain`, but Gmail and Slack do not offer a complete application-level idempotency receipt here; an operator must verify the provider before retrying.
- **Scheduled consolidation catch-up.** Nightly consolidation resumes submitted provider batches safely, but a daemon that was offline at the scheduled time does not yet launch a newly missed consolidation immediately on boot.
- **General structured proposals.** A bare “yes” safely redeems a visible reminder proposal; calendar/task changes intentionally require the owner to name the item/choice. A future immutable proposal payload can extend natural affirmations to more private action classes without trusting external push text.
- **Reactive/digest outbox.** Successful replies are logged only after delivery and failed reminder/watch delivery retries, but reactive replies and composed digests do not yet have a full provider-receipt outbox. A partial Telegram failure can still require a later retry/recomposition.

The research-backed target architecture and prioritized follow-up plan are in [docs/PROACTIVE_ASSISTANT_RESEARCH.md](docs/PROACTIVE_ASSISTANT_RESEARCH.md).
