# Codebase Memory File

This file documents the structure and key facts about `emailexporter4mail.com` so AI agents can quickly orient themselves without re-reading the full codebase.

## Overview

`export_emails.py` — main script that exports emails from mail.com via a Playwright-based daemon (`openbrowser-ai`). Saves each email as `.eml` + `.html` + attachments. Resumable via `export_progress.json`.

## Key Architecture

### Output layout (per run)
```
OPEXPORT_ROOT (~/Downloads/openbrowser-daemon)/
  <RUN_NAME>/
    eml/          # .eml files
    html/         # .html files
    attachments/  # <prefix>/ subdirs with attachment files + .zip
    drafts/       # same structure for Drafts folder emails
      eml/
      html/
      attachments/
    export_progress.json   # resume state (incl. bogus_ids / fail_counts for retry-pending)
    email_index.txt        # newest-first index (generated at end)
    perf_log.jsonl          # structured performance log (PerfLog)
    diagnostics/           # written for each retry-pending email
      <eid>.json           # failure reason + per-step flags + counts
      <eid>.ax.txt         # trimmed detail-view accessibility-tree snapshot
```

### Daemon
- Uses `openbrowser-ai` (Playwright wrapper) — daemon process started via `uvx openbrowser-ai daemon start`
- Downloads land in **`~/Downloads/openbrowser-daemon`** (STAGE_DIR), then relocated to per-run folder
- `OPENBROWSER_HEADLESS` env var controls headless mode (default: visible for interactive login)
- RTT (round-trip time) to daemon is consistently ~1.5s per `run()` call

### Core functions

| Function/Class | Line | Purpose |
|---|---|---|
| `main()` | 2703 | Entry point. orchestration: prepare dir, kill daemons, start/navigate, login, search, iterate pages, cleanup |
| `run(code)` | 317 | Execute JS/Python on daemon via `execute_code_via_daemon()`, with retry + session-loss detection. **Instruments every RTT via PerfLog.** |
| `_wait_for_state(condition)` | 359 | Poll browser state until condition is met (label, backoff, timeout) |
| `step_do_search()` | 747 | Find search input, type SEARCH_TERM, press Enter (fallback: options panel) |
| `process_page(page)` | 2567 | Collect all list rows on current page, export each email |
| `export_rows(rows)` | 2490 | Open each email by its eid, call `export_single_email()`, track progress. Returns `(exported_new, given_up_count)`. |
| `export_single_email()` | 1896 | For the currently-open detail view: save .html + .eml + attachments. Returns `(saved_html, saved_eml, name_prefix, att_ok, next_btn_idx, diag)`. |
| `_poll_and_scan()` | 1790 | One daemon call: poll for detail view, extract date/subject/ACT index/next_btn/attachment targets |
| `_click_both_saves(act_index, delay)` | 1015 | Click "Show further actions" → "Save (.html)" → "Save (.eml)" in one daemon call |
| `_download_ui_attachments(prefix)` | 1149 | Download attachments via ZIP button (multi) or per-attachment button (single) |
| `_collect_all_rows()` | 2388 | Return all `(index, eid, folder)` for the current page (list-only enumeration) |
| `goto_page(page)` | 852 | Navigate to page N via page-input jump (fallback: Next-page clicks) |
| `_ensure_list_only()` | 2229 | Make sure list pane is visible (not a detail view) |
| `_open_by_eid(eid)` | 2099 | Open email by its stable list-row eid |
| `_already_logged_in()` | 655 | Quick check if browser session is still authenticated |
| `_install_resource_blocking()` | 483 | Block image/media/font via Playwright route interception |
| `_find_download_targets()` | 1057 | Scan detail view for attachment button indices |
| `_download_one(idx, dest, snapshot)` | 1099 | Download a single attachment file (polling + file detection) |
| `find_disk_partials()` | 2637 | Return prefixes present in only one of eml/ or html/ |
| `reconcile_duplicates()` | 1602 | Remove Message-ID duplicate .eml files |
| `reconcile_names()` | 1649 | Re-align artifact names to match .eml base |
| `migrate_date_format()` | 1515 | Rename legacy date format to compact yymmdd |
| `cleanup_strays()` | 2649 | Remove/move un-filed files in PROGRESS_DIR |
| `load_progress()` | 425 | Parse export_progress.json with defaults |
| `save_progress(p)` | 434 | Atomic write to export_progress.json |
| `PerfLog` (class) | 55 | Performance logging: records RTTs + phase timings to JSONL |
| `_perf_phase(name, **kw)` | 142 | Context manager factory: wraps a code block, times it, counts RTTs inside |
| `_PerfPhaseCtx` (class) | 158 | Context manager object yielded by `_perf_phase()`. `ok` attribute signals success/failure. |
| `run_interactive.py` | (separate) | Interactive TUI launcher: questionary-based guided config with preset save/load, review screen, then spawns `export_emails.py`. Mode section collects `--from`/`--count` and `build_env_and_args()` appends them to the spawned command (review screen shows them too). `--to` is CLI-only. |

