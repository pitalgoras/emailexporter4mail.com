# mail.com email exporter

Export every email matching a search term from a mail.com mailbox as `.eml` +
`.html` (+ attachments), with reliable list-only enumeration, Drafts → `drafts/`
routing, and open-by-`eid` traversal.

**Nothing is hardwired.** Every behaviour is configurable through `OPEXPORT_*`
environment variables so different users can run different exports unchanged.

**Prerequisites:** `uv` (Python 3.11+ and all dependencies are managed automatically).
The `openbrowser-ai` daemon is launched by the script via `uvx openbrowser-ai` (part of
`uv`), so there is no manual install step.

```bash
uv run export_emails.py          # or:  uv run run_interactive.py
```

`uv` reads `pyproject.toml`, creates a managed environment, and installs `openbrowser-ai`,
`playwright`, and `questionary` on first run. You never invoke `python3` directly — `uv`
always provides the correct interpreter (the one that has the `openbrowser` package), which
avoids the "wrong interpreter / missing `openbrowser`" failure mode.

---

## 1. How to run

### Quick start (interactive TUI)

```bash
uv run run_interactive.py
```

Opens a guided terminal UI that walks through all settings (search term, destination
folder, mode flags, tuning) with defaults pre-filled, preset save/load, and a
configuration review screen before launching.

**Personal credentials:** `OPEXPORT_SEARCH_TERM` (what to search for — *them*) and
`OPEXPORT_LOCAL_ACCOUNT` (your logged-in address — *you*) are personal. If unset, the
tool prompts for them once and saves them to `~/.config/emailexporter4mail.com/settings.json`
(outside the repo, `chmod 600`) so subsequent runs auto-fill. Override anytime via the
environment variables.

### Direct (command line)

```bash
# Full export (all pages + Drafts folder) into a fresh per-run folder
OPEXPORT_RUN=run1 uv run export_emails.py

# Only a few emails, for testing (PAGE:COUNT syntax; COUNT>0 = first N, <0 = last N)
OPEXPORT_RUN=sel1 uv run export_emails.py --select 1:-3,2:5,last:-1
#   page 1, last 3 emails (oldest) · page 2, first 5 (newest) · last page, last 1 (oldest)
```

The browser is **visible by default** — mail.com login is interactive, so the daemon
opens a real window for you to log in. `OPEXPORT_HEADLESS=true` is only for hands-off
runs where a saved login / `storage_state` already exists (otherwise there is no window
to log into and the run will wait forever).

