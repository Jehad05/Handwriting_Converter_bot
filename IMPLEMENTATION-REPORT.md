# Implementation report

The updated bot is implemented in this separate project tree. It keeps the A3 PDF workflow and the existing `create_pdf(text, output_path)` interface, while correcting the session, quota, path, and cleanup issues identified during review.

## Implemented behavior

The bot processes messages only in private chats. It explicitly rejects group or channel updates before any command or text handler can collect content, and sessions are keyed by `(chat_id, user_id)`. Drafts collect up to 15 text messages; the fifteenth prompts for an editable title, with a dated `دفتر رسائلي — [تاريخ الإنشاء]` example. `/done`, `/skip`, and `/cancel` remain available. Drafts expire after 30 minutes of inactivity and a periodic application-managed job sweeps idle sessions.

There is no custom per-message cap below Telegram’s 4096-character limit and no aggregate cap or truncation. The full 15-message payload of 61,440 characters is accepted. Emoji are removed with narrowly enumerated code-point ranges before PDF rendering, consistent with the original behavior and the available fonts; Arabic presentation forms, Arabic diacritics, accented Latin marks, and join controls are not part of those removal ranges. The original `utils.py` was retained unchanged and verified byte-identical to the uploaded file.

Quota reservations use SQLite `BEGIN IMMEDIATE`, count active reservations against the limit, and apply a fixed daily window starting at 02:00 UTC; the default is 3 conversions per user per window. The schema persists `heartbeat_at` and a random process lease owner; active conversion and Telegram delivery renew the heartbeat every five minutes, and renewal succeeds only for the owning process and a still-pending row. Startup cleanup, periodic cleanup, and quota-admission cleanup release a reservation only when its last heartbeat (or legacy `reserved_at`) is more than two hours old. The schema migration adds these nullable fields to existing databases, so older pending reservations remain recoverable using their original `reserved_at`. Conversion and known pre-delivery failures refund; cancellation before `send_document` starts also refunds, while cancellation during upload retains the reservation because delivery may be ambiguous. After confirmed delivery, bookkeeping is attempted up to three times; progress-message deletion, remaining-quota notices, and PDF cleanup cannot trigger a refund. PDF paths are unique, and the normal `finally` path removes the temporary file after success or failure; a forced process kill can skip cleanup and leave a temporary file. Logs contain error types and short references, not message or title text. Relative database, data, temporary-file, and font paths resolve from the project directory. A missing `BOT_TOKEN` exits with status 1 before application startup.

The font downloader is located under `fonts/`, writes atomically to the same directory used by the converter, restricts downloads to its fixed font manifest, and validates font-file headers. Documentation and `.gitignore` reflect the optional fonts and their separate licensing.

SQLite data permissions are enforced without chmodding a shared parent: only the user-owned app `DATA_DIR` is made `0700`; an external custom `DB_PATH` is accepted only under an already-existing user-owned `0700` directory. Shared paths such as `/tmp` are rejected before opening/creating the database, without changing directory permissions or the sticky bit. The database and its WAL/SHM/journal files are restricted to `0600`.

## Offline verification

`python3 -m unittest discover -s tests -v` passed **43 tests** on Python 3.12.3 / Ubuntu 24.04.4 LTS (1.292 seconds). The verbose output distribution was `test_delivery.py`: 10, `test_limits.py`: 12, `test_main.py`: 8, `test_outbound.py`: 11, and `test_pdf.py`: 2. In addition to message/session, quota, delivery, and PDF cases, deterministic lifecycle regressions exercise delivery beyond the original two-hour age while concurrent cleaners run, active heartbeat renewal, one-time concurrent recovery of an abandoned lease, migration and recovery of a pending row from the previous schema, cancellation before versus during document sending, private file/directory modes, and rejection of `/tmp` without changing its mode or sticky bit. The delivery regression uses a fake clock and mocked local bot; it confirms quota remains reserved while delivery is held open and is committed after the mock send returns.

A concise record of the test run, static checks, and benchmark summary is saved in `TEST-RESULTS.txt`.

`python3 -m compileall -q .` and `python3 -m pip check` also passed. Project-relative paths were checked from `/tmp`. No bot process was started, no real token was used, no live Telegram endpoint was contacted, and no hosting deployment was tested. A two-user end-to-end run and validation of Arabic-font availability and PDF rendering on the target host remain deployment gates. No repository was published or uploaded.

## Converter benchmark

The benchmark used 15 realistic Arabic, English, mixed-script, and emoji-containing messages. Raw payload totals match each target exactly; the bot’s local emoji sanitation is applied before PDF conversion. The converter input count therefore differs slightly from the raw total, and includes the 28 newline-separator characters between messages.

| Raw payload characters | Converter input characters | Time | PDF bytes | Pages |
|---:|---:|---:|---:|---:|
| 8,000 | 7,986 | 0.3062 s | 44,009 | 4 |
| 16,000 | 15,939 | 0.6253 s | 50,159 | 7 |
| 30,000 | 29,872 | 1.1252 s | 58,859 | 12 |
| 45,000 | 44,790 | 1.6210 s | 68,477 | 17 |
| 61,440 | 61,141 | 2.2682 s | 78,125 | 23 |

The single-run 61,440-message-character record in `benchmark-results.json` completed without a layout failure, producing a 23-page, 78,125-byte PDF in **2.2682 seconds** on this computer (converter input: 61,141 characters after emoji sanitation and separators). This is a local converter measurement, not a hosting or production-performance guarantee. **The measured result supports keeping the full theoretical limit; no smaller aggregate cap is added.** The 45,000-character run initially exposed that long Arabic paragraphs could not split across pages. `ArabicFlowable.split()` was added and verified by a dedicated multi-page Arabic test and by every larger benchmark size.

Detailed measurements are in `benchmark-results.json`; rerun them with `python benchmarks/benchmark_converter.py --output benchmark-results.json`. The script uses local conversion only, does not instantiate the bot application, and does not contact Telegram.

## Known delivery-boundary limitation

Telegram delivery and a local SQLite commit cannot participate in one atomic transaction. Drafts are in process memory and are lost on restart; the limiter is process-local, while quota reservations coordinate only through the same local SQLite database. Run exactly one process per token. A crashed process stops renewing its lease, and abandoned reservations are recovered only after more than two hours without a heartbeat. SQLite `BEGIN IMMEDIATE` operations serialize renewal and stale deletion for writers using the same database, but do not coordinate separate database copies or establish distribution-wide safety. If the process/event loop is paused longer than two hours while a `send_document` request is already in flight, the lease can expire before that external request resumes; a successful late Telegram delivery cannot be made atomic with, or recalled by, SQLite. The immediate owner-checked renewal before starting a document send fences work that has already lost its reservation, but this pause-during-upload race remains a limitation. Confirmed sends are committed without refund, with up to three bookkeeping attempts. Normal `finally` cleanup removes temporary PDFs, but a forced process kill can leave one behind.