### Module-level state

| Variable | Line | Purpose |
|---|---|---|
| `_SESSION_LOST` | 49 | Set True by `run()` when daemon connection is unrecoverably broken |
| `_CONSEC_FAILS` | 50 | Incremented by `export_rows`, reset per page attempt |
| `_ON_EMAIL_PAGE` | 51 | Set False by `_poll_and_scan` when AX tree has no email content |
| `_SAVE_DELAY` | 52 | Inter-save gap (html→eml); adaptively learned across emails, bumped on failure |
| `_BOGUS_IDS` | 53 | **Within-run guard**: eids that failed `BOGUS_THRESHOLD` times THIS run; skipped for the rest of the run (so one dead email can't abort it). Reset to empty at the start of every run (loaded as empty in `main()`). |
| `_PENDING` | 56 | **Retry-pending marker**: eids queued for retry on the NEXT run (the "blacklist" the user sees). Persisted in `export_progress.json["bogus_ids"]`; loaded at run start; cleared on a successful export. A normal run re-attempts these (they are NOT subtracted from `skip_ids`). |
| `_FAIL_COUNTS` | 54 | Per-eid consecutive failure streak (across runs); drives promotion into `_BOGUS_IDS`+`_PENDING`; reset on success; persisted as `fail_counts`. |
| `BOGUS_THRESHOLD` | 55 | Fails before an eid becomes retry-pending (env `OPEXPORT_BOGUS_THRESHOLD`, default 3). |
| `_RTT_FAILS` | 57 | Count of `run()` calls that returned `ok=False` this run (connection-health signal, printed in the end-of-run completeness report). |
| `_perf_log` | 140 | `PerfLog` instance, initialized in `main()` after `prepare_run_dir()` |
| `_RUN_RTT_BUDGET` | 313 | Cumulative RTT time within current `_perf_phase` scope (None when no phase active) |
| `_RUN_RTT_COUNT_BUDGET` | 314 | RTT count within current `_perf_phase` scope (None when no phase active) |

### PerfLog — Performance Logging System

Located at `export_emails.py:55`. Writes JSONL to `<run_dir>/perf_log.jsonl`.

**What it records:**
1. **Daemon RTTs** — every `run()` call timed via `_perf_log.rtt(dur, ok, out_len)`. Persistent ~1.5s each.
2. **Phase timings** — named phases with duration, RTT count, RTT cumulative time, and success flag.

**Phase events (JSONL keys):**
- `email.open` / `email.extract` / `email.save` / `email.attach` / `email.back` / `email.total`
- `page.navigate` / `page.collect` / `page.total`
- `session.setup` / `session.search` / `session.recovery` / `session.summary`
- `rtt` — every daemon round-trip

**RTT tracking via budget globals:** `_RUN_RTT_BUDGET` and `_RUN_RTT_COUNT_BUDGET` are set to `0` on phase entry, incremented by each `run()` call within the phase, restored to previous values on phase exit. This allows nested phases to correctly attribute RTTs to the innermost active phase while accumulating totals upward.

**Current status:**
- ✅ `PerfLog` class — implemented
- ✅ `run()` RTT timing — implemented
- ✅ `_perf_phase()` context manager — implemented
- ✅ Phase instrumentation in `export_rows` — email.open, email.back, email.total
- ✅ Phase instrumentation in `export_single_email` — email.extract, email.save, email.attach
- ✅ Phase instrumentation in `process_page` / `goto_page` — page.collect, page.total, page.navigate
- ✅ Session events in `main()` — session.setup, session.search, session.recovery, session.summary
- ✅ Summary at end of `main()` — `_perf_log.summary()` called with final counts
- 🔲 Broader session-loss detection — widen `_SESSION_LOST` triggers

### _SAVE_DELAY — Adaptive inter-save delay

Module-level float (`export_emails.py:52`). Controls the gap between clicking "Save (.html)" and "Save (.eml)" in `_click_both_saves()`.

**Behavior:**
- Starts at `0.1s` per process.
- Each email uses `_SAVE_DELAY` as the initial gap.
- If `.eml` file is missing after the wait, delay bumps `+0.1s` (cap `1.0s`), retries up to 3 times.
- On success (both `.html` and `.eml` saved): `_SAVE_DELAY = _delay` (records the working delay).
- On partial (`.html` saved, `.eml` not): `_SAVE_DELAY` also bumps `+0.1s` so next email starts higher.
- On total failure: `_SAVE_DELAY` bumps `+0.1s`.

This means the delay converges upward to the minimum reliable value for the current mailbox / network conditions, and stays there for subsequent emails.

### Progress tracking (`export_progress.json`)
```json
{
  "exported": 0,          // count of newly exported this run
  "last_page": 1,         // last page processed
  "skip_on_page": 0,      // resume offset within page
  "search_done": false,   // whether search was executed
  "max_pages": 0,         // total pages from search
  "exported_ids": [],     // ids with both .eml + .html saved
  "seen_ids": [],         // every id encountered
  "attachment_ids": [],   // ids with attachments successfully obtained
  "exported_map": {},     // eid -> filename prefix
  "partials": [],          // filename prefixes missing one format
   "bogus_ids": [],        // RETRY-PENDING ids (auto-populated once an eid fails BOGUS_THRESHOLD times). A normal run re-attempts these.
   "fail_counts": {},       // eid -> consecutive failure count (drives promotion into retry-pending)
   "save_delay": 0.0       // legacy field (unused; _SAVE_DELAY replaces it in-memory)
}
```

### Config env vars (all `OPEXPORT_*`)
See README §2 for full table. Key ones:
- `OPEXPORT_ROOT`, `OPEXPORT_RUN` — output paths
- `OPEXPORT_SEARCH_TERM`, `_FIELD`, `_FOLDER` — search criteria
- `OPEXPORT_LOCAL_ACCOUNT` — logged-in address
- `OPEXPORT_HEADLESS` / `OPEXPORT_DEBUG` — session mode
- `OPEXPORT_WAIT` (default 10), `OPEXPORT_SLEEP` (0.3), `OPEXPORT_OPEN_POLL` (0.3) — timing
- `OPEXPORT_PAGE_SIZE` (50), `OPEXPORT_TOTAL` (0) — pagination
- `OPEXPORT_BLOCK_RESOURCES` (image,media,font)
- `OPEXPORT_LOGIN_TIMEOUT` (0 = forever)
- `OPEXPORT_MAX_FAILURES` (5)

### CLI flags
- `--fresh` — archive existing, start from scratch
- `--new` — export only emails never seen
- `--from PAGE.INDEX` — start at specific email
- `--count N` — export N emails from start (alias for --limit)
- `--to PAGE.INDEX` — export through specific email (requires --from)
- `--limit N` — stop after N new exports
- `--select PAGE:COUNT,...` — targeted extraction
- `--keepdaemon` — don't kill stale daemons on start
- `--dry-run` — (run_interactive.py only) print command without executing

### Key concepts
- **eid**: stable email identifier (`id=id<epoch-nanoseconds>` in list rows). Used as the primary resume key.
- **list-only mode**: compact view that renders all page rows in one accessibility tree snapshot (no virtualization).
- **ACT**: "Show further actions" button that opens the save dropdown.
- **Session recovery**: when `_SESSION_LOST` is True, the main loop restarts daemon, re-logs in, re-searches, and navigates back to the target page.
- **SKIP logic**: `skip_ids = (exported_ids & attachment_ids) - _BOGUS_IDS`. Only fully-done emails (both formats + attachments) are skipped. Retry-pending ids (`_PENDING`) are intentionally NOT subtracted — a normal run re-attempts them.
- **RTT**: daemon round-trip time, consistently ~1.5s per `run()` call. The key optimization lever is **counting** RTTs, not timing them.
- **Retry-pending / "blacklist"**: an eid that fails `BOGUS_THRESHOLD` (default 3, env `OPEXPORT_BOGUS_THRESHOLD`) times is added to `_PENDING` (persisted as `bogus_ids`) AND to the within-run guard `_BOGUS_IDS`. The next run re-attempts it; on success it's cleared from both. Inspect/debug with `unblacklist.py`.
- **Diagnostics**: when an eid becomes retry-pending, `<run>/diagnostics/<eid>.json` (reason + counts) and `<eid>.ax.txt` (detail-view AX snapshot) are written so you can see *why* it failed (wrong zoom, dead daemon, etc.).
- **Completeness banner**: at the end of a run, if any partials / retry-pending / unseen emails remain, `export_emails.py` prints `⚠ BACKUP INCOMPLETE` plus a re-run command, connection-health (`_RTT_FAILS`) + live daemon ping, and zoom-out advice. In a direct TTY it offers `Re-run now? [y/N]` (suppressed under `run_interactive.py` via `OPEXPORT_SUBPROCESS=1`).

### Per-email RTT count breakdown (typical)
| Phase | run() calls | Notes |
|---|---|---|
| open | 4–8 | 1× ensure_list_only + 1× visible_index_of + 1× click + 1–5× poll |
| extract | 1 | `_poll_and_scan` — **bundled** |
| save | 1–3 | `_click_both_saves` — bundled (ACT + both saves); retries if .eml misses |
| attach | 1 + N | 1× find_targets + N× download_one |
| back | 1 | back_to_list or navigate_next |
| **total** | **8–14** | × 1.5s = 12–21s daemon IPC per email |

### Known dead code / stale references
- `_click_save()` no longer exists in `export_emails.py` (refactored into `_click_both_saves`). Broken in: `probe_html2.py:28`, `probe_html_menu.py:36`, `explore_html.py:47` (these probe scripts are archived locally in `scratch/`, not published).
- `check_multi.py` references `E._detect_multiple_recipients()` which no longer exists; it is archived in `scratch/` for reference (the live multiple-recipient → `'multiple'` naming lives in `_party_from_page` / `_party_from_eml`).
- `OPEXPORT_LISTONLY_MIN` was listed in older README but never existed in code — removed.
- `save_delay` in `export_progress.json` initialized to `0.0` but never read or used by code (replaced by in-memory `_SAVE_DELAY` in Jul 2026).
- Maintenance / debug / probe scripts (`clean_failed_runs.py`, `reconcile.py`, `migrate_dates.py`, `build_index.py`, `run.sh`, `run_perf.sh`, `debug_*.py`, `probe_*.py`, `explore*.py`, …) are **not published** to GitHub; they live in the local `scratch/` directory.

### README fixes applied (Jul 2026)
- §1 — Added CLI flags table, quick start (TUI), `--dry-run` flag
- §2 — Fixed `OPEXPORT_WAIT` default 30→10, removed `OPEXPORT_LISTONLY_MIN`, added missing env vars
- §3.4 — Removed "dedicated Drafts folder pass" language (drafts handled inline)
- §4.3 — Added PerfLog phase documentation
- §4.4 — Added adaptive save delay (`_SAVE_DELAY`) documentation
- §5 — Added 6 new bug-fix entries (save delay, perf log, etc.)
- §6 — Expanded file table to cover all 40+ files, added `run_interactive.py` and `requirements.txt`

### Planned improvements
1. **Broader session-loss detection** — widen `_SESSION_LOST` triggers (empty state, critical op failures). 🔲 NOT done (only 2 triggers in `run()`).
2. **Pre-page health check** — fast keepalive before each `goto_page()`. 🔲 NOT done.
3. **Adaptive download polling** — exponential backoff in `_download_one` (0.3s → 0.6s → 1.2s → cap 3s). 🔲 NOT done (fixed `asyncio.sleep(1.0)` at `export_emails.py:1124`).
4. **Retry-pending (was "blacklist") population** — ✅ DONE. Per-eid streak (`_FAIL_COUNTS`) drives promotion into the within-run guard `_BOGUS_IDS` AND the retry-pending marker `_PENDING` (persisted as `bogus_ids`). On `BOGUS_THRESHOLD` (default 3, env `OPEXPORT_BOGUS_THRESHOLD`) failures an eid is held for retry (NOT permanently excluded); a normal run re-attempts it, clears it on success, and diagnostics are written. Cleared on `--fresh`. End-of-run summary reports the retry-pending count.
5. **End-of-run completeness summary** — ✅ DONE (most of it). `session.summary` prints an `⚠ BACKUP INCOMPLETE` banner when partials / retry-pending / unseen emails remain, a re-run command, connection-health (`_RTT_FAILS` + live daemon ping), zoom-out advice, and a `Re-run now? [y/N]` prompt in a direct TTY (suppressed under `run_interactive.py` via `OPEXPORT_SUBPROCESS=1`). No LIMIT-vs-actual or draft-count breakdown yet.
6. **`unblacklist.py`** — ✅ DONE. Inspect (`--list`), clear (`--remove`/`--remove-all`), and re-run (`--rerun`) retry-pending emails from `export_progress.json`.