| Flag | Purpose |
|---|---|
| `--fresh` | Archive existing files in the run folder, start from scratch (resets all progress) |
| `--new` | Export only emails never seen in any previous run (re-scans all pages, skips fully-done ids) |
| `--limit N` | Stop after N newly exported emails (counts only first-time exports) |
| `--select PAGE:COUNT,...` | Targeted extraction. `COUNT>0` = newest N on page, `COUNT<0` = oldest N. `last` as page = final page. Skips non-listed pages entirely. |
| `--from PAGE.INDEX` | Start at a specific email (e.g. `--from 1.25` = page 1, email #25). Sets start page and skip count. |
| `--count N` | Export N emails from the start position. Alias for `--limit`. Use with `--from` for a contiguous range. |
| `--to PAGE.INDEX` | Export through a specific email (e.g. `--from 1.25 --to 2.1`). Computes count from start to end (inclusive). Requires `--from`. |
| `--limit N` | Stop after N newly exported emails (counts only first-time exports) |
| `--select PAGE:COUNT,...` | Targeted extraction. `COUNT>0` = newest N on page, `COUNT<0` = oldest N. `last` as page = final page. Skips non-listed pages entirely. |
| `--keepdaemon` | Skip killing stale daemon processes on start (preserves a logged-in session for faster re-runs during development) |
| `--dry-run` | (run_interactive.py only) Print the generated command and environment without executing |

The **browser must be logged in to mail.com**. The daemon runs visibly while
debugging (`OPEXPORT_HEADLESS=false`, the default) so you can log in interactively;
`step_wait_login()` blocks until it detects the mailbox. Once logged in, the run
proceeds automatically.

**Daemon lifecycle:** The script kills any stale `openbrowser-ai` daemon processes
at startup so a crashed previous session doesn't leave a half-broken daemon behind.
Use `--keepdaemon` to skip the kill when you want to preserve a logged-in session
for back-to-back test re-runs (the script reuses the existing daemon if it responds
to a ping).

**Folder conflict resolution:** When the run folder already has exported emails and
no `--fresh`, `--select`, `--new`, or `--from` flag is given, the script prompts interactively:
`c` to continue/resume, `f` for a fresh start (archives old files, resets state),
`n` to exit and re-run with a different `OPEXPORT_RUN`, or `a` to abort. This
prevents accidentally resuming when you expected a clean run.

---

## 2. Configuration (`OPEXPORT_*` environment variables)

| Variable | Default | Purpose |
|---|---|---|---|
| `OPEXPORT_ROOT` | `~/Downloads/openbrowser-daemon` | Base dir that holds **one sub-folder per run**. |
| `OPEXPORT_RUN` | *(timestamped)* | Run folder name. A stable name = resume/reuse the same folder; omitting it creates a unique timestamped folder so runs never collide. |
| `OPEXPORT_SEARCH_TERM` | *(prompted / env)* | Text/address/domain to search for. If unset, the tool prompts once and saves it locally. |
| `OPEXPORT_SEARCH_FIELD` | `All headers` | Where to match (`All headers` / `Sender` / `Recipient` / `Subject`). |
| `OPEXPORT_SEARCH_FOLDER` | `All folders` | Folder scope (`All folders` or a specific folder). |
| `OPEXPORT_LOCAL_ACCOUNT` | *(prompted / env)* | Logged-in address; decides SENT vs RECEIVED and the filename party. If unset, the tool prompts once and saves it locally. |
| `OPEXPORT_HEADLESS` | `false` | `true` = hands-off production run (needs a saved login / storage_state). `false` (or `OPEXPORT_DEBUG=true`) = visible browser for interactive login. |
| `OPEXPORT_DEBUG` | `true` | Forces a visible browser regardless of `OPEXPORT_HEADLESS`. |
| `OPEXPORT_WAIT` | `10` | Max seconds to wait for a Save click to produce a download (safety net; returns early once the file is complete). |
| `OPEXPORT_SLEEP` | `0.3` | Fixed settle sleep between actions (`SETTLE_SLEEP`). |
| `OPEXPORT_OPEN_POLL` | `0.3` | Poll interval while waiting for an email's detail view to open. |
| `OPEXPORT_BLOCK_RESOURCES` | `image,media,font` | Resource types blocked at the browser level to speed up page loads (incl. per-email detail views). Set to `none` to disable, or add `stylesheet` to also block CSS (see caveat below). |
| `OPEXPORT_PAGE_SIZE` | `50` | Expected number of list rows per non-last page. Used with `OPEXPORT_TOTAL` for row-count enforcement. |
| `OPEXPORT_TOTAL` | `0` | Total search result count. When > 0, the script enforces every non-last page has `OPEXPORT_PAGE_SIZE` items (by zooming/scrolling/repolling). Omit or 0 to skip enforcement. |
| `OPEXPORT_LOGIN_TIMEOUT` | `0` (wait forever) | Max seconds to wait for interactive login. Set to a positive value to fail fast in automated contexts. |
| `OPEXPORT_POLLS` | *(per-call default)* | Override the number of poll iterations in `_wait_for_state` (for tuning connection-speed). |
| `OPEXPORT_POLL_INTERVAL` | *(per-call default)* | Override the poll interval in `_wait_for_state` (seconds). |
| `OPEXPORT_MAX_FAILURES` | `5` | Consecutive partial/fail exports before the run aborts. |

---

## 3. Architecture

### 3.1 Per-run output folders (single source of truth)
- `BASE_DIR = OPEXPORT_ROOT`
- `RUN_DIR  = BASE_DIR / OPEXPORT_RUN`  (or a timestamped name)
- `PROGRESS_DIR = RUN_DIR` — **all** output dirs derive from this one value:
  `EML_DIR`, `HTML_DIR`, `ATTACH_DIR`, `DRAFTS_*`, `PROGRESS_FILE`.
- Each run is fully isolated. There is **no cross-run `exported_ids` pollution**
  (an earlier bug where run 8 silently skipped emails that run 7 had already
  exported into the shared folder).

### 3.2 Staging vs output
The openbrowser daemon **hardcodes** its download directory to
`~/Downloads/openbrowser-daemon`. That is treated as a transient **`STAGE_DIR`**:
downloads are detected there (`_dir_snapshot`, `_wait_for_new_file`,
`_download_one`, `_try_save`) and then relocated into the per-run `RUN_DIR`.
So `PROGRESS_DIR` (output) and `STAGE_DIR` (browser download) are intentionally
distinct.

### 3.3 Run lifecycle / state
- `prepare_run_dir()` creates the run folder and writes status `running` to
  `OPEXPORT_ROOT/.opexport_runs.json`.
- On clean completion, `main()` writes status `completed`.
- A run that crashes keeps status `running` (treated as not-completed).
- **Failed/partial runs are NEVER auto-deleted** — they stay on disk for
  inspection. Emptying them is a separate, on-demand maintenance script
  (`clean_failed_runs.py`, kept locally in `scratch/` — not published to GitHub):

```bash
python3 scratch/clean_failed_runs.py            # dry-run: list non-completed runs
python3 scratch/clean_failed_runs.py --apply    # empty their folders (status -> cleared)
```

### 3.4 Drafts routing
Drafts appear in search results tagged `div title="Drafts"` (the per-row folder
tag; the sidebar's `title=Drafts` carries an `N/M` count and is excluded). They
are exported into `RUN_DIR/drafts/{eml,html,attachments}`. A dedicated Drafts
folder pass also runs (unless `--select`) to guarantee capture.

---

## 4. Performance: per-email optimisation

"Page" optimisations (the 4 list pages / 50-email batches, enumeration,
pagination) are **not** worth chasing. The work that matters is **per email**.

Per email, time is spent in:

| # | Location | Sleep | Env-driven? | Notes |
|---|----------|-------|-------------|-------|
| A | `_wait_for_new_file` | 0.3s then 0.2s | ❌ hardcoded | Confirms a downloaded file's size is stable. Hit 2×/email (.html+.eml) + per attachment. |
| B | `_download_one` | 1.0s per poll | ❌ hardcoded | Attachment download wait; dominant for multi-attachment emails. |
| C | `SETTLE_SLEEP` | 0.3s | ✅ `OPEXPORT_SLEEP` | After every click / Escape / open step. |
| D | `OPEN_POLL_SLEEP` | 0.3s | ✅ `OPEXPORT_OPEN_POLL` | Waiting for the detail view to open. |
| E | `await run(...)` round-trips | ~0.1–0.3s each | n/a (IPC) | **Dozens per email** (open-by-eid, open, attachments, save clicks, state reads). The hidden dominant cost. |

**Lever 1 (low risk):** make A and B env-driven with smaller defaults, and lower
the `C`/`D` defaults. Already partially tunable via `OPEXPORT_SLEEP` /
`OPEXPORT_OPEN_POLL` / `OPEXPORT_WAIT`.

**Lever 2 (higher gain):** **batch `run()` calls** — combine a state-read + click
+ read into a single `execute_code_via_daemon` snippet. This attacks E, the real
per-email time sink, not a sleep number.

### 4.1 Resource blocking (images / CSS)
`OPEXPORT_BLOCK_RESOURCES` installs a **Playwright `context.route(...)`** handler
once per session (after login) that **aborts** `image` / `media` / `font` (and
optionally `stylesheet`) requests. It is independent of headless and covers
**every** navigation, including each per-email detail view, so it speeds per-email
opens. Documents / scripts / XHR / fetch are never blocked, so the DOM-based
accessibility tree and the `.eml`/`.html` downloads keep working.

**Caveat — do NOT block CSS on mail.com.** The default blocks only
`image,media,font`. Adding `stylesheet` aborts CSS, and mail.com's pagination /
list controls are CSS-revealed: with CSS blocked they vanish from the
accessibility tree, so `goto_page` cannot find the Next-page control (navigation
fails) and `_collect_all_rows` picks up non-email rows (e.g. a "Search options"
pseudo-item gets exported instead of a real email). Keep CSS unblocked here.

> Headless mode does **not** block resources by itself — only this explicit code
> does, and it applies in both headless and visible modes.

### 4.2 Measuring time (filesystem journal)
Wall-clock script time includes idle waits, so the **relevant measure is the
filesystem journal**: the creation/modification timestamps of the files written
into each run folder. `run_perf.sh` (kept locally in `scratch/` — not published
to GitHub) runs N complete (or `--select`) exports on empty per-run folders with
different tunables; analyze the span
`max(mtime) − min(mtime)` of the run folder's files for the true export time, and
count `.eml`/`.html`/attachment files for completeness.

```bash
bash run_perf.sh                 # 3 short --select runs, varying WAIT/SLEEP/POLL
# then inspect ~/Downloads/openbrowser-daemon/<run> file mtimes
```

### 4.3 PerfLog — structured performance logging
Every daemon round-trip (RTT) and named execution phase is logged to
`<run_dir>/perf_log.jsonl` in JSONL format for post-hoc analysis.

**RTT entries** — one per `run()` call:
```jsonl
{"e": "rtt", "dur": 1.5234, "ok": true, "out_len": 1228}
```
At end of run, RTT stats are printed (p50, p95, p99, min, max).

**Phase entries** — instrumented function scopes with duration, RTT count, and
cumulative RTT time inside the phase:

| Phase | Scope | Records |
|---|---|---|
| `session.setup` | Daemon start + login + resource blocking | Wall time, RTTs inside |
| `session.search` | Search execution | Wall time, RTTs inside |
| `session.recovery` | Full session recovery (daemon→login→search→goto) | Wall time, RTTs inside, target page |
| `session.summary` | End-of-run stats print | Final counts |
| `page.navigate` | `goto_page()` pagination jump | Target page |
| `page.collect` | List-row enumeration via `_collect_all_rows()` | Page number |
| `page.total` | Full page processing (wraps `export_rows`) | Page number, row count |
| `email.open` | Opening an email (retries included) | EID |
| `email.extract` | `_poll_and_scan` — one bundled RTT | EID |
| `email.save` | `_click_both_saves` + file detection | EID |
| `email.attach` | UI attachment download | EID |
| `email.back` | Navigate next / back to list | EID |
| `email.total` | Full per-email lifecycle (wraps all sub-phases) | EID, page, position |

### 4.4 Adaptive save delay (`_SAVE_DELAY`)
When saving `.html` and `.eml`, the script clicks "Save (.html)", sleeps a gap,
then clicks "Save (.eml)". The gap (`_SAVE_DELAY`, defaults to `0.1s`)
**adaptively learns** the shortest reliable delay:

```
Email 1: try 0.1s → .eml fails → bump to 0.2s → try 0.2s → both succeed
         → _SAVE_DELAY = 0.2s (record the working delay)
Email 2: start at 0.2s → both succeed → keep at 0.2s
Email 3: start at 0.2s → .eml fails → bump to 0.3s → both succeed
         → _SAVE_DELAY = 0.3s
```

- **Cross-email persistence:** `_SAVE_DELAY` is a module-level `float`, so the
  learned value carries forward within a run.
- **Bump on any miss:** The delay increments `+0.1s` on any missing `.eml` file
  (not just when the daemon reports the click failed), capped at `1.0s`.
- **Partial updates:** If `.html` saves but `.eml` doesn't, `_SAVE_DELAY` is
  also bumped so the next email starts higher.

---

## 5. Bugs fixed during development

- **Page-4 navigation failure.** `goto_page(page)` only fell back to Next-page
  clicks when the page-input jump control was *missing*. When the jump was
  *present but ignored*, navigation failed → 0 ids → crash. Now it falls back to
  clicking Next when the jump does not confirm the target page.
- **`process_page` `IndexError` on empty `take` rows.** With `--select` and a
  failed navigation (0 ids collected), `rows[0]` crashed. The eid-range log line
  is now guarded and empty rows simply return 0.
- **Shared-folder `exported_ids` pollution.** Replaced the single shared output
  folder with per-run folders (§3.1), eliminating cross-run skip pollution.
- **Save delay jump to 0.3s.** The bump formula `(None or 0.2) + 0.1` jumped
  straight to `0.3` on the first miss, skipping `0.2`. Fixed to track the actual
  delay used rather than re-evaluating from `None`.
- **No cross-email save-delay learning.** `_delay` was a local variable that
  reset to the initial value for every email. Added module-level `_SAVE_DELAY`
  that records the last working delay and is used as the starting point for the
  next email.
- **Narrow bump condition.** The delay only bumped when `ok1 and not ok2`
  (daemon-reported click status). When the daemon reported both clicks as
  successful but the `.eml` file never appeared, the delay never increased.
  Changed to bump on any missing `.eml` file regardless of click status.
- **`_perf_log` never instantiated.** The module-level `_perf_log` variable was
  declared with type `PerfLog | None` at import time, but `PerfLog()` was never
  called. No performance log was ever written. Fixed by initializing in
  `main()` right after `prepare_run_dir()`.
- **`_RUN_RTT_BUDGET` UnboundLocalError.** The `+=` augmented assignment in
  `run()` made Python treat `_RUN_RTT_BUDGET` as a local variable, shadowing
  the module-level global. Added `global` declaration.
- **`_perf_log.summary()` signature mismatch.** The method required positional
  arguments but was called with none. Made all parameters optional with safe
  defaults.
- **Nested PerfLog phase budgets lost.** `_perf_phase()` saved and reset the
  RTT budget globals at creation time, then `__enter__` did it again. The
  creation-time save was to a discarded local, so parent budgets were zeroed
  before `__enter__` could capture them. Parent phases (`email.total`,
  `page.total`) showed only the last child's RTTs. Removed the premature
  save/reset from the factory function; `__enter__` alone handles it.
 - **No retry-pending for persistently failing emails.** A single flaky email
   could abort the whole run via the consecutive-failure limit, and it was never
   remembered across runs. Added a per-eid failure streak (`_FAIL_COUNTS`) that
   marks an email **retry-pending** (held in `_PENDING`, persisted as `bogus_ids`)
   after `OPEXPORT_BOGUS_THRESHOLD` (default 3) consecutive failures. A normal run
   re-attempts retry-pending emails (they are *not* subtracted from `skip_ids`);
   on success they are cleared. An email is also added to the within-run guard
   `_BOGUS_IDS` so the rest of the current run can proceed. When an email becomes
   retry-pending, `<run>/diagnostics/<eid>.json` + `<eid>.ax.txt` are written so
   you can see *why* it failed (wrong zoom, dead daemon, etc.). Use
   `unblacklist.py` to inspect/clear them, or `--fresh` to clear everything.
 - **No end-of-run completeness signal.** When any email is partial, retry-pending,
   or never seen, the run now prints `⚠ BACKUP INCOMPLETE` with a re-run command,
   connection-health (`_RTT_FAILS`) + a live daemon ping, and zoom-out advice; in a
   direct TTY it offers `Re-run now? [y/N]`.

---

## 6. Files

| File | Role |
|---|---|
| `export_emails.py` | The exporter. Config block + run-state helpers + per-email logic. Entry point: `main()`. |
| `run_interactive.py` | Interactive TUI launcher. Walks through all settings with defaults, presets, review screen, then spawns `export_emails.py`. |
| `unblacklist.py` | Inspect (`--list`), clear (`--remove`/`--remove-all`), and re-run (`--rerun`) retry-pending emails from `export_progress.json`. |
| `requirements.txt` | Python package dependencies (`questionary`, `openbrowser-ai`, `playwright`). |
| `test_*.py` | End-to-end / unit tests driving a live daemon (require a logged-in mail.com session). |
| `docs/DESIGN.md` | Design rationale for recovery, `--new`, download pipeline, etc. |
| `README.md` | This document. |
| `AGENTS.md` | Internal codebase map for AI agents (architecture, key functions, conventions). |

> **Note:** maintenance / debug / probe scripts (`clean_failed_runs.py`, `reconcile.py`,
> `migrate_dates.py`, `build_index.py`, `check_multi.py`, `validate_save.py`, `run.sh`,
> `run_perf.sh`, `debug_*.py`, `probe_*.py`, `explore*.py`, …) are intentionally **not
> published** to GitHub. They live in the local `scratch/` directory for reference and
> one-off investigations.



---

## 7. Recent changes

### Retry-pending (was "auto-blacklist") + diagnostics + completeness banner
- **Retry-pending model**: a persistently failing email (after `OPEXPORT_BOGUS_THRESHOLD`,
  default 3, failures) is held for retry on the **next** run rather than permanently
  excluded. It is recorded in two sets:
  - `_BOGUS_IDS` — within-run guard, reset empty each run; skips the eid for the rest of
    the current run so one dead email can't abort it.
  - `_PENDING` — retry marker, persisted as `export_progress.json["bogus_ids"]`, loaded at
    run start; a normal run re-attempts these (they are *not* in `skip_ids`); cleared on
    success.
  - `_FAIL_COUNTS` (persisted as `fail_counts`) tracks the per-eid streak across runs and
    drives promotion into `_PENDING`/`_BOGUS_IDS`.
- **Diagnostics**: when an email becomes retry-pending, `<run>/diagnostics/<eid>.json`
  (reason + per-step flags + counts) and `<eid>.ax.txt` (trimmed detail-view AX snapshot)
  are written so you can see *why* it failed (wrong zoom, dead daemon, etc.).
- **End-of-run completeness banner**: when any partial / retry-pending / unseen email
  remains, `export_emails.py` prints `⚠ BACKUP INCOMPLETE` plus a re-run command,
  connection-health (`_RTT_FAILS`) + a live daemon ping, and zoom-out advice; in a direct
  TTY it offers `Re-run now? [y/N]` (suppressed under `run_interactive.py` via
  `OPEXPORT_SUBPROCESS=1`).
- **`unblacklist.py`** (new): inspect (`--list`), clear (`--remove`/`--remove-all`), and
  re-run (`--rerun`) retry-pending emails from `export_progress.json`.
- `export_single_email()` now returns a 6-tuple including a `diag` dict; `export_rows()`
  returns `(exported_new, given_up_count)`; `process_page()` returns the same tuple
  (existing test scripts updated to unpack it).
- `OPEXPORT_SUBPROCESS=1` is now set on the child `export_emails.py` process spawned by
  `run_interactive.py` so the TUI keeps control of the terminal.
- **Local credential persistence** (new): `OPEXPORT_SEARCH_TERM` (*them*) and
  `OPEXPORT_LOCAL_ACCOUNT` (*you*) are no longer hard-coded defaults. If unset, the tool
  prompts once and saves them to `~/.config/emailexporter4mail.com/settings.json`
  (`chmod 600`, outside the repo) so subsequent runs auto-fill; `run_interactive.py`
  pre-fills its prompts from the same file and persists on submit.
