# 429 Handling Report — Telegram PDF Notebook Bot

## Changed behavior

All outgoing Bot API message creation, copy, and forward calls now pass through one shared FIFO rate-control layer configured on the application's Telegram request client. That includes handler `reply_text` calls, direct `send_message` calls, and `send_document`; polling and message edits are not treated as new outgoing sends. Default pacing is 20 sends per second bot-wide, at least one second between sends to the same private chat, and 3.05 seconds between sends to the same group. Telegram's [official FAQ](https://core.telegram.org/bots/faq) advises avoiding more than one message per second per chat, limits groups to 20 messages per minute, and describes bulk broadcast limits of about 30 messages per second. These defaults include headroom, but do not guarantee that 429 responses can never occur.

On HTTP 429, the adapter reads `RetryAfter.retry_after`, applies the returned duration plus a 0.1-second safety pad to the bot-wide queue and the originating chat, then retries the request through the same limiter. A request receives at most two extra 429 retries (three attempts total). Even after that bound is reached, its last cooldown remains in effect for later queued sends. This follows Telegram's [`ResponseParameters.retry_after`](https://core.telegram.org/bots/api#responseparameters), which specifies how many seconds remain before a request can be repeated. Only uploads that can be replayed safely are retried; PTB's normal document upload path materializes the file content before the request.

Limited exponential retries are reserved for connection failures whose underlying HTTPX cause proves the request was not sent (`ConnectError`, `ConnectTimeout`, or `PoolTimeout`). There are at most two additional attempts, with 0.25- and 0.5-second backoff, and each attempt still obeys the shared rate gate. Other network failures are not automatically replayed.

## Delivery and quota safeguards

A `sendDocument` network error or timeout with an ambiguous outcome is converted to an explicit uncertain-delivery failure; the document is not automatically sent again. The user-facing notice asks the user to check the chat before starting another conversion. The reservation is retained instead of refunded, preventing an uncertain delivery from immediately becoming a free retry. A confirmed successful document delivery is committed as before; known pre-delivery failures still refund; cleanup or bookkeeping failures after confirmed delivery never refund.

Reservations now persist `heartbeat_at` and a random process lease owner. While conversion or Telegram delivery is active, the owner renews the heartbeat every five minutes; a renewal updates only a still-reserved row owned by that process. Startup recovery, periodic cleanup, and cleanup during new quota reservations all use SQLite `BEGIN IMMEDIATE` and release only rows with no heartbeat for more than two hours. Existing databases are migrated with nullable fields; a legacy row without a heartbeat ages from `reserved_at`. An uncertain send stops renewing when the task ends, remains charged as pending, and is recovered two hours after its last heartbeat if it is not otherwise resolved.

PDF generation has an independent `PDF_CONVERSION_CONCURRENCY` semaphore, defaulting to two conversions per event loop. Conversion work does not consume or hold an outbound send slot while rendering, and send pacing does not act as a conversion-worker limit.

The following behavior remains unchanged: private-only collection, at most 15 messages of up to 4096 characters each (61,440 message characters before separators/title), and an editable title prompt after message 15 or five seconds of inactivity. Drafts expire after 30 minutes. The default daily quota is 3 conversions per user and resets at 02:00 UTC. Reservations heartbeat every five minutes and are recovered only after more than two hours without a heartbeat. PDF conversion concurrency defaults to 2.

## Operator settings

- `BOT_GLOBAL_SENDS_PER_SECOND`: default 20; accepted range 1–25.
- `BOT_PRIVATE_CHAT_INTERVAL_SECONDS`: default 1 second; values below 1 are rejected in favor of the default.
- `BOT_GROUP_CHAT_INTERVAL_SECONDS`: default 3.05 seconds; values below 3 seconds are rejected in favor of the default.
- `PDF_CONVERSION_CONCURRENCY`: positive integer, default 2.

Invalid or out-of-range pacing values fall back to their defaults. The corresponding safe examples are in `.env.example`.

## Verification and limitations

The full offline suite passed: **43 tests** (`python3 -m unittest discover -s tests -v`) on Python 3.12.3 / Ubuntu 24.04.4 LTS. The verbose output distribution was `test_delivery.py`: 10, `test_limits.py`: 12, `test_main.py`: 8, `test_outbound.py`: 11, and `test_pdf.py`: 2. Deterministic mocked coverage checks FIFO ordering, private-chat and bot-wide pacing, the group-chat floor, the shared message/document path, 429 delay and queue-wide cooldown, bounded repeated 429 handling, safe connection retry backoff, no replay after an ambiguous document timeout, quota retention for uncertain delivery, active lease renewal beyond two hours during concurrent cleanup, stale abandoned recovery, schema migration, cancellation before versus during document sending, SQLite `0700`/`0600` permissions, and rejection of a DB_PATH under `/tmp` without changing its mode or sticky bit. `python3 -m compileall -q .` and `python3 -m pip check` passed. Clocks and sleeps are controlled in tests; no real token was used, the bot was not started, and no live Telegram endpoint was contacted.

The outbound limiter and cooldown are in-memory and coordinate only one bot process; run exactly one process per token. Quota lease renewal and cleanup are serialized only for writers using the same local SQLite database; separate database copies are not coordinated, and this report does not claim distribution-wide safety. Drafts are in memory and disappear on restart. A process/event-loop pause longer than two hours during an already-started Telegram upload can let that external request outlive its lease; Telegram delivery and SQLite commit are not atomic, so that residual late-delivery race cannot be eliminated here. Normal `finally` cleanup removes temporary PDFs, but a forced process kill may leave one behind. A request already past the pacing gate when a 429 response arrives cannot be recalled; the new cooldown delays subsequent queued and future sends. This suite is offline; no two-user end-to-end run, live Telegram interaction, or hosted deployment / target Arabic-font PDF check was performed. Telegram may apply additional or changing flood rules, so operators should treat this as conservative risk reduction—not absolute immunity from 429.
