#!/usr/bin/env python3
"""
Export emails matching a search term from a mail.com mailbox.

Saves each email as .html and .eml via the browser's built-in export buttons.
Resumable: tracks progress in <run>/export_progress.json; skips already-exported
items. Search state tracked to avoid re-searching on resume.

EVERYTHING is configurable via environment variables (no hardwired values):
  OPEXPORT_ROOT        base dir that holds one sub-folder PER RUN (default
                       ~/Downloads/openbrowser-daemon)
  OPEXPORT_RUN         run folder name (stable name = resume/reuse; omit = a
                       timestamped folder is created so runs never collide)
  OPEXPORT_SEARCH_TERM / _SEARCH_FIELD / _SEARCH_FOLDER   the search
  OPEXPORT_LOCAL_ACCOUNT   the logged-in mailbox address (sent/received logic)
  OPEXPORT_HEADLESS    "true" for a hands-off run (needs a saved login);
                       "false" (or OPEXPORT_DEBUG=true) for a visible browser
  OPEXPORT_WAIT / _SLEEP / _OPEN_POLL / _PAGE_SIZE / _TOTAL   tunables

Workflow:
  1. Kill stale daemons, start a daemon (via `uvx openbrowser-ai daemon start`).
     The browser is visible only when debugging (OPEXPORT_HEADLESS=false /
     OPEXPORT_DEBUG=true) so the user can log into mail.com interactively.
  2. Navigate to mail.com
  3. Wait for the user to log in (polls URL + page state)
  4. Search SEARCH_FIELD / SEARCH_FOLDER for SEARCH_TERM
  5. Iterate search-result pages, exporting .html/.eml (+ attachments)

The browser downloads into its fixed staging dir (~/Downloads/openbrowser-daemon,
hardcoded in the daemon); this script detects those downloads there and RELATES
them into the per-run folder. Each run is isolated; failed runs are left intact
for inspection (clean_failed_runs.py empties them on demand). Within a run the
export is idempotent: emails already present on disk are skipped, so a run can be
re-run safely.
"""
import sys, re, asyncio, json, os, signal, time, shutil
from datetime import datetime

sys.path.insert(0, os.path.expanduser(
    "~/.local/share/uv/tools/openbrowser-ai/lib/python3.14/site-packages"
))
import warnings
warnings.filterwarnings("ignore", message="Failed to set up openbrowser logging")
from openbrowser.daemon.client import execute_code_via_daemon

class SessionLostError(Exception):
    """Daemon connection or browser session is unrecoverably broken."""

_SESSION_LOST = False  # set True when run() detects corruption/daemon death
_CONSEC_FAILS = 0       # incremented by export_rows, reset per page attempt
_ON_EMAIL_PAGE = True   # set False by _poll_and_scan when AX tree has no email content
_SAVE_DELAY = 0.1       # inter-save gap (html→eml); learned across emails, bumped on failure
_BOGUS_IDS: set[str] = set()      # WITHIN-RUN guard: eids that already failed BOGUS_THRESHOLD
                                    # times THIS run; skipped for the rest of the run so a dead
                                    # email can't abort the run. Reset to empty each run.
_PENDING: set[str] = set()         # RETRY-PENDING marker: eids queued for retry on the next
                                    # run (persisted in export_progress.json as bogus_ids). A
                                    # normal run re-attempts these (they're "missed"), and on
                                    # success they're cleared.
_FAIL_COUNTS: dict[str, int] = {}  # per-eid consecutive-failure streak (persisted); drives both
BOGUS_THRESHOLD = int(os.environ.get("OPEXPORT_BOGUS_THRESHOLD", "3"))  # fails before guard+retry-pending
_RTT_FAILS = 0                      # count of daemon RTTs that returned not-ok (connection-health signal)

# ── Performance log ───────────────────────────────────────────────────────────
class PerfLog:
    """Structured performance log — JSONL to <run_dir>/perf_log.jsonl + summary.

    Records every daemon round-trip (RTT) with duration, and every named phase
    (email open/save/attach, page navigation, session events) with timing and
    success. All writes are buffered and fail-safe (never raises).
    """

    def __init__(self, path: str):
        self.path = path
        self._buf: list[dict] = []
        self._t0 = time.monotonic()
        self.rtts: list[dict] = []

    def rtt(self, *, dur: float, ok: bool, out_len: int = 0):
        self.rtts.append({"dur": round(dur, 4), "ok": ok, "out_len": out_len})
        self._flush([{"e": "rtt", "dur": round(dur, 4), "ok": ok, "out_len": out_len}])

    def phase(self, name: str, *, dur: float, ok: bool = True, **kw):
        self._flush([{"e": name, "dur": round(dur, 4), "ok": ok, **kw}])

    def _flush(self, batch: list[dict]):
        if not self.path:
            return
        self._buf.extend(batch)
        if len(self._buf) >= 20:
            self._write()

    def _write(self):
        if not self._buf:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, 'a') as f:
                for rec in self._buf:
                    f.write(json.dumps(rec) + '\n')
        except OSError:
            pass
        self._buf = []

    def close(self):
        self._write()

    def summary(self, total_pages: int = 0, total_exported: int = 0, total_new: int = 0,
                seen: int = 0, partials: list | None = None, given_up: set | None = None):
        wall = time.monotonic() - self._t0
        nrtt = len(self.rtts)
        rtt_total = sum(r["dur"] for r in self.rtts)
        rtt_avg = (rtt_total / nrtt) if nrtt else 0
        lines = [
            f"\n{'='*55}",
            f"  Performance Summary",
            f"{'='*55}",
            f"  Wall clock:       {wall:.1f}s",
            f"  Pages:            {total_pages}",
            f"  Exported:         {total_exported}  (new: {total_new}, seen: {seen})",
            f"  Partials:         {len(partials) if partials else 0}",
            f"  Given up:         {len(given_up) if given_up else 0}",
            f"",
            f"  Daemon RTTs:      {nrtt} calls  ({rtt_total:.1f}s = {100*rtt_total/max(wall,1):.0f}% of wall)",
            f"  Avg RTT:          {rtt_avg:.3f}s",
            f"  Local + waits:    {wall - rtt_total:.1f}s ({100*(wall-rtt_total)/max(wall,1):.0f}% of wall)",
            f"",
        ]
        if nrtt:
            sorted_rtt = sorted(r["dur"] for r in self.rtts)
            p50 = sorted_rtt[len(sorted_rtt)//2]
            p95 = sorted_rtt[int(len(sorted_rtt)*0.95)]
            p99 = sorted_rtt[int(len(sorted_rtt)*0.99)]
            lines += [
                f"  RTT p50:          {p50:.3f}s",
                f"  RTT p95:          {p95:.3f}s",
                f"  RTT p99:          {p99:.3f}s",
                f"  RTT min:          {sorted_rtt[0]:.3f}s",
                f"  RTT max:          {sorted_rtt[-1]:.3f}s",
            ]
        lines += [
            f"  RTTs per export:  {nrtt/max(total_exported,1):.1f}",
            f"  RTT time/export:  {rtt_total/max(total_exported,1):.1f}s",
            f"",
            f"  Log:              {self.path}",
            f"{'='*55}",
        ]
        print('\n'.join(lines))

_perf_log: PerfLog | None = None

def _perf_phase(name: str, **kw):
    """Context manager: times a named phase and counts daemon RTTs within it.

    Usage:
        with _perf_phase("email.open", eid=eid):
            await _open_by_eid(eid)

    Budget save/reset/restore is handled entirely by _PerfPhaseCtx.__enter__
    and __exit__ — this factory only creates the context object.
    """
    t0 = time.monotonic()
    return _PerfPhaseCtx(name, t0, kw)


class _PerfPhaseCtx:
    __slots__ = ("name", "t0", "kw", "ok", "_saved_budget", "_saved_count")
    def __init__(self, name, t0, kw):
        self.name = name
        self.t0 = t0
        self.kw = kw
        self.ok = True
        self._saved_budget = None
        self._saved_count = None
    def __enter__(self):
        global _RUN_RTT_BUDGET, _RUN_RTT_COUNT_BUDGET
        self._saved_budget = _RUN_RTT_BUDGET
        self._saved_count = _RUN_RTT_COUNT_BUDGET
        _RUN_RTT_BUDGET = 0.0
        _RUN_RTT_COUNT_BUDGET = 0
        return self
    def __exit__(self, typ, val, tb):
        global _RUN_RTT_BUDGET, _RUN_RTT_COUNT_BUDGET
        dur = time.monotonic() - self.t0
        rtt = _RUN_RTT_BUDGET or 0.0
        rtt_n = _RUN_RTT_COUNT_BUDGET or 0
        if self._saved_budget is not None:
            _RUN_RTT_BUDGET = (self._saved_budget or 0.0) + rtt
            _RUN_RTT_COUNT_BUDGET = (self._saved_count or 0) + rtt_n
        else:
            _RUN_RTT_BUDGET = None
            _RUN_RTT_COUNT_BUDGET = None
        ok = (typ is None)
        if _perf_log is not None:
            _perf_log.phase(self.name, dur=dur, ok=ok and self.ok,
                          rtt=rtt_n, rtt_dur=round(rtt, 4), **self.kw)
        return False

# This script REQUIRES an interactive, VISIBLE browser so the user can log in to
# mail.com. The openbrowser daemon defaults to HEADLESS, and the script's
# auto-start path (run() -> execute_code_via_daemon() -> client.execute() ->
# _start_daemon()) spawns the server with THIS process's environment. So we must
# force non-headless HERE -- setting it only in step_start_daemon's `uvx ...
# daemon start` prefix is NOT enough, because the auto-start path bypasses that
# and would otherwise launch a headless browser with no window to log into.
# =====================================================================
# CONFIG -- EVERYTHING below is configurable via environment variables so
# different users can run different exports with no code changes. Nothing is
# hardwired except safe defaults.
# =====================================================================

# ── Session mode (headless / debug) ──────────────────────────────────────────
# The browser runs NON-HEADLESS only while debugging or when an interactive login
# is needed. For a hands-off production run set OPEXPORT_HEADLESS=true (this
# requires a saved login / storage_state). OPEXPORT_DEBUG=true forces a visible
# browser regardless of OPEXPORT_HEADLESS. Nothing about the session mode is
# hardwired; once the flow is smooth, headless becomes the default and the
# visible window is purely a debug option.
DEBUG = os.environ.get("OPEXPORT_DEBUG", "true").lower() in ("1", "true", "yes", "on")
_headless_env = os.environ.get("OPEXPORT_HEADLESS")
if _headless_env is None:
    _headless_env = "false" if DEBUG else "true"
os.environ['OPENBROWSER_HEADLESS'] = _headless_env
HEADLESS = (_headless_env.lower() == "true")

# ── Output root (SINGLE SOURCE OF TRUTH) ──────────────────────────────────────
# OPEXPORT_ROOT is the BASE directory that holds ONE sub-folder per run. Each run
# writes to its own folder so runs never share or overwrite each other's output,
# and a run that did NOT complete is emptied before it is re-used (see
# _prepare_run_dir). Override the base with OPEXPORT_ROOT and the run folder name
# with OPEXPORT_RUN (a stable name lets you resume/re-run cleanly; if omitted, a
# timestamped run folder is created automatically).
BASE_DIR = os.path.expanduser(os.environ.get("OPEXPORT_ROOT", "~/Downloads/openbrowser-daemon"))

def _resolve_run_name() -> str:
    name = os.environ.get("OPEXPORT_RUN", "").strip()
    if name:
        name = re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_')
        if name:
            return name
    return "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")

RUN_NAME = _resolve_run_name()
RUN_DIR = os.path.join(BASE_DIR, RUN_NAME)
PROGRESS_DIR = RUN_DIR  # ALL output dirs derive from this single value

# The openbrowser daemon ALWAYS downloads into this fixed folder (hardcoded in the
# daemon package). It is only a transient STAGING area: downloads are detected here
# and then relocated into the per-run PROGRESS_DIR. Keep detection pointed at it.
STAGE_DIR = os.path.expanduser("~/Downloads/openbrowser-daemon")

PROGRESS_FILE = os.path.join(PROGRESS_DIR, "export_progress.json")
RUN_STATE_FILE = os.path.join(BASE_DIR, ".opexport_runs.json")  # per-run completion status

# Separate folders per file type, as requested.
EML_DIR = os.path.join(PROGRESS_DIR, "eml")
HTML_DIR = os.path.join(PROGRESS_DIR, "html")
ATTACH_DIR = os.path.join(PROGRESS_DIR, "attachments")

# Drafts are real emails too, but must land in their own folder (separate from
# sent/received). They appear in the search results tagged `div title=Drafts`.
DRAFTS_DIR = os.path.join(PROGRESS_DIR, "drafts")
DRAFTS_EML_DIR = os.path.join(DRAFTS_DIR, "eml")
DRAFTS_HTML_DIR = os.path.join(DRAFTS_DIR, "html")
DRAFTS_ATT_DIR = os.path.join(DRAFTS_DIR, "attachments")

# Expected page size for non-last pages. mail.com shows 50 items per page in
# list-only mode (confirmed: page 3 of the test mailbox renders all 50).
# The viewport size affects how many rows the accessibility tree exposes; we
# adjust zoom/scroll/repoll until each non-last page reaches this count.
# Set OPEXPORT_PAGE_SIZE to override (e.g. a different provider).
PAGE_SIZE = int(os.environ.get("OPEXPORT_PAGE_SIZE", "50"))
# Optional total results count. When set, the script enforces that every
# non-last page has PAGE_SIZE items (by zooming/scrolling/repolling) so the
# final tally matches. Omit or set to 0 to skip enforcement.
TOTAL = int(os.environ.get("OPEXPORT_TOTAL", "0"))

# Resource types to BLOCK at the browser level (Playwright request interception)
# to speed up page loads -- including each per-email detail view. Comma-separated
# Playwright resource types; set OPEXPORT_BLOCK_RESOURCES="none" to disable.
# Blocking image/media/font is always safe and is the default. Blocking stylesheet
# (CSS) is NOT enabled by default: on mail.com the pagination / list controls are
# CSS-revealed, so aborting CSS removes them from the accessibility tree and breaks
# goto_page / _collect_all_rows (navigation fails, non-email rows get collected).
# Enable it explicitly (OPEXPORT_BLOCK_RESOURCES="image,media,font,stylesheet") only
# if you have verified the target UI still works with CSS blocked.
BLOCK_RESOURCES = os.environ.get("OPEXPORT_BLOCK_RESOURCES", "image,media,font")

# Tracks absolute paths of files the browser has downloaded this session, so we
# can match a Save click to its file precisely (the download lands in STAGE_DIR
# under its original subject-based name, which breaks mtime diffing when a file
# with that name already exists from a prior attempt).
DOWNLOAD_SEEN: set = set()

# ── Search filter (fully variable / granular, for ANY user) ───────────────────
# The address (or domain, or raw text) to search for. Resolved at runtime
# (env -> local saved config -> interactive prompt); None means "not set yet".
SEARCH_TERM = os.environ.get("OPEXPORT_SEARCH_TERM") or None
# Which part of each message to match against SEARCH_TERM. Granular per
# sender / recipient / subject, or all of them. Valid values mirror mail.com's
# search-field dropdown, e.g.: "All headers", "Sender", "Recipient", "Subject".
SEARCH_FIELD = os.environ.get("OPEXPORT_SEARCH_FIELD", "All headers")
# Which folder(s) to search. "All folders" or a specific folder name.
SEARCH_FOLDER = os.environ.get("OPEXPORT_SEARCH_FOLDER", "All folders")
# The logged-in mailbox address. Used to decide whether an email was SENT (you
# are the sender) or RECEIVED (you are the recipient), and to pick the other
# party for the filename. Resolved at runtime (env -> local saved config ->
# interactive prompt); None means "not set yet".
LOCAL_ACCOUNT = os.environ.get("OPEXPORT_LOCAL_ACCOUNT") or None

# ── Local credential persistence (outside the repo; never committed) ──────────
# The search term / local account are personal. They are resolved in priority
# order, at runtime (never at import):
#   1. environment variable (OPEXPORT_SEARCH_TERM / OPEXPORT_LOCAL_ACCOUNT)
#   2. a local JSON settings file under ~/.config (gitignored, lives outside repo)
#   3. an interactive prompt (only when stdin is a TTY), after which the value is
#      saved back to the local settings file so it auto-fills on the next run.
_LOCAL_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config",
                                 "emailexporter4mail.com")
_LOCAL_CONFIG_FILE = os.path.join(_LOCAL_CONFIG_DIR, "settings.json")


def _load_credential(config_key: str) -> str | None:
    """Read a saved personal value from the local settings file (or None)."""
    try:
        with open(_LOCAL_CONFIG_FILE) as f:
            return (json.load(f) or {}).get(config_key) or None
    except (OSError, ValueError):
        return None


def _save_credential(config_key: str, value: str) -> None:
    """Persist a personal value to the local settings file (chmod 600). Best
    effort: failures (e.g. read-only home) are silently ignored."""
    try:
        os.makedirs(_LOCAL_CONFIG_DIR, exist_ok=True)
        data: dict = {}
        try:
            with open(_LOCAL_CONFIG_FILE) as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            pass
        data[config_key] = value
        with open(_LOCAL_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(_LOCAL_CONFIG_FILE, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _resolve_credential(env_key: str, config_key: str, prompt_text: str,
                        example: str) -> str:
    """Resolve a personal value: env -> local saved config -> interactive prompt.

    Aborts with a clear message (non-TTY, no env, nothing saved) so headless /
    scripted runs fail loudly instead of hanging on a prompt.
    """
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    saved = _load_credential(config_key)
    if saved:
        return saved
    if not sys.stdin.isatty():
        raise SystemExit(
            f"ERROR: {env_key} is not set and no saved value exists. Set it via "
            f"the environment (export {env_key}=... ) or run interactively once "
            f"to save it locally.")
    while True:
        val = input(f"{prompt_text} [{example}]: ").strip()
        if val:
            _save_credential(config_key, val)
            return val
        print("  (a value is required; enter it, or press Ctrl-C to abort)")

# --- Tunables (env-overridable for experiments) -------------------------
# How long to wait for a Save click to actually produce a download before
# declaring it failed and retrying. Lower = fail faster (good for measuring
# the true click-reliability / retry rate). Set via OPEXPORT_WAIT (seconds).
DOWNLOAD_WAIT_TIMEOUT = float(os.environ.get("OPEXPORT_WAIT", "10"))
# Fixed settle sleeps between actions. Set via OPEXPORT_SLEEP (seconds).
SETTLE_SLEEP = float(os.environ.get("OPEXPORT_SLEEP", "0.3"))
# Poll interval while waiting for an email's detail view to open after we click
# the list row. Shorter = we detect the open sooner and click "Show further
# actions" with less delay, but too short can miss a slowly-opening view.
# Set via OPEXPORT_OPEN_POLL (seconds).
OPEN_POLL_SLEEP = float(os.environ.get("OPEXPORT_OPEN_POLL", "0.3"))

_RUN_RTT_BUDGET: float | None = None  # set >0 to track cumulative RTT time within a phase
_RUN_RTT_COUNT_BUDGET: int | None = None  # set to count RTTs within a phase

async def run(code: str) -> tuple[bool, str]:
    """Execute code on the daemon with transient-error retry and session-failure detection.

    Transient connection errors are retried up to 3 times with exponential
    backoff. If the daemon responds but the browser page is in a corrupted state
    (known error patterns), we set ``_SESSION_LOST = True`` so the main loop
    can trigger a full session recovery. Returns ``(success, output)`` on every
    code path — never raises.
    """
    global _SESSION_LOST, _RUN_RTT_BUDGET, _RUN_RTT_COUNT_BUDGET
    t0 = time.monotonic()
    last_error: Exception | None = None
    ok = False
    out = ""
    for attempt in range(3):
        try:
            resp = await execute_code_via_daemon(code)
        except (ConnectionRefusedError, FileNotFoundError, OSError, TimeoutError) as e:
            last_error = e
            await asyncio.sleep(1.0 * (2 ** attempt))
            continue
        out = (resp.output or "") + ("\n" + resp.error if resp.error else "")
        if resp.error and "has no attribute" in resp.error:
            if attempt < 2:
                await asyncio.sleep(0.5)
                continue
            _SESSION_LOST = True
        ok = resp.success
        break
    else:
        _SESSION_LOST = True
        out = f"Daemon unreachable after 3 attempts: {last_error}"
    dur = time.monotonic() - t0
    global _RTT_FAILS
    if not ok:
        _RTT_FAILS += 1
    if _perf_log is not None:
        _perf_log.rtt(dur=dur, ok=ok, out_len=len(out))
    if _RUN_RTT_BUDGET is not None:
        _RUN_RTT_BUDGET += dur
    if _RUN_RTT_COUNT_BUDGET is not None:
        _RUN_RTT_COUNT_BUDGET += 1
    return ok, out


async def _wait_for_state(
    condition: str,
    *,
    prelude: str = "",
    polls: int = 12,
    interval: float = 0.3,
    backoff: float = 1.0,
    max_interval: float = 1.0,
    label: str = "",
) -> bool:
    """Poll browser.get_state_as_text() until ``condition`` is truthy in the daemon.

    ``condition`` is a Python expression evaluated inside the daemon with
    variable ``st`` bound to the current state text.  Example::

        await _wait_for_state("'detail-body-iframe' in st")

    Optional ``prelude`` is prepended as setup code (e.g. ``"import re"``).
    When ``backoff > 1.0`` the poll interval grows after each failure
    (multiplied by ``backoff``, capped at ``max_interval``), reducing daemon
    noise for server-side operations.  ``label`` is printed in timing logs::

        [poll search-enter] OK 3/8 (1.5s)

    Returns ``True`` once the condition is met, ``False`` after all polls
    exhausted without it ever becoming true.

    When the env-var ``OPEXPORT_POLLS`` or ``OPEXPORT_POLL_INTERVAL`` is
    set it overrides the per-call defaults — useful for capturing a
    connection-speed baseline (e.g. ``OPEXPORT_POLLS=30
    OPEXPORT_POLL_INTERVAL=0.1``), then omitted for production runs.
    """
    import os as _os, time as _time
    _pe = _os.environ.get("OPEXPORT_POLLS")
    _ie = _os.environ.get("OPEXPORT_POLL_INTERVAL")
    if _pe is not None:
        polls = int(_pe)
    if _ie is not None:
        interval = float(_ie)
    t0 = _time.monotonic()
    cur = interval
    for i in range(polls):
        s, out = await run(
            "st = await browser.get_state_as_text();\n"
            + (prelude + "\n" if prelude else "")
            + f"print('WF_OK' if ({condition}) else 'WF_NO')"
        )
        if not s and _SESSION_LOST:
            tag = f" {label}" if label else ""
            print(f"  [poll{tag}] SESSION LOST ({i+1}/{polls})")
            return False
        if any('WF_OK' in ln for ln in out.split('\n')):
            elapsed = _time.monotonic() - t0
            tag = f" {label}" if label else ""
            print(f"  [poll{tag}] OK {i+1}/{polls} ({elapsed:.1f}s)")
            return True
        if i < polls - 1:
            await asyncio.sleep(cur)
            if backoff > 1.0:
                cur = min(cur * backoff, max_interval)
    elapsed = _time.monotonic() - t0
    tag = f" {label}" if label else ""
    print(f"  [poll{tag}] FAILED {i+1}/{polls} ({elapsed:.1f}s)")
    return False


def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"exported": 0, "last_page": 1, "skip_on_page": 0, "search_done": False,
            "max_pages": 0, "exported_ids": [], "seen_ids": [],
            "attachment_ids": [], "exported_map": {}, "partials": [],
            "bogus_ids": [], "fail_counts": {},
            "save_delay": 0.0}

def save_progress(p: dict) -> None:
    os.makedirs(PROGRESS_DIR, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f, indent=2)

def count_downloaded_files() -> int:
    n = 0
    for d in (EML_DIR, HTML_DIR):
        if os.path.isdir(d):
            n += len([f for f in os.listdir(d)
                      if f.endswith((".html", ".eml", ".htm"))])
    return n


# ── Per-run folder lifecycle ─────────────────────────────────────────────────
# Each run gets its own folder (RUN_DIR) under BASE_DIR. We NEVER auto-delete a
# run's output: a run that fails is left intact so it can be inspected. A separate
# maintenance script (clean_failed_runs.py) empties non-completed runs on demand.
# Here we only record each run's status so that tool (and the user) can tell which
# runs finished and which did not.
def _load_run_states() -> dict:
    if os.path.exists(RUN_STATE_FILE):
        try:
            with open(RUN_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_run_states(states: dict) -> None:
    os.makedirs(os.path.dirname(RUN_STATE_FILE), exist_ok=True)
    with open(RUN_STATE_FILE, "w") as f:
        json.dump(states, f, indent=2)


def _mark_run_status(status: str) -> None:
    states = _load_run_states()
    entry = states.get(RUN_NAME, {})
    entry.update({"status": status,
                  "run_name": RUN_NAME,
                  "run_dir": RUN_DIR,
                  "updated_at": datetime.now().isoformat()})
    if "started_at" not in entry:
        entry["started_at"] = datetime.now().isoformat()
    states[RUN_NAME] = entry
    _save_run_states(states)


async def _install_resource_blocking() -> None:
    """Block slow resource types (images/CSS/fonts/media) at the browser level.

    Installed ONCE per session via Playwright request interception on the browser
    context, so it speeds up EVERY subsequent navigation -- including each
    per-email detail view. Documents/scripts/XHR/fetch are never blocked, so the
    accessibility tree (DOM-based) and the .eml/.html downloads keep working.
    Disabled when OPEXPORT_BLOCK_RESOURCES is "none". Best-effort: a failure here
    is reported and the run continues unblocked.
    """
    blocked = {b.strip().lower() for b in BLOCK_RESOURCES.split(",") if b.strip()}
    if not blocked or blocked == {"none"}:
        print("  [block] resource blocking disabled")
        return
    try:
        s, out = await run(f"""
blocked = {sorted(blocked)!r}
page = await browser.get_current_page()
ctx = getattr(page, 'context', None)
if ctx is None:
    print('RESOURCE_BLOCK_SKIPPED no page.context')
else:
    async def _block_route(route):
        if route.request.resource_type in blocked:
            await route.abort()
        else:
            await route.continue_()
    await ctx.route('**/*', _block_route)
    print('RESOURCE_BLOCK_INSTALLED ' + ','.join(sorted(blocked)))
""")
        print(f"  [block] {(out or '').strip() or ('status=' + str(s))}")
    except Exception as e:
        print(f"  [block] WARNING: could not install resource blocking: {e}")


def prepare_run_dir() -> None:
    """Create this run's output folder and record its status as 'running'.

    Does NOT delete any existing output. Failed/partial runs are preserved for
    inspection; emptying them is the job of clean_failed_runs.py.
    """
    for d in (PROGRESS_DIR, EML_DIR, HTML_DIR, ATTACH_DIR,
              DRAFTS_DIR, DRAFTS_EML_DIR, DRAFTS_HTML_DIR, DRAFTS_ATT_DIR):
        os.makedirs(d, exist_ok=True)
    _mark_run_status("running")
    print(f"  Run folder: {RUN_DIR}")
    print(f"  Run name : {RUN_NAME}  (set OPEXPORT_RUN=<name> to resume/reuse)")


def build_email_index(path: str | None = None) -> int:
    """Write a human-readable, newest-first index of every exported email.

    One block per email: date, From -> To, subject, on-disk status (eml /
    html / attachment count) and the filename prefix. Re-run after every export
    so newly added emails land in the correct (newest-first) position. Returns
    the number of emails indexed."""
    import email as _email
    from email.utils import parsedate_to_datetime, getaddresses as _ga
    if path is None:
        path = os.path.join(PROGRESS_DIR, "email_index.txt")
    entries = []

    def _who(msg, h):
        addrs = [a for _, a in _ga(msg.get_all(h, []))]
        return addrs[0] if addrs else ''

    # Scan both the main folders and the Drafts sub-folder.
    _dirsets = [(EML_DIR, HTML_DIR, ATTACH_DIR, ''),
                (DRAFTS_EML_DIR, DRAFTS_HTML_DIR, DRAFTS_ATT_DIR, 'DRAFTS')]
    for eml_dir, html_dir, att_dir_root, flabel in _dirsets:
        if not os.path.isdir(eml_dir):
            continue
        for f in os.listdir(eml_dir):
            if not f.lower().endswith('.eml'):
                continue
            prefix = f[:-4]
            try:
                msg = _email.message_from_binary_file(
                    open(os.path.join(eml_dir, f), 'rb'))
            except Exception:
                continue
            dt = None
            if msg.get('Date'):
                try:
                    dt = parsedate_to_datetime(msg.get('Date'))
                except Exception:
                    dt = None
            frm, to = _who(msg, 'From'), _who(msg, 'To')
            subj = msg.get('Subject', '') or ''
            has_html = os.path.exists(os.path.join(html_dir, prefix + '.html'))
            att_dir = os.path.join(att_dir_root, prefix)
            att_n = 0
            if os.path.isdir(att_dir):
                att_n = len([x for x in os.listdir(att_dir)
                             if os.path.isfile(os.path.join(att_dir, x))])
            disp = (flabel + '/' + prefix) if flabel else prefix
            entries.append((dt, disp, frm, to, subj, has_html, att_n))
    # Newest first; emails whose date can't be parsed sort to the very bottom.
    entries.sort(key=lambda e: (e[0] is not None, e[0] or datetime.min),
                 reverse=True)
    lines = [f"Email Index (newest -> oldest)  - generated "
             f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"Total: {len(entries)} emails", ""]
    for dt, prefix, frm, to, subj, has_html, att_n in entries:
        dts = dt.strftime('%Y-%m-%d %H:%M') if dt else '????-??-?? ??:??'
        status = ['eml', 'html' if has_html else 'HTML-MISSING', f'att:{att_n}']
        lines.append(f"{dts}  [{frm} -> {to}]")
        lines.append(f"    {subj}")
        lines.append(f"    {' | '.join(status)}   file: {prefix}")
        lines.append("")
    try:
        with open(path, 'w') as o:
            o.write('\n'.join(lines))
    except OSError:
        return len(entries)
    return len(entries)

def kill_existing_daemons():
    import subprocess as _sp
    for f in [os.path.expanduser("~/.openbrowser/daemon.pid"),
              os.path.expanduser("~/.openbrowser/daemon.sock")]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass
    try:
        p = _sp.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        for line in p.stdout.split('\n'):
            if 'openbrowser' in line and 'grep' not in line:
                parts = line.split()
                if parts:
                    pid = int(parts[1])
                    os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    time.sleep(2)

async def wait_for_daemon(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ok, out = await run("print('ping')")
            if ok:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False

async def step_start_daemon():
    # NOTE: mail.com login is INTERACTIVE, so the daemon MUST run NON-headless
    # (OPENBROWSER_HEADLESS=false) and be started via `uvx openbrowser-ai` so the
    # user can SEE and log into the browser window. A headless daemon has no
    # visible window, and step_wait_login() would then block forever waiting for
    # a login that can never happen. This was the cause of a previous deadlock.
    print("=== Starting daemon (clean session, VISIBLE browser for login) ===")
    kill_existing_daemons()
    proc = await asyncio.create_subprocess_shell(
        f"OPENBROWSER_HEADLESS={_headless_env} uvx openbrowser-ai daemon start",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    ready = await wait_for_daemon(timeout=35)
    print(f"  Daemon {'ready' if ready else 'WARNING: not ready'}")
    return ready

async def step_navigate():
    print("=== Navigate to mail.com ===")
    s, out = await run("print(await navigate('https://mail.com'))")
    await _wait_for_state("len(st) > 100", polls=12, interval=0.5, label="navigate")

async def _already_logged_in() -> bool:
    """Return True if the browser is already showing the logged-in mailbox.

    Lets a run that reuses a still-authenticated daemon skip navigation and the
    interactive login wait entirely (important for back-to-back automated runs,
    where re-navigating away from a live session can drop the login detection)."""
    try:
        s, out = await run("print(await browser.get_current_page_url())")
        url = out.strip()
        s, state_out = await run("state = await browser.get_state_as_text(); print(state[:2000])")
    except Exception:
        return False
    if '/mail' in url and 'sid=' in url:
        return True
    if 'list-mail-item' in state_out or 'thirdPartyFrame_mail' in state_out:
        return True
    return False


async def step_wait_login():
    """Wait (with an optional timeout) for the user to log into mail.com.

    Polls the page URL and state looking for evidence of the logged-in
    mailbox interface (URL containing '/mail' and `sid=` parameter).
    Prints a reminder every ~18 seconds. Set OPEXPORT_LOGIN_TIMEOUT (seconds,
    0 = wait forever) to fail fast instead of hanging in automated contexts.
    Returns True once logged in, False if the timeout elapsed.
    """
    timeout = float(os.environ.get("OPEXPORT_LOGIN_TIMEOUT", "0") or "0")
    print("\n  \a=== PLEASE LOG INTO mail.com IN THE BROWSER WINDOW ===")
    print("  (The script will wait until you are logged in)\n")
    tick = 0
    deadline = time.time() + timeout if timeout > 0 else None
    while True:
        s, out = await run("print(await browser.get_current_page_url())")
        url = out.strip()
        s, state_out = await run("state = await browser.get_state_as_text(); print(state[:2000])")

        logged_in = False
        if '/mail' in url and 'sid=' in url:
            logged_in = True
        if 'list-mail-item' in state_out or 'thirdPartyFrame_mail' in state_out:
            logged_in = True

        if logged_in:
            print(f"  Logged in via: {url[:80]}")
            await _wait_for_state(
                "'list-mail-item' in st or 'thirdPartyFrame_mail' in st",
                polls=10, interval=0.3, label="login-settle",
            )
            return True

        if deadline is not None and time.time() >= deadline:
            print("  LOGIN TIMEOUT: not logged in within "
                  f"{timeout:g}s; aborting run")
            return False

        tick += 1
        if tick % 6 == 0:  # every ~18 s
            print(f"  Waiting for login... (url: {url[:50]})\a")
        await asyncio.sleep(3)

async def find_element(pattern: str, extra_filter: str = "") -> int | None:
    code = f"""
import re
state = await browser.get_state_as_text()
lines = state.splitlines()
for i,l in enumerate(lines):
    if {repr(pattern)} in l {extra_filter}:
        m = re.search(r'\\[(\\d+)\\]', l)
        if m: print(f"IDX:{{m.group(1)}}"); break
else:
    print("IDX:none")
"""
    s, out = await run(code)
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('IDX:'):
            v = ln.split(':', 1)[1]
            return int(v) if v.isdigit() else None
    return None

async def wait_for_element(pattern: str, extra_filter: str = "", timeout=15) -> int | None:
    """Poll state until element matching pattern appears, then return its index."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        idx = await find_element(pattern, extra_filter)
        if idx is not None:
            return idx
        await asyncio.sleep(1)
    return None

async def step_do_search():
    """Search SEARCH_FIELD / SEARCH_FOLDER for SEARCH_TERM.

    Dynamically discovers element indices each time via state text matching.
    """
    print(f"=== Search for '{SEARCH_TERM}' ===")

    idx = await wait_for_element("searchTermInput", timeout=20)
    if idx is None:
        print("  FAILED: search input not found")
        return False
    print(f"  Found search input at [{idx}]")
    await run(f"print(await input_text({idx}, '{SEARCH_TERM}', clear=True))")
    await asyncio.sleep(0.5)

    for attempt in range(3):
        await run("print(await send_keys('Enter'))")
        if await _wait_for_state(
            "re.search(r'Search results', st) is not None",
            prelude="import re",
            polls=8, interval=0.5, backoff=1.15, label="search-enter",
        ):
            print(f"  Search results found via Enter key (attempt {attempt + 1})")
            return True
        print(f"  Enter search attempt {attempt + 1} returned no results, retrying...")

    print("  Enter key didn't trigger search; trying options panel...")

    idx = await find_element("Show search options")
    if idx is None:
        print("  FAILED: Show search options not found")
        return False
    await run(f"print(await click({idx}))")
    await _wait_for_state("'headerNameSelect' in st", polls=8, interval=0.3, label="search-panel")

    idx = await find_element("headerNameSelect")
    if idx:
        await run(f"print(await select_dropdown(index={idx}, text='{SEARCH_FIELD}'))")
        await asyncio.sleep(0.5)

    idx = await find_element("folderSelect")
    if idx:
        await run(f"print(await select_dropdown(index={idx}, text='{SEARCH_FOLDER}'))")
        await asyncio.sleep(0.5)

    idx = await find_element("type=submit", "and 'Search' in l")
    if idx is None:
        idx = await find_element("title=Search", "and 'disabled' not in l")
    if idx:
        await run(f"print(await click({idx}))")
        await _wait_for_state(
            "re.search(r'Search results', st) is not None",
            prelude="import re",
            polls=10, interval=0.5, backoff=1.15, label="search-submit",
        )
        print("  Search submitted via options panel")
    else:
        print("  WARNING: no submit button found")
        return False

    s, out = await run("""
state = await browser.get_state_as_text()
import re
print(f"SR:{'YES' if re.search(r'Search results', state) else 'NO'} AOA:{'YES' if re.search(re.escape(SEARCH_TERM), state) else 'NO'}")
""")
    print(f"  Result: {out.strip()}")
    return 'SR:YES' in out

async def _read_page_state():
    """Return (current_page:int|None, input_idx:int|None, next_idx:int|None)."""
    code = r"""
import re
state = await browser.get_state_as_text()
m = re.search(r'title=Current page:\s*(\d+)', state)
print('CUR:' + (m.group(1) if m else '?'))
cur = int(m.group(1)) if m else None
inp = nxt = None
for l in state.splitlines():
    if 'title=Current page' in l:
        mml = list(re.finditer(r'\[(\d+)\]', l))
        if mml:
            inp = int(mml[-1].group(1))
    if 'title=Next page' in l.lower():
        mml = list(re.finditer(r'\[(\d+)\]', l))
        if mml:
            nxt = int(mml[-1].group(1))
print('INP:' + str(inp))
print('NXT:' + str(nxt))
"""
    s, out = await run(code)
    cur = inp = nxt = None
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('CUR:'):
            v = ln[4:]
            cur = int(v) if v.isdigit() else None
        elif ln.startswith('INP:'):
            v = ln[4:]
            inp = int(v) if v.isdigit() else None
        elif ln.startswith('NXT:'):
            v = ln[4:]
            nxt = int(v) if v.isdigit() else None
    return cur, inp, nxt


async def goto_page(page: int) -> None:
    """Navigate mail.com search results to *page*.

    mail.com paginates via an <input title="Current page: N"> box. Typing the
    target number and pressing Enter jumps directly. If that jump fails to land
    (the input is sometimes ignored), fall back to clicking the "Next page"
    button the required number of times from the current page.
    """
    # Ensure we are on a fully-rendered list (pagination controls present) before
    # reading page state. After exporting emails the view can collapse to
    # preview-only, where the pagination input/buttons are absent from the tree.
    with _perf_phase("page.navigate", page=page):
        await _ensure_list_only()
        for attempt in range(3):
            cur, inp, nxt = await _read_page_state()
            if DEBUG:
                print(f"  [goto] attempt {attempt+1}: cur={cur} inp={inp} "
                      f"nxt={nxt}")
            if cur == page:
                print(f"  Already on page {page}")
                return
            # Prefer the "Current page" input jump.
            if inp is not None:
                await run(f"print(await input_text({inp}, '{page}', clear=True))")
                await asyncio.sleep(0.6)
                await run("print(await send_keys('Enter'))")
                if await _wait_for_state(
                    f"re.search(r'title=Current page:\\s*{page}', st) is not None",
                    prelude="import re",
                    polls=8, interval=0.3, label="page-jump",
                ):
                    print(f"  Navigated to page {page}")
                    return
                cur2, _, _ = await _read_page_state()
                if DEBUG:
                    print(f"  [goto] after jump: cur2={cur2}")
                if cur2 == page:
                    print(f"  Navigated to page {page}")
                    return
                print(f"  page jump to {page} not confirmed (now {cur2}); "
                      f"falling back to Next-page clicks")
                cur = cur2
        # Fallback: click "Next page" until we reach the target.
        if nxt is not None and cur is not None and page > cur:
            print(f"  clicking Next page x{page - cur}")
            for _ in range(page - cur):
                await run(f"print(await click({nxt}))")
                if await _wait_for_state(
                    f"re.search(r'title=Current page:\\s*{page}', st) is not None",
                    prelude="import re",
                    polls=8, interval=0.3, label="page-next",
                ):
                    print(f"  Navigated to page {page} via Next")
                    return
                cur, _, _ = await _read_page_state()
                if cur == page:
                    print(f"  Navigated to page {page} via Next")
                    return
                if cur is None:
                    break
        # last resort: scroll to top to defeat virtualization and retry
        await run("await send_keys('Home')")
        await asyncio.sleep(1.0)
    print(f"  WARNING: could not navigate to page {page}")

def _dir_snapshot() -> dict:
    """Return {filename: mtime} for all files in the browser's STAGE_DIR.

    The daemon downloads into STAGE_DIR (hardcoded in the daemon package); this
    script relocates those files into the per-run PROGRESS_DIR. So download
    detection must watch STAGE_DIR, not PROGRESS_DIR.
    """
    snap = {}
    if not os.path.isdir(STAGE_DIR):
        return snap
    for f in os.listdir(STAGE_DIR):
        fp = os.path.join(STAGE_DIR, f)
        if os.path.isfile(fp):
            snap[f] = os.path.getmtime(fp)
    return snap


async def _collect_downloads() -> list[str]:
    """Return absolute paths of session downloads not yet seen, marking them seen.

    Uses the browser's own download tracker (browser.downloaded_files) rather
    than directory mtime diffing, so re-downloads of a file with an identical
    original name are still detected.
    """
    s, out = await run("""
import json
try:
    print('DLJSON' + json.dumps(browser.downloaded_files))
except Exception:
    print('DLJSON[]')
""")
    paths: list[str] = []
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('DLJSON'):
            try:
                paths = json.loads(ln[6:])
            except Exception:
                paths = []
            break
    new = []
    for p in paths:
        if p in DOWNLOAD_SEEN:
            continue
        if os.path.exists(p):
            new.append(p)
            DOWNLOAD_SEEN.add(p)
    return new


async def _prime_downloads() -> None:
    """Mark all downloads already present in the session as seen (call once)."""
    await _collect_downloads()

async def _wait_for_new_file(before: dict, suffix: str, timeout=30) -> str | None:
    """Wait for a download to finish, *verifying* it is fully written.

    Instead of returning the instant a file merely appears (possibly still
    mid-write), we lock onto the new/updated file and confirm its size is
    stable across two reads before returning. `before` maps filename -> mtime
    captured just before the click, so a same-named re-download (newer mtime)
    is still detected. The *timeout* is only a safety net; in the normal case
    we return as soon as the file is complete.
    """
    deadline = time.time() + timeout
    candidate = None
    while time.time() < deadline:
        after = _dir_snapshot()
        if candidate is None:
            for f, mt in after.items():
                if f.endswith(suffix) and (f not in before or after[f] > before.get(f, 0)):
                    candidate = f
                    break
        if candidate:
            p = os.path.join(STAGE_DIR, candidate)
            try:
                s1 = os.path.getsize(p)
            except OSError:
                s1 = -1
            await asyncio.sleep(0.3)
            try:
                s2 = os.path.getsize(p)
            except OSError:
                s2 = -1
            if s1 == s2 and s1 > 0:
                return candidate
            # still being written; keep waiting on this candidate
        else:
            await asyncio.sleep(0.2)
    return candidate

def _sanitize(text: str, maxlen=200) -> str:
    """Replace chars unsafe for filenames; collapse whitespace."""
    safe = text.replace('@', '.')
    safe = re.sub(r'[^\w\s\-.,()#]', '_', safe)
    safe = re.sub(r'\s+', ' ', safe).strip()
    return safe[:maxlen].rstrip('._- ')

async def _click_both_saves(act_index: int | None, delay: float = 0.1) -> tuple[bool, bool]:
    POLLS, SLEEP = 6, 0.15
    code = f'''
import re, asyncio
def _find_act(state):
    for l in state.splitlines():
        if 'Show further actions' in l:
            m = re.search(r'\\[(\\d+)\\]', l)
            if m: return int(m.group(1))
    return None
async def _open_and_click(act, label):
    if act is None: return False
    await click(act)
    for _ in range({POLLS}):
        st = await browser.get_state_as_text()
        ls = st.split('\\n')
        for i, l in enumerate(ls):
            if label in l and i > 0:
                m = re.search(r'\\[(\\d+)\\]', ls[i - 1])
                if m:
                    await click(int(m.group(1)))
                    return True
        await asyncio.sleep({SLEEP})
    return False
if {act_index!r} is None:
    act = _find_act(await browser.get_state_as_text())
else:
    act = {act_index!r}
ok1 = await _open_and_click(act, 'Save (.html)')
await asyncio.sleep({delay})
ok2 = await _open_and_click(act, 'Save (.eml)')
print(f'CLICKED html={{ok1}} eml={{ok2}} delay={delay}')
'''
    s, out = await run(code)
    ok1 = 'html=True' in out
    ok2 = 'eml=True' in out
    if not ok2:
        _trim = out.replace('\n', ' | ')[:160]
        print(f"    [debug] _click_both_saves: html={ok1} eml={ok2} | {_trim}")
    return ok1, ok2


async def _find_download_targets() -> tuple[list[int], int | None]:
    """Locate attachment download controls in the open email.

    Returns ``(individual_buttons, zip_button)`` where:
      * ``individual_buttons`` = indices of per-attachment
        'Download (to hard disc)' buttons,
      * ``zip_button`` = index of the 'Download all files (ZIP)' button, or
        ``None``.

    The email detail view renders an attachment section (between the
    'Show further actions' area and the mail body). Multiple attachments expose
    a single 'Download all files (ZIP)' button (plus per-attachment buttons we
    don't need); a single attachment exposes only a per-attachment button.
    Read fresh from the accessibility tree on every call so indices stay valid.
    """
    s, out = await run(r"""
import re
state = await browser.get_state_as_text()
indiv = []
zipbtn = None
for l in state.splitlines():
    ll = l.lower()
    if 'download (to hard disc)' in ll:
        m = list(re.finditer(r'\[(\d+)\]', l))
        if m:
            indiv.append(int(m[-1].group(1)))
    elif 'download all files' in ll:
        m = list(re.finditer(r'\[(\d+)\]', l))
        if m and zipbtn is None:
            zipbtn = int(m[-1].group(1))
print('DL ' + repr((indiv, zipbtn)))
""")
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('DL '):
            try:
                indiv, zipbtn = eval(ln[3:])
                return indiv, zipbtn
            except Exception:
                return [], None
    return [], None

async def _download_one(idx: int, dest: str, snapshot: set) -> str | None:
    """Click download button [idx], wait for the file, rename into dest.

    Returns the sanitized basename that was written, or None if no download came.
    """
    before = _dir_snapshot()
    _s, _out = await run(f"""
        st = await browser.get_state_as_text()
        for l in st.splitlines():
            if '[{idx}]' in l:
                print('DL_ELEM_AT_' + str({idx}) + ': ' + l[:200])
                break
        print(await click({idx}))
    """)
    for _l in _out.splitlines():
        if _l.startswith('DL_ELEM_AT_') or _l.strip().startswith('clicked'):
            print(f"    [dl] {_l}")
    got = None
    for _ in range(20):
        after = _dir_snapshot()
        cands = [f for f in after
                 if (f not in before or after[f] > before.get(f, 0))
                 and not f.endswith('.crdownload')
                 and not f.endswith('.part')]
        if cands:
            got = cands[0]
            break
        await asyncio.sleep(1.0)
    if not got:
        return None
    src = os.path.join(STAGE_DIR, got)
    safe = _sanitize(got, maxlen=160)
    target = os.path.join(dest, safe)
    if os.path.exists(target):
        # Idempotent re-run: attachment already present; discard the new copy.
        try:
            os.remove(src)
        except OSError:
            pass
        return safe
    try:
        os.rename(src, target)
    except OSError as e:
        print(f"    [attach] rename failed: {e}")
        return None
    return os.path.basename(target)


_ATTACH_SCAN = object()

async def _download_ui_attachments(
    name_prefix: str,
    indiv: list[int] | None = _ATTACH_SCAN,
    zipbtn: int | None = _ATTACH_SCAN,
) -> tuple[int, bool, bool]:
    """Download attachments for the open email into ATTACH_DIR/<name_prefix>/. 
    When ``indiv`` / ``zipbtn`` are provided (from ``_poll_and_scan()``) the scan 
    is skipped, saving one daemon round-trip.

    mail.com exposes two DISTINCT controls, and which one appears tells you how
    many attachments there are:

      * a single ``Download (to hard disc)`` button -> exactly ONE attachment
      * a ``Download all files (ZIP)`` button        -> TWO OR MORE attachments
      * neither present                              -> NO attachments

    So the presence of the ZIP button is the discriminator: if it is there we
    have a multi-attachment email (download the ZIP once, extract in place, keep
    the ZIP renamed to ``<name_prefix>.zip``); otherwise we click the single
    ``Download (to hard disc)`` button. We never rely on counting individual
    buttons to decide "multi".

    Returns ``(files_written, had_attachments, all_ok)``:
      * ``had_attachments`` is True when a control was present,
      * ``all_ok`` is True when every expected attachment was obtained (or there
        were none). The caller uses ``all_ok`` to retry the email on a later run
        if attachments failed to download.
    """
    if indiv is _ATTACH_SCAN or zipbtn is _ATTACH_SCAN:
        indiv, zipbtn = await _find_download_targets()
    had_attachments = bool(indiv) or (zipbtn is not None)
    if not had_attachments:
        return 0, False, True

    dest = os.path.join(ATTACH_DIR, name_prefix)
    os.makedirs(dest, exist_ok=True)

    # Idempotent re-run: if attachment files already sit in the target dir
    # (from a prior export), don't re-download them. A genuinely failed download
    # leaves the dir empty, so it is still retried below.
    existing = [f for f in os.listdir(dest)
                if os.path.isfile(os.path.join(dest, f)) and os.path.getsize(
                    os.path.join(dest, f)) > 0]
    if existing:
        return len(existing), True, True

    # ── Multi-attachment: the ZIP button ──
    if zipbtn is not None:
        print(f"    [attach] 2+ attachments → 'Download all files (ZIP)' [{zipbtn}]")
        name = await _download_one(zipbtn, dest, set())
        zip_path = os.path.join(dest, name) if name else None
        # On a re-run the downloaded zip may already have been renamed to
        # <name_prefix>.zip; reuse it instead of re-downloading.
        if not (zip_path and os.path.exists(zip_path)):
            alt = os.path.join(dest, name_prefix + '.zip')
            if os.path.exists(alt):
                zip_path = alt
        if not zip_path or not os.path.exists(zip_path):
            print(f"    [attach] FAILED: ZIP download produced no file "
                  f"(button [{zipbtn}])")
            return 0, True, False
        try:
            import zipfile
            with zipfile.ZipFile(zip_path) as z:
                members = [i for i in z.infolist() if not i.is_dir()]
                written = present = 0
                for info in members:
                    base = (_sanitize(os.path.basename(info.filename), maxlen=160)
                            or 'attachment')
                    target = os.path.join(dest, base)
                    if os.path.exists(target):
                        present += 1
                        continue
                    try:
                        data = z.read(info)
                    except (zipfile.BadZipFile, OSError):
                        continue
                    with open(target, 'wb') as o:
                        o.write(data)
                    written += 1
                    print(f"    [attach] {base} (from zip)")
                # Keep the zip, renamed to match the email, for archival.
                zip_dest = os.path.join(dest, name_prefix + '.zip')
                if os.path.abspath(zip_path) != os.path.abspath(zip_dest):
                    if os.path.exists(zip_dest):
                        try:
                            os.remove(zip_dest)
                        except OSError:
                            pass
                    try:
                        os.rename(zip_path, zip_dest)
                    except OSError:
                        pass
            # all_ok only when every member was obtained (written or already present)
            all_ok = len(members) > 0 and (written + present) >= len(members)
            return written, True, all_ok
        except (zipfile.BadZipFile, OSError) as e:
            print(f"    [attach] unzip failed: {e}")
            return 0, True, False

    # ── Single attachment: the 'Download (to hard disc)' button ──
    ok = True
    written = 0
    for idx in indiv:
        print(f"    [attach] 1 attachment → 'Download (to hard disc)' [{idx}]")
        name = await _download_one(idx, dest, set())
        if name is None:
            name = await _download_one(idx, dest, set())  # one retry
        if name:
            written += 1
            print(f"    [attach] {name}")
        else:
            ok = False
            print(f"    [attach] FAILED: single attachment 'Download (to hard disc)' "
                  f"[{idx}] produced no file (opened inline / blocked?)")
    return written, True, ok

def _party_from_eml(eml_path: str) -> str:
    """Return the filename party from the saved .eml's literal To (recipient) field.

    The user wants the recipient shown literally, even when it is the local
    mailbox. Multiple recipients collapse to 'multiple'.
    """
    import email
    from email.utils import getaddresses
    with open(eml_path, 'rb') as f:
        msg = email.message_from_binary_file(f)
    tos = [a for _, a in getaddresses(msg.get_all('To', []))]
    if len(tos) > 1:
        return 'multiple'
    if tos:
        return _sanitize(tos[0])
    froms = [a for _, a in getaddresses(msg.get_all('From', []))]
    if froms:
        return _sanitize(froms[0])
    return _sanitize(SEARCH_TERM)


async def _party_from_page() -> str:
    """Best-effort recipient (party) read from the open email's detail view.

    Used as a stable fallback when the .eml itself failed to download, so a
    partial export still gets a consistent filename when retried later. Returns
    'multiple' for several recipients, else the sanitized email address.
    """
    s, out = await run(r"""
import re
state = await browser.get_state_as_text()
party = ""
for l in state.splitlines():
    if re.search(r'(?i)\bto\b', l) and '@' in l:
        m = re.search(r'([\w.+-]+@[\w.-]+\.[\w.-]+)', l)
        if m:
            party = m.group(1)
            break
print('PARTY||' + party)
""")
    party = ""
    for ln in out.split('\n'):
        if ln.startswith('PARTY||'):
            party = ln[7:].strip()
            break
    if not party:
        return _sanitize(SEARCH_TERM)
    if party.count('@') > 1:
        return 'multiple'
    return _sanitize(party, maxlen=200)


def _extract_attachments(eml_path: str, dest_dir: str) -> int:
    """Decode and write every attachment part of the .eml into dest_dir.

    Returns the number of attachments written.
    """
    import email
    with open(eml_path, 'rb') as f:
        msg = email.message_from_binary_file(f)
    count = 0
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        fn = part.get_filename()
        if not fn:
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        os.makedirs(dest_dir, exist_ok=True)
        safe = _sanitize(fn, maxlen=120)
        full = os.path.join(dest_dir, safe)
        if os.path.exists(full):
            base, ext = os.path.splitext(safe)
            i = 1
            while os.path.exists(os.path.join(dest_dir, f"{base}_{i}{ext}")):
                i += 1
            full = os.path.join(dest_dir, f"{base}_{i}{ext}")
        with open(full, 'wb') as o:
            o.write(data)
        count += 1
    return count


def _message_id_of(eml_path: str) -> str | None:
    """Return the email's Message-ID header, or None."""
    import email as _e
    try:
        with open(eml_path, 'rb') as f:
            msg = _e.message_from_binary_file(f)
        mid = (msg.get('Message-ID') or '').strip()
        return mid or None
    except Exception:
        return None


def _find_existing_by_msgid(msgid: str, exclude_prefix: str | None = None) -> str | None:
    """Return the on-disk prefix of an already-saved .eml sharing *msgid*, if any.

    Prefers a COMPLETE copy (has .html) over a partial one, so a retry of a
    misnamed/broken partial reconciles onto the good file instead of creating a
    second, differently-named copy of the same email."""
    if not msgid or not os.path.isdir(EML_DIR):
        return None
    best = None
    for f in os.listdir(EML_DIR):
        if not f.lower().endswith('.eml'):
            continue
        prefix = f[:-4]
        if prefix == exclude_prefix:
            continue
        if _message_id_of(os.path.join(EML_DIR, f)) == msgid:
            if best is None:
                best = prefix
            if os.path.exists(os.path.join(HTML_DIR, prefix + '.html')):
                return prefix
    return best


def _remove_email_artifacts(prefix: str) -> None:
    """Delete the .eml, .html and attachments/ dir for *prefix* (best-effort)."""
    for fp in (os.path.join(EML_DIR, prefix + '.eml'),
               os.path.join(EML_DIR, prefix + '.htm'),
               os.path.join(HTML_DIR, prefix + '.html'),
               os.path.join(HTML_DIR, prefix + '.htm')):
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
    att_dir = os.path.join(ATTACH_DIR, prefix)
    if os.path.isdir(att_dir):
        import shutil
        try:
            shutil.rmtree(att_dir)
        except OSError:
            pass


def _convert_datetime(dt: str) -> str | None:
    """Normalise a ``DATE_TIME`` token to ``yymmdd_HH.MM`` (compact date, 24-hour
    clock, no AM/PM). Accepts the legacy ``MM.DD.YYYY_HH.MMAM`` and the
    already date-migrated ``yymmdd_HH.MMAM`` styles. Returns None if it can't be
    parsed (e.g. ``nodate``), so callers can pass such tokens through unchanged."""
    if '_' not in dt:
        return None
    datepart, timepart = dt.split('_', 1)
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', datepart):
        mo, da, yyyy = datepart.split('.')
        ymd = f"{yyyy[-2:]}{mo.zfill(2)}{da.zfill(2)}"
    elif re.match(r'^\d{6}$', datepart):
        ymd = datepart
    else:
        return None
    m = re.match(r'^(\d{1,2})\.(\d{2})\s*(AM|PM)?$', timepart.strip(), re.I)
    if not m:
        return None
    hh = int(m.group(1))
    mm = m.group(2)
    ap = m.group(3)
    if ap:
        ap = ap.upper()
        if ap == 'PM' and hh != 12:
            hh += 12
        elif ap == 'AM' and hh == 12:
            hh = 0
    return f"{ymd}_{hh:02d}.{mm}"


def _reformat_base(base: str) -> str:
    """Convert a filename prefix to the canonical ``party_yymmdd_HH.MM_subject``
    style (compact date, 24-hour clock). Splits off the leading party, then the
    ``DATE_TIME`` token (date_ time, exactly one underscore) and the remaining
    subject (which may itself contain underscores). Unparseable prefixes (e.g.
    ``nodate_...``) pass through unchanged. Idempotent."""
    if '_' not in base:
        return base
    party, rest = base.split('_', 1)
    if '_' not in rest:
        return base
    if rest.count('_') >= 2:
        datepart, timepart, subject = rest.split('_', 2)
    else:
        datepart, timepart, subject = rest, '', ''
    new_dt = _convert_datetime(f"{datepart}_{timepart}")
    if new_dt is None:
        return base
    if subject:
        return f"{party}_{new_dt}_{subject}"
    return f"{party}_{new_dt}"


def _to_yymmdd(date_str: str) -> str:
    """Reformat a ``MM.DD.YYYY_HH.MMAM`` date string to the canonical
    ``yymmdd_HH.MM`` (24-hour) style. Non-matching values pass through unchanged."""
    return _convert_datetime(date_str) or date_str


def reconcile_duplicate_html() -> int:
    """Remove .html files that are byte-identical to a COMPLETE email's .html
    (an .eml with the same base exists) but have no .eml of their own.

    These are redundant stale copies left by early runs with buggy naming (e.g.
    a wrong party/date prefix). Deleting them leaves the genuine complete file
    untouched and clears a false PARTIAL. Returns the number removed."""
    import hashlib
    if not (os.path.isdir(HTML_DIR) and os.path.isdir(EML_DIR)):
        return 0

    def _hash(p):
        try:
            h = hashlib.md5()
            with open(p, 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    # Hashes of .html files that already belong to a complete (eml+html) pair.
    complete_hashes: set[str] = set()
    for f in os.listdir(HTML_DIR):
        if not f.lower().endswith(('.html', '.htm')):
            continue
        base = os.path.splitext(f)[0]
        if os.path.exists(os.path.join(EML_DIR, base + '.eml')):
            h = _hash(os.path.join(HTML_DIR, f))
            if h:
                complete_hashes.add(h)

    removed = 0
    for f in os.listdir(HTML_DIR):
        if not f.lower().endswith(('.html', '.htm')):
            continue
        base = os.path.splitext(f)[0]
        if os.path.exists(os.path.join(EML_DIR, base + '.eml')):
            continue  # it's a real (maybe partial) email's own html
        h = _hash(os.path.join(HTML_DIR, f))
        if h and h in complete_hashes:
            try:
                os.remove(os.path.join(HTML_DIR, f))
                removed += 1
                print(f"  Removed redundant duplicate .html: {f}")
            except OSError:
                pass
    return removed


def migrate_date_format(dry_run: bool = False) -> int:
    """Rename every on-disk artifact from the legacy ``MM.DD.YYYY_HH.MMAM`` date
    style to the compact ``yymmdd_HH.MMAM`` style, keeping the .eml, .html,
    attachments folder and .zip in lock-step. Idempotent: already-migrated names
    are left alone. Also rewrites the affected prefixes in progress.json
    (exported_map / partials) so id-based skip tracking keeps working.

    Returns the number of prefixes whose name changes (0 if nothing to do, or
    the number that *would* change on a dry run)."""
    eml_bases: set[str] = set()
    if os.path.isdir(EML_DIR):
        for f in os.listdir(EML_DIR):
            if f.lower().endswith('.eml'):
                eml_bases.add(os.path.splitext(f)[0])
    html_bases: set[str] = set()
    if os.path.isdir(HTML_DIR):
        for f in os.listdir(HTML_DIR):
            if f.lower().endswith(('.html', '.htm')):
                html_bases.add(os.path.splitext(f)[0])
    att_bases: set[str] = set()
    if os.path.isdir(ATTACH_DIR):
        for d in os.listdir(ATTACH_DIR):
            if os.path.isdir(os.path.join(ATTACH_DIR, d)):
                att_bases.add(d)

    mapping: dict[str, str] = {}
    for b in (eml_bases | html_bases | att_bases):
        nb = _reformat_base(b)
        if nb != b:
            mapping[b] = nb
    if not mapping:
        return 0

    # Guard against collisions (two old bases collapsing to one new base).
    rev: dict[str, list[str]] = {}
    for old, new in mapping.items():
        rev.setdefault(new, []).append(old)
    collisions = {n: o for n, o in rev.items() if len(o) > 1}
    if collisions:
        for new, olds in collisions.items():
            print(f"  MIGRATION COLLISION -> {new}: {olds}")
        print("  Aborting migration due to collisions.")
        return 0

    if dry_run:
        return len(mapping)

    renamed = 0
    for old, new in mapping.items():
        for ext in ('.eml',):
            op = os.path.join(EML_DIR, old + ext)
            if os.path.exists(op) and not os.path.exists(os.path.join(EML_DIR, new + ext)):
                os.rename(op, os.path.join(EML_DIR, new + ext))
                renamed += 1
        for ext in ('.html', '.htm'):
            op = os.path.join(HTML_DIR, old + ext)
            if os.path.exists(op) and not os.path.exists(os.path.join(HTML_DIR, new + ext)):
                os.rename(op, os.path.join(HTML_DIR, new + ext))
                renamed += 1
        od = os.path.join(ATTACH_DIR, old)
        if os.path.isdir(od) and not os.path.exists(os.path.join(ATTACH_DIR, new)):
            os.rename(od, os.path.join(ATTACH_DIR, new))
            renamed += 1
            nd = os.path.join(ATTACH_DIR, new)
            for zf in os.listdir(nd):
                if zf.lower().endswith('.zip') and os.path.splitext(zf)[0] == old \
                        and not os.path.exists(os.path.join(nd, new + '.zip')):
                    try:
                        os.rename(os.path.join(nd, zf),
                                  os.path.join(nd, new + '.zip'))
                    except OSError:
                        pass

    prog = load_progress()
    em = prog.get("exported_map", {})
    if isinstance(em, dict):
        for k in list(em):
            if em[k] in mapping:
                em[k] = mapping[em[k]]
        prog["exported_map"] = em
    parts = prog.get("partials", [])
    if isinstance(parts, list):
        prog["partials"] = [mapping.get(p, p) for p in parts]
    save_progress(prog)
    return renamed


def reconcile_duplicates() -> int:
    """Remove redundant Message-ID duplicate .eml files, keeping one good copy.

    mail.com has no stable per-message id in the UI, and the on-disk filename is
    derived from fragile page-state, so the same email can be saved twice under
    different (often misnamed) prefixes. Scan eml/ for Message-ID groups; for
    each group keep the best copy (a COMPLETE one whose name is not 'no-subject'
    / 'Search options' / 'nodate') and delete the rest. Returns the number of
    redundant files removed. Also drops removed prefixes from progress partials."""
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    if os.path.isdir(EML_DIR):
        for f in os.listdir(EML_DIR):
            if not f.lower().endswith('.eml'):
                continue
            mid = _message_id_of(os.path.join(EML_DIR, f))
            if mid:
                groups[mid].append(f[:-4])

    removed = 0
    prog = load_progress()
    partials = prog.get("partials", [])
    for mid, prefixes in groups.items():
        if len(prefixes) < 2:
            continue
        complete = [p for p in prefixes
                    if os.path.exists(os.path.join(HTML_DIR, p + '.html'))]
        def _good(p):
            return not ('no-subject' in p or 'Search options' in p
                        or p.startswith('nodate'))
        if complete:
            keeper = next((p for p in complete if _good(p)), complete[0])
        else:
            keeper = prefixes[0]  # none complete; keep one to retry later
        for p in prefixes:
            if p == keeper:
                continue
            _remove_email_artifacts(p)
            removed += 1
            if p in partials:
                partials.remove(p)
    if removed:
        prog["partials"] = partials
        save_progress(prog)
    return removed


def reconcile_names() -> int:
    """Make every email's four on-disk artifacts share one exact prefix.

    The script saves .eml, .html, the attachments/ folder and the .zip all from
    a single ``name_prefix``, but EARLY runs saved the .html (and occasionally the
    attachment folder) with a divergent name (e.g. a trailing space left in the
    browser's raw .html name). That breaks the pairing and makes a complete email
    look like a PARTIAL. Re-align each .html / attachment folder / .zip onto the
    .eml's exact base (paired by normalized name or Message-ID). Returns the
    number of artifacts realigned. The .eml name is treated as canonical (renaming
    it would disturb the idempotent skip keys)."""
    import re as _re

    def _norm(s):
        return ' '.join(_re.sub(r'\s+', ' ', s.replace('\xa0', ' ')).split())

    eml_norm = {}
    eml_msgid = {}
    for f in os.listdir(EML_DIR):
        if f.lower().endswith('.eml'):
            b = f[:-4]
            eml_norm.setdefault(_norm(b), b)
            mid = _message_id_of(os.path.join(EML_DIR, f))
            if mid:
                eml_msgid.setdefault(mid, b)
    aligned = 0

    # 1) .html files -> match the .eml base.
    for f in os.listdir(HTML_DIR):
        low = f.lower()
        if not low.endswith(('.html', '.htm')):
            continue
        ext = '.htm' if low.endswith('.htm') else '.html'
        base = f[:-4]
        target = eml_norm.get(_norm(base))
        if target is None and _message_id_of(os.path.join(HTML_DIR, f)) in eml_msgid:
            target = eml_msgid[_message_id_of(os.path.join(HTML_DIR, f))]
        if target and target != base:
            dst = os.path.join(HTML_DIR, target + ext)
            if not os.path.exists(dst):
                try:
                    os.rename(os.path.join(HTML_DIR, f), dst)
                    aligned += 1
                except OSError:
                    pass

    # 2) attachment folders -> match the .eml base.
    if os.path.isdir(ATTACH_DIR):
        for d in os.listdir(ATTACH_DIR):
            dp = os.path.join(ATTACH_DIR, d)
            if not os.path.isdir(dp):
                continue
            target = eml_norm.get(_norm(d))
            if target and target != d:
                dst = os.path.join(ATTACH_DIR, target)
                if not os.path.exists(dst):
                    try:
                        os.rename(dp, dst)
                        aligned += 1
                    except OSError:
                        pass

    # 3) .zip inside attachment folders -> match the folder name.
    if os.path.isdir(ATTACH_DIR):
        for d in os.listdir(ATTACH_DIR):
            dp = os.path.join(ATTACH_DIR, d)
            if not os.path.isdir(dp):
                continue
            for f in os.listdir(dp):
                if f.lower().endswith('.zip'):
                    zbase = f[:-4]
                    if zbase != d:
                        dst = os.path.join(dp, d + '.zip')
                        if not os.path.exists(dst):
                            try:
                                os.rename(os.path.join(dp, f), dst)
                                aligned += 1
                            except OSError:
                                pass
    return aligned


def _is_done_on_disk(eid: str, exported_map: dict) -> bool:
    """True only if BOTH .eml and .html for this email actually exist on disk.

    Used so a re-run never skips an email whose id was recorded as exported but
    whose file is missing/collided (a real partial)."""
    prefix = exported_map.get(eid)
    if not prefix:
        return False
    return (os.path.exists(os.path.join(EML_DIR, prefix + '.eml')) and
            os.path.exists(os.path.join(HTML_DIR, prefix + '.html')))


def _resolve_prefix(name_prefix: str, eml_path: str | None) -> str:
    """Return the filename prefix to use, disambiguating when a DIFFERENT email
    already occupies ``<name_prefix>`` (same derived name, different content).

    On an idempotent re-run the existing file has the same size as the new one,
    so we reuse the name (no drift). Only a genuine collision (different size)
    gets a ``_N`` suffix, keeping the two emails' files distinct."""
    dest = os.path.join(EML_DIR, f"{name_prefix}.eml")
    if eml_path and os.path.exists(dest) and os.path.getsize(dest) != os.path.getsize(eml_path):
        i = 1
        while (os.path.exists(os.path.join(EML_DIR, f"{name_prefix}_{i}.eml")) or
               os.path.exists(os.path.join(HTML_DIR, f"{name_prefix}_{i}.html"))):
            i += 1
        return f"{name_prefix}_{i}"
    return name_prefix


def _existing_prefix_for(date: str, subject: str):
    """Find an on-disk filename prefix that already corresponds to (date, subject).

    The party part of the name has changed across script versions, so we match on
    the STABLE ``_<date>_<subject>`` suffix rather than the full name. Returns the
    prefix (with whatever party spelling is on disk) if a matching ``.eml`` OR
    ``.html`` exists, else None. The caller checks which formats are present to
    decide between "skip" (both present) and "complete in place" (only one).

    A genuine collision (two different emails sharing the same date+subject, with
    mismatched eml/html names) returns None so the caller re-exports and lets
    ``_resolve_prefix`` mint a ``_N`` disambiguated name.
    """
    suffix = f"_{date}_{subject}"
    eml_base = html_base = None
    if os.path.isdir(EML_DIR):
        for f in os.listdir(EML_DIR):
            if f.lower().endswith('.eml') and f[:-4].endswith(suffix):
                eml_base = f[:-4]
                break
    if os.path.isdir(HTML_DIR):
        for f in os.listdir(HTML_DIR):
            if f.lower().endswith(('.html', '.htm')) and f.rsplit('.', 1)[0].endswith(suffix):
                html_base = f.rsplit('.', 1)[0]
                break
    if eml_base and html_base and eml_base != html_base:
        return None  # collision -> re-export with disambiguation
    return eml_base or html_base


async def _poll_and_scan() -> tuple[str, str, int | None, int | None, list[int], int | None]:
    """One daemon call: poll for detail view ready, then extract everything.

    Returns ``(date, subject, act_index, next_btn_idx, attach_indiv, attach_zip)``.
    ``act_index`` / ``next_btn_idx`` / ``attach_zip`` are ``None`` when absent.
    Sets ``_SESSION_LOST = True`` if extraction sees "has no attribute".
    """
    POLLS, SLEEP = 12, OPEN_POLL_SLEEP
    code = f'''
import re, asyncio
for _ in range({POLLS}):
    state = await browser.get_state_as_text()
    lines = state.splitlines()
    date = ""
    for l in lines:
        m = re.search(r'(\\d{{1,2}}/\\d{{1,2}}/\\d{{4}})\\s*-\\s*(\\d{{1,2}}:\\d{{2}}\\s*(?:AM|PM))', l)
        if m:
            date = m.group(1).replace('/','.') + '_' + m.group(2).replace(':','.').replace(' ','')
            break
    if not date:
        await asyncio.sleep({SLEEP})
        continue
    subject = ""
    for i, l in enumerate(lines):
        if 'detail-favorite-marker' in l:
            for j in range(i-1, -1, -1):
                text = lines[j].strip()
                if text and not text.startswith('[') and not text.startswith('*') \\
                   and not text.startswith('|') and 'Shadow' not in text \\
                   and 'mail-list-container' not in text:
                    subject = text
                    break
            break
    act = ""
    for l in lines:
        if 'Show further actions' in l:
            m = re.search(r'\\[(\\d+)\\]', l)
            if m: act = m.group(1); break
    next_btn = ""
    for l in lines:
        if 'title=next email' in l.lower():
            m = list(re.finditer(r'\\[(\\d+)\\]', l))
            if m: next_btn = m[-1].group(1); break
    indiv = []
    zipbtn = ""
    for l in lines:
        ll = l.lower()
        if 'download (to hard disc)' in ll:
            m = list(re.finditer(r'\\[(\\d+)\\]', l))
            if m: indiv.append(int(m[-1].group(1)))
        elif 'download all files' in ll:
            m = list(re.finditer(r'\\[(\\d+)\\]', l))
            if m and not zipbtn: zipbtn = m[-1].group(1)
    _markers = sum(1 for l in lines if 'detail-favorite-marker' in l or 'Show further actions' in l)
    print(f"DT||{{date}}||SUBJ||{{subject}}||ACT||{{act}}||NEXT||{{next_btn}}||ATTACH||{{repr((indiv, zipbtn))}}||MARKERS||{{_markers}}")
    break
else:
    _markers = sum(1 for l in lines if 'detail-favorite-marker' in l or 'Show further actions' in l)
    print(f"DT|| ||SUBJ|| ||ACT|| ||NEXT|| ||ATTACH||([], '')||MARKERS||{{_markers}}")
'''
    s, out = await run(code)
    if not s:
        return ("", "", None, None, [], None)
    date = subject = ""
    act_index = next_btn_idx = None
    attach_indiv: list[int] = []
    attach_zip: int | None = None
    global _ON_EMAIL_PAGE
    _ON_EMAIL_PAGE = True
    for ln in out.split('\n'):
        ln = ln.strip()
        parts = ln.split('||')
        for i, p in enumerate(parts):
            if p == 'DT' and i + 1 < len(parts):
                date = parts[i + 1]
            elif p == 'SUBJ' and i + 1 < len(parts):
                subject = parts[i + 1]
            elif p == 'ACT' and i + 1 < len(parts):
                v = parts[i + 1]
                act_index = int(v) if v.isdigit() else None
            elif p == 'NEXT' and i + 1 < len(parts):
                v = parts[i + 1]
                next_btn_idx = int(v) if v.isdigit() else None
            elif p == 'ATTACH' and i + 1 < len(parts):
                try:
                    iv, zv = eval(parts[i + 1])
                    attach_indiv = list(iv) if iv else []
                    attach_zip = int(zv) if str(zv).isdigit() else None
                except Exception:
                    pass
            elif p == 'MARKERS' and i + 1 < len(parts):
                _ON_EMAIL_PAGE = parts[i + 1].strip() != '0'
    date = _to_yymmdd(_sanitize(date) or 'nodate')
    subject = _sanitize(subject) or 'no-subject'
    return date, subject, act_index, next_btn_idx, attach_indiv, attach_zip


async def export_single_email() -> tuple[bool, bool, str, bool, int | None, dict]:
    """Export .html and .eml for the currently open email detail view.

    Returns ``(saved_html, saved_eml, name_prefix, att_ok, next_btn_idx, diag)``.
    ``next_btn_idx`` is the index of the ``Next email`` button for this email's
    detail view (``None`` if not found). One format missing => a PARTIAL (retry
    later), not a full success. ``diag`` is a small dict describing WHY an export
    failed (used by the end-of-run diagnostics / unblacklist helper).
    """
    diag: dict = {"reason": "ok", "on_email_page": _ON_EMAIL_PAGE,
                  "act_index": None, "act_found": False,
                  "html_click_ok": None, "eml_click_ok": None,
                  "html_file_saved": None, "eml_file_saved": None,
                  "date": "", "subject": ""}
    # ── 1) Poll detail ready + extract + scan in one daemon call ──
    with _perf_phase("email.extract"):
        date, subject, act_index, next_btn_idx, attach_indiv, attach_zip = \
            await _poll_and_scan()
    diag["act_index"] = act_index
    diag["act_found"] = act_index is not None
    diag["date"] = date
    diag["subject"] = subject

    if not _ON_EMAIL_PAGE:
        print(f"    [export] not on email page (wrong tab?), skipping save")
        diag["reason"] = "WRONG_PAGE"
        return (False, False, f"WRONG_PAGE", False, next_btn_idx, diag)

    # ── Idempotent skip ──
    name_prefix = None
    _existing = _existing_prefix_for(date, subject)
    if _existing is not None:
        _eml = os.path.join(EML_DIR, _existing + '.eml')
        _htm = os.path.join(HTML_DIR, _existing + '.html')
        if os.path.exists(_eml) and os.path.exists(_htm):
            print(f"    [export] already complete on disk, skipping: {_existing}")
            return (True, True, _existing, True, next_btn_idx, diag)
        name_prefix = _existing

    # ── 2) Both saves in one daemon call, retry up to 3x with adaptive delay ──
    # Starts at _SAVE_DELAY (last successful delay from prior emails, or 0.1).
    # Bumps +0.1 per miss; on success, records the working delay as _SAVE_DELAY
    # so the next email picks up from there.
    global _SAVE_DELAY
    html_path = eml_path = None
    _delay = _SAVE_DELAY
    with _perf_phase("email.save") as pctx_save:
        for attempt in range(1, 4):
            before = _dir_snapshot()
            ok1, ok2 = await _click_both_saves(act_index, _delay)
            html_fut = (asyncio.ensure_future(
                _wait_for_new_file(before, '.html', timeout=DOWNLOAD_WAIT_TIMEOUT))
                        if ok1 else None)
            eml_fut = (asyncio.ensure_future(
                _wait_for_new_file(before, '.eml', timeout=DOWNLOAD_WAIT_TIMEOUT))
                       if ok2 else None)
            html_path = eml_path = None
            if html_fut:
                _f = await html_fut
                html_path = os.path.join(STAGE_DIR, _f) if _f else None
            if eml_fut:
                _f = await eml_fut
                eml_path = os.path.join(STAGE_DIR, _f) if _f else None
            if html_path and eml_path:
                break
            if not eml_path:
                _delay = min(_delay + 0.1, 1.0)
                print(f"    [save] .eml missed, bumped delay to {_delay:.1f}s")
            if html_path or eml_path:
                continue
            await asyncio.sleep(SETTLE_SLEEP)
        pctx_save.ok = bool(html_path and eml_path)
        diag["html_click_ok"] = ok1
        diag["eml_click_ok"] = ok2
        diag["html_file_saved"] = bool(html_path)
        diag["eml_file_saved"] = bool(eml_path)
        if html_path and eml_path:
            diag["reason"] = "ok"
        elif html_path:
            diag["reason"] = "eml_missing"
        elif eml_path:
            diag["reason"] = "html_missing"
        else:
            diag["reason"] = "neither_downloaded"

    if not html_path and not eml_path:
        _SAVE_DELAY = min(_SAVE_DELAY + 0.1, 1.0)
        print(f"    [export] neither .html nor .eml downloaded")
        return (False, False, f"{_sanitize(SEARCH_TERM)}_{date}_{subject}",
                False, next_btn_idx, diag)
    if eml_path:
        _SAVE_DELAY = _delay
    else:
        _SAVE_DELAY = min(_SAVE_DELAY + 0.1, 1.0)

    # ── 2.5) Content-based dedup ──
    if eml_path:
        msgid = _message_id_of(eml_path)
        if msgid:
            dup = _find_existing_by_msgid(msgid, exclude_prefix=name_prefix)
            if dup is not None and dup != name_prefix:
                existing_html = os.path.exists(
                    os.path.join(HTML_DIR, dup + '.html'))
                dup_att_dir = os.path.join(ATTACH_DIR, dup)
                existing_att = (os.path.isdir(dup_att_dir) and
                                any(os.path.isfile(os.path.join(dup_att_dir, x))
                                    for x in os.listdir(dup_att_dir)))
                if existing_html:
                    for p in (html_path, eml_path):
                        if p and os.path.exists(p):
                            try: os.remove(p)
                            except OSError: pass
                    print(f"    [export] duplicate of existing {dup} (msgid)")
                    return (True, True, dup, True, next_btn_idx, diag)
                saved_html = False
                if html_path:
                    os.makedirs(HTML_DIR, exist_ok=True)
                    os.rename(html_path, os.path.join(HTML_DIR, dup + '.html'))
                    saved_html = True
                if eml_path and os.path.exists(eml_path):
                    try: os.remove(eml_path)
                    except OSError: pass
                print(f"    [export] duplicate of partial {dup} (msgid)")
                return (saved_html, True, dup, existing_att, next_btn_idx, diag)
        for f in os.listdir(EML_DIR):
            if not f.lower().endswith('.eml'): continue
            p = f[:-4]
            if p == (name_prefix or '') or (dup is not None and p == dup): continue
            if _message_id_of(os.path.join(EML_DIR, f)) == msgid:
                _remove_email_artifacts(p)

    # ── 3) Party ──
    if name_prefix is None:
        if eml_path:
            party = _party_from_eml(eml_path)
        else:
            party = await _party_from_page()
        name_prefix = f"{party}_{date}_{subject}"
        name_prefix = _resolve_prefix(name_prefix, eml_path)

    saved_html = saved_eml = False
    if html_path:
        os.makedirs(HTML_DIR, exist_ok=True)
        os.rename(html_path, os.path.join(HTML_DIR, f"{name_prefix}.html"))
        saved_html = True
    else:
        print(f"    [export] .html not downloaded")

    # ── 3.5) Attachments (pre-found indices from _poll_and_scan) ──
    with _perf_phase("email.attach") as pctx_att:
        att_count, had_att, att_ok = await _download_ui_attachments(
            name_prefix, indiv=attach_indiv, zipbtn=attach_zip)
        if att_count == 0 and eml_path:
            att_count = _extract_attachments(eml_path,
                                             os.path.join(ATTACH_DIR, name_prefix))
            if att_count: att_ok = True
        pctx_att.ok = att_ok
    if att_count:
        print(f"    [export] {att_count} attachment(s)")

    if eml_path:
        os.makedirs(EML_DIR, exist_ok=True)
        os.rename(eml_path, os.path.join(EML_DIR, f"{name_prefix}.eml"))
        saved_eml = True
    else:
        print(f"    [export] .eml not downloaded")

    return (saved_html, saved_eml, name_prefix, att_ok, next_btn_idx, diag)


async def _write_diagnostics(eid: str, diag: dict, fail_count: int) -> None:
    """Persist a failure diagnostic for a retry-pending email so the user can see
    *why* it failed (and whether it's a connection vs zoom/viewport issue).

    Writes ``<run>/diagnostics/<eid>.json`` (structured) and ``<eid>.ax.txt``
    (a trimmed accessibility-tree snapshot of the detail view, one extra daemon
    RTT — only called when an email is marked retry-pending).
    """
    import json as _json
    ddir = os.path.join(PROGRESS_DIR, "diagnostics")
    try:
        os.makedirs(ddir, exist_ok=True)
    except OSError:
        return
    payload = {"eid": eid, "fail_count": fail_count,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "diag": diag}
    try:
        with open(os.path.join(ddir, f"{eid}.json"), "w") as f:
            _json.dump(payload, f, indent=2)
    except OSError:
        pass
    try:
        _, out = await run("st = await browser.get_state_as_text(); print(st)")
        snap = out[:4000]
        with open(os.path.join(ddir, f"{eid}.ax.txt"), "w") as f:
            f.write(snap)
    except Exception:
        pass


async def _open_email(idx: int, expect_eid: str | None = None) -> str | None:
    """Click list item [idx] and verify the detail view actually opened (not a
    stale settings panel). Returns ``expect_eid`` on success, or None if it never
    opened.

    IMPORTANT: the opened email's unique id is NOT available in the detail
    accessibility tree — it lives inside the cross-origin ``detail-body-iframe``
    and ``get_state_as_text`` does not surface it (verified: detail state has no
    ``id=id…`` and no bare 18-19 digit eid). So we CANNOT confirm "this is email
    <eid>" from the detail view. Instead we trust the row we clicked (its eid was
    read from the list, which is authoritative) and only guard against a non-email
    panel opening instead of the detail (via the ``detail-body-iframe`` / Reply+
    Forward / detail-* markers).
    """
    for attempt in range(1, 4):
        await run(f"print(await click({idx}))")
        for _ in range(12):
            s, out = await run(
                "st=await browser.get_state_as_text(); "
                "import re; "
                "det = ('detail-body-iframe' in st) "
                "or ('detail-date-label' in st) "
                "or ('detail-favorite-marker' in st) "
                "or ('Reply' in st and 'Forward' in st); "
                "print('OPEN ' + ('1' if det else '0'))"
            )
            detail_opened = False
            for ln in out.split('\n'):
                ln = ln.strip()
                if ln.startswith('OPEN '):
                    detail_opened = ln[5:] == '1'
            if detail_opened:
                # Detail is open. We trust that clicking list row [idx] (whose
                # eid we resolved from the list) opened that email.
                return expect_eid or ''
            await asyncio.sleep(OPEN_POLL_SLEEP)
        # Detail never opened: dismiss and retry.
        await run("await send_keys('Escape')")
        await asyncio.sleep(OPEN_POLL_SLEEP)
    return None


async def _visible_index_of(eid: str) -> int | None:
    """Return the current on-screen index of the row id=id<eid> in the
    currently-rendered list window, or None if it is not visible.
    PAIRS format is ``idx:eid:folder`` (comma-separated)."""
    s, out = await run(_collect_code())
    npairs = 0
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('PAIRS '):
            body = ln[6:]
            if body:
                for tok in body.split(','):
                    if ':' in tok:
                        a, rest = tok.split(':', 1)
                        b = rest.split(':')[0]
                        npairs += 1
                        if b == eid and a.isdigit():
                            return int(a)
    if DEBUG:
        print(f"  [visible_index_of] eid={eid} not found among {npairs} pairs")
    return None


async def _open_by_eid(eid: str) -> str | None:
    """Open the email whose list row carries id=id<eid>.

    The mail list is virtualized and CANNOT be scrolled programmatically, but the
    compact "list-only" view renders EVERY row in a single snapshot. So we make
    sure we are on the (fully-visible) list, locate the row's current on-screen
    index, and open it. Returns the eid on success or None if it never opened.
    """
    # Ensure we're on the list (not a detail view) before locating the row. The
    # view mode persists across emails; "Back to email list" returns to the whole
    # list, so we only ever click back if the pane is actually collapsed.
    await _ensure_list_only()
    idx = await _visible_index_of(eid)
    if DEBUG:
        print(f"  [open_by_eid] eid={eid} -> visible idx={idx}")
    if idx is not None:
        res = await _open_email(idx, eid)
        if res:
            return res
    return None


# ---------------------------------------------------------------------------
# List-only view + navigation helpers
# ---------------------------------------------------------------------------
# The mail list renders rows in the accessibility tree proportional to viewport
# size. A larger viewport (= smaller zoom) exposes more rows. When total count
# is known (OPEXPORT_TOTAL) we enforce PAGE_SIZE rows per non-last page by
# adjusting the viewport and/or scrolling. See _ensure_page_row_count().


async def _count_list_rows() -> int:
    """Number of list rows currently rendered in the accessibility tree."""
    ok, out = await run(
        "state = await browser.get_state_as_text(); "
        "import re; "
        "n = len([l for l in state.splitlines() "
        "if 'list-mail-item' in l and 'id=id' in l]); "
        "print('NLIST', n)"
    )
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('NLIST'):
            try:
                return int(ln.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


PAGE_SIZE_ADJUST_MAX = 6

async def _ensure_page_row_count(expected: int, page_label: str) -> int:
    """Try to get at least ``expected`` list rows visible, adjusting viewport
    and/or scrolling until we reach it or exhaust attempts.

    Strategies (attempted in order per iteration):
      1. Repoll (maybe the list is still rendering)
      2. Scroll to bottom via End key (triggers lazy rendering)
      3. Enlarge viewport via ``_cdp_set_viewport`` (zoom-out effect)
      4. Wait and repoll again

    Returns the final count (which may be < expected after max attempts).
    """
    for attempt in range(1, PAGE_SIZE_ADJUST_MAX + 1):
        n = await _count_list_rows()
        if DEBUG:
            print(f"  [page {page_label} row adj] attempt {attempt}: "
                  f"count={n}, target={expected}")
        if n >= expected:
            return n
        if attempt == PAGE_SIZE_ADJUST_MAX:
            if n < expected:
                print(f"  [page {page_label} row adj] WARNING: only {n} rows "
                      f"visible after {PAGE_SIZE_ADJUST_MAX} attempts "
                      f"(expected {expected})")
            return n
        # Strategy 1: wait and repoll
        await asyncio.sleep(0.8)
        n2 = await _count_list_rows()
        if n2 > n:
            continue
        # Strategy 2: scroll to bottom
        await run("print(await send_keys('End'))")
        await asyncio.sleep(1.2)
        n3 = await _count_list_rows()
        if n3 > n2:
            continue
        # Strategy 3: enlarge viewport via CDP (_cdp_set_viewport scales
        # the browser window = smaller zoom = more rows fit).
        pg = await run(
            "p = await browser.get_current_page(); "
            "vp = await p.evaluate('() => ({w: window.innerWidth, h: window.innerHeight})'); "
            "print(vp)"
        )
        import json as _json
        try:
            vp = _json.loads(pg.strip())
            new_h = int(vp["h"] * 1.3)
            await run(
                f"await browser._cdp_set_viewport(width={vp['w']}, height={new_h})"
            )
            if DEBUG:
                print(f"  [page {page_label} row adj] viewport: "
                      f"{vp['w']}x{vp['h']} -> {vp['w']}x{new_h}")
        except (ValueError, KeyError, TypeError):
            pass
        await asyncio.sleep(1.0)
    return await _count_list_rows()


async def _find_change_view_idx() -> int | None:
    """Find the index of the list 'Change view' toggle (lenient match)."""
    ok, out = await run(
        "state = await browser.get_state_as_text(); "
        "import re; "
        "for l in state.splitlines():\n"
        "    if 'change view' in l.lower():\n"
        "        m = re.search(r'\\[(\\d+)\\]', l)\n"
        "        if m: print('CV:' + m.group(1)); break\n"
        "else: print('CV:none')"
    )
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('CV:'):
            v = ln[3:]
            return int(v) if v.isdigit() else None
    return None


async def _ensure_list_only() -> None:
    """Ensure the list pane is visible (rows present) so rows can be collected
    and clicked.

    mail.com keeps the current view mode across emails, and the in-app "Back to
    email list" button returns to the (whole) list -- so once we are on the list
    we stay there. We deliberately do NOT press "Change view" and do NOT re-run
    the search: if the pane is ever collapsed we simply click "Back to email
    list". Any non-zero row count is accepted (a small last page legitimately has
    fewer rows than a full page but is still fully rendered)."""
    for _ in range(4):
        n = await _count_list_rows()
        if DEBUG:
            print(f"  [list] n={n}")
        if n > 0:
            return
        await _back_to_list()
        await asyncio.sleep(1.5)


async def _back_to_list() -> None:
    """If we're on an opened email's detail view, return to the list.

    In list-only mode opening a row replaces the list with a full detail view,
    so after exporting we must go back before opening the next email."""
    idx = await find_element("title=Back to email list")
    if idx is not None:
        await run(f"print(await click({int(idx)}))")
        await asyncio.sleep(0.5)


async def _navigate_next(pos: int, total: int, next_btn: int | None = None) -> bool:
    """Click the pre-found ``Next email`` button, no scan or poll.

    ``next_btn`` is the index from ``_poll_and_scan()``.  Returns True if the
    click was sent, False if at end of page or no button index given.
    """
    if pos >= total - 1 or next_btn is None:
        return False
    await run(f"print(await click({int(next_btn)}))")
    return True


def _parse_detail_date(s: str):
    """Parse the detail-view date string 'MM/DD/YYYY - H:MM AM/PM' to epoch."""
    import re as _re
    from datetime import datetime as _dt
    m = _re.search(r'(\d{2})/(\d{2})/(\d{4}) - (\d{1,2}):(\d{2}) ([AP]M)', s)
    if not m:
        return None
    mo, da, yr, h, mi, ap = m.groups()
    h = int(h) % 12 + (12 if ap == 'PM' else 0)
    try:
        return _dt(int(yr), int(mo), int(da), h, int(mi)).timestamp()
    except ValueError:
        return None


def _parse_list_date(s: str):
    """Parse the list-view date string 'MM/DD/YY at H:MM AM/PM' to epoch."""
    import re as _re
    from datetime import datetime as _dt
    m = _re.search(r'(\d{2})/(\d{2})/(\d{2}) at (\d{1,2}):(\d{2}) ([AP]M)', s)
    if not m:
        return None
    mo, da, yy, h, mi, ap = m.groups()
    h = int(h) % 12 + (12 if ap == 'PM' else 0)
    try:
        return _dt(2000 + int(yy), int(mo), int(da), h, int(mi)).timestamp()
    except ValueError:
        return None


async def _detail_date_epoch() -> float | None:
    """Return the opened email's date (epoch) from the detail view, or None."""
    ok, out = await run(
        "state = await browser.get_state_as_text(); "
        "import re; "
        "m = re.search(r'\\d{2}/\\d{2}/\\d{4} - \\d{1,2}:\\d{2} [AP]M', state); "
        "print('DDATE', m.group(0) if m else 'NONE')"
    )
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('DDATE'):
            v = ln[6:]
            if v == 'NONE':
                return None
            return _parse_detail_date(v)
    return None


class _drafts_dirs:
    """Context manager: temporarily point EML_DIR/HTML_DIR/ATTACH_DIR at the
    Drafts sub-folders so export_single_email (and its helpers, which read these
    module globals) file drafts separately. Restores on exit."""

    def __enter__(self):
        global EML_DIR, HTML_DIR, ATTACH_DIR
        self._saved = (EML_DIR, HTML_DIR, ATTACH_DIR)
        EML_DIR, HTML_DIR, ATTACH_DIR = (DRAFTS_EML_DIR, DRAFTS_HTML_DIR,
                                          DRAFTS_ATT_DIR)

    def __exit__(self, *exc):
        global EML_DIR, HTML_DIR, ATTACH_DIR
        EML_DIR, HTML_DIR, ATTACH_DIR = self._saved


def _collect_code():
    # We are already on a results page (search results OR a folder like Drafts).
    # Collect ALL list items that carry a stable id=id… and detect each row's
    # folder. Drafts (and Inbox/Sent/Trash/Spam) are tagged per-row by a child
    # div, e.g. <div class="list-mail-item__folder ..." title="Drafts">Drafts</div>
    # which appears in the accessibility tree as a line containing 'title=Drafts'
    # (the sidebar folder nav also has title=Drafts but carries a "N/M" count, so
    # we exclude that). If no per-row tag is found we fall back to the list
    # container's type= attribute (e.g. type=DRAFTS). This lets us route Drafts to
    # a separate folder without guessing.
    return """
import re
state = await browser.get_state_as_text()
lines = state.splitlines()
folder_default = 'INBOX'
for l in lines:
    if 'webmailer-mail-list' in l:
        m = re.search(r'type=(\\w+)', l)
        if m: folder_default = m.group(1)
        break
FOLDERS = ['Inbox', 'Sent', 'Drafts', 'Trash', 'Spam']
seen = set()
pairs = []
total = 0
N = len(lines)
i = 0
while i < N:
    l = lines[i]
    if 'list-mail-item' in l and 'id=id' in l:
        total += 1
        m = re.search(r'\\[(\\d+)\\]', l)
        e = re.search(r'id=id(\\d+)', l)
        if m and e and e.group(1) not in seen:
            seen.add(e.group(1))
            folder = folder_default
            j = i + 1
            while j < N and 'list-mail-item' not in lines[j]:
                for f in FOLDERS:
                    if ('title=' + f) in lines[j] and not re.search(r'\\d+/\\d+', lines[j]):
                        folder = f.upper()
                        break
                if folder != folder_default:
                    break
                j += 1
            pairs.append((int(m.group(1)), e.group(1), folder))
    i += 1
print('FOLDER ' + folder_default)
print('TOTAL ' + str(total))
print('PAIRS ' + ','.join(f"{a}:{b}:{c}" for a, b, c in pairs))
"""


async def _collect_all_rows():
    """Return the full ordered list of (index, eid, folder) for every email on
    the current page.

    CRITICAL FACT ABOUT THE IDS (verified empirically): each list item is
    ``id=id<N>`` where ``N`` is the email's timestamp in EPOCH-NANOSECONDS.
    It is strictly monotonic and smaller N == older email. That is why
    chronological selection (``--select``) sorts rows by *int(eid)*.

    The mail list is virtualized and CANNOT be scrolled programmatically, but the
    compact "list-only" view (via the "Change view" button) renders EVERY row of
    the page in a single accessibility-tree snapshot. So we switch to list-only
    and take ONE snapshot — no scrolling, no guessing which window the extreme
    eids live in."""
    t0 = time.monotonic()
    await _ensure_list_only()
    _, out = await run(_collect_code())
    ordered: list[tuple[int, str, str]] = []
    folder = 'INBOX'
    for ln in out.split('\n'):
        ln = ln.strip()
        if ln.startswith('FOLDER '):
            folder = ln[7:]
        elif ln.startswith('PAIRS '):
            body = ln[6:]
            if body:
                for tok in body.split(','):
                    if ':' in tok:
                        a, rest = tok.split(':', 1)
                        b, folder = rest.split(':', 1)
                        if a.lstrip('-').isdigit():
                            ordered.append((int(a), b, folder))
    print(f"  [collect] gathered {len(ordered)} ids in "
          f"{time.monotonic() - t0:.1f}s (folder={folder})")
    return ordered


async def export_rows(rows, page_label: str,
                      skip_ids: set[str] | None = None,
                      seen_ids: set[str] | None = None,
                      exported_ids: set[str] | None = None,
                      attachment_ids: set[str] | None = None,
                      use_next: bool = False) -> int:
    """Open each ``(idx, eid, folder)`` in ``rows`` by its list eid, export
    ``.eml``/``.html`` (+attachments) and return the number of emails newly
    completed by this call.

    Each email is opened deterministically from its list eid — the detail view
    does NOT expose the eid (it lives in the cross-origin ``detail-body-iframe``),
    so we trust the row we clicked and only verify that a detail view opened.
    After each export we return to the list so the next eid can be located.
    When ``use_next=True`` (sequential full-page export) we skip the back-to-list
    cycle and use the in-detail "Next email" button between rows, saving ~4 s/email.
    Drafts (``folder == 'DRAFTS'``) are filed under the drafts sub-folders via the
    ``_drafts_dirs`` context manager.

    Robustness over speed: failures are recorded (partials / given-up) and retried
    on a later idempotent re-run — never silently dropped. The open step has a
    small in-run retry because it is the flaky part.

    Returns ``(exported_new, given_up_count)`` so the caller can track both the
    new completions and how many emails couldn't even be opened (for the
    end-of-run completeness report).
    """
    exported = 0
    exported_eids: set[str] = set()
    given_up: set[str] = set()
    skip_ids = skip_ids or set()
    if seen_ids is None:
        seen_ids = set()
    if exported_ids is None:
        exported_ids = set()
    if attachment_ids is None:
        attachment_ids = set()
    global _CONSEC_FAILS, _BOGUS_IDS, _FAIL_COUNTS, _PENDING
    MAX_OPEN_RETRIES = 3
    MAX_CONSECUTIVE_FAILURES = int(os.environ.get("OPEXPORT_MAX_FAILURES", "5"))
    page_int = int(page_label) if str(page_label).isdigit() else None
    on_detail = False
    prog = load_progress()

    for pos, (idx, eid, folder) in enumerate(rows):
        seen_ids.add(eid)
        if (eid in exported_eids or eid in given_up or eid in skip_ids
                or eid in exported_ids or eid in _BOGUS_IDS):
            on_detail = False
            continue
        print(f"  [{page_label}.{pos + 1}] Opening id={eid} (folder={folder})...",
              end=" ", flush=True)

        with _perf_phase("email.total", eid=eid, page=page_label, pos=pos + 1) as pctx:
            if not on_detail:
                with _perf_phase("email.open", eid=eid):
                    opened = None
                    for _ in range(MAX_OPEN_RETRIES):
                        if page_int is not None:
                            await goto_page(page_int)
                        opened = await _open_by_eid(eid)
                        if opened is not None:
                            break
                        if page_int is not None:
                            await goto_page(page_int)
                        else:
                            await _back_to_list()
                        await asyncio.sleep(0.5)
                if opened is None:
                    given_up.add(eid)
                    print("FAILED to open after retries")
                    on_detail = False
                    pctx.ok = False
                    continue
            else:
                if DEBUG:
                    print(" (already on detail via Next) ", end="", flush=True)

            if folder == 'DRAFTS':
                with _drafts_dirs():
                    saved_html, saved_eml, prefix, att_ok, next_btn_idx, diag = \
                        await export_single_email()
            else:
                saved_html, saved_eml, prefix, att_ok, next_btn_idx, diag = \
                    await export_single_email()

            ok = bool(saved_html and saved_eml)
            if ok:
                _CONSEC_FAILS = 0
                newly = eid not in exported_ids
                if newly:
                    exported += 1
                exported_eids.add(eid)
                exported_ids.add(eid)
                if att_ok:
                    attachment_ids.add(eid)
                prog = load_progress()
                prog["exported"] = prog.get("exported", 0) + (1 if newly else 0)
                if str(page_label).isdigit():
                    prog["last_page"] = int(page_label)
                prog["exported_ids"] = sorted(exported_ids)
                prog["seen_ids"] = sorted(seen_ids)
                prog["attachment_ids"] = sorted(attachment_ids)
                prog.setdefault("exported_map", {})[eid] = prefix
                partials = prog.setdefault("partials", [])
                if prefix in partials:
                    partials.remove(prefix)
                # This eid succeeded: clear any prior failure streak and any
                # retry-pending marker so a transient earlier failure isn't held.
                _FAIL_COUNTS.pop(eid, None)
                _BOGUS_IDS.discard(eid)
                _PENDING.discard(eid)
                prog["bogus_ids"] = sorted(_PENDING)
                prog["fail_counts"] = _FAIL_COUNTS
                save_progress(prog)
                print(f"ok {prefix}" + ("" if newly else " (already done)"))
            else:
                prog = load_progress()
                prog["seen_ids"] = sorted(seen_ids)
                partials = prog.setdefault("partials", [])
                if prefix not in partials:
                    partials.append(prefix)
                # Per-eid failure streak: a single flaky email shouldn't abort the
                # whole run, but if it keeps failing we mark it retry-pending (so a
                # later run re-attempts it) AND add it to the within-run guard so the
                # rest of THIS run can proceed without looping on it.
                sr = _FAIL_COUNTS[eid] = _FAIL_COUNTS.get(eid, 0) + 1
                if sr >= BOGUS_THRESHOLD and eid not in _BOGUS_IDS:
                    _BOGUS_IDS.add(eid)
                    _PENDING.add(eid)
                    await _write_diagnostics(eid, diag, sr)
                    print(f"  RETRY-PENDING id={eid} after {sr} failures "
                          f"(reason={diag.get('reason')}); will retry on next run")
                prog["bogus_ids"] = sorted(_PENDING)
                prog["fail_counts"] = _FAIL_COUNTS
                save_progress(prog)
                print(f"PARTIAL/FAIL {prefix} "
                      f"(fail #{_FAIL_COUNTS[eid]}/{BOGUS_THRESHOLD}, "
                      f"reason={diag.get('reason')})")
                _CONSEC_FAILS += 1
                if _CONSEC_FAILS >= MAX_CONSECUTIVE_FAILURES:
                    print(f"  [{page_label}] {_CONSEC_FAILS} consecutive failures, "
                          f"aborting run (set OPEXPORT_MAX_FAILURES to override)")
                    break

            if _SESSION_LOST:
                print(f"  [{page_label}.{pos + 1}] session lost, stopping page early")
                break

            with _perf_phase("email.back", eid=eid):
                if use_next:
                    on_detail = await _navigate_next(pos, len(rows), next_btn_idx)
                    if not on_detail:
                        await _back_to_list()
                else:
                    await _back_to_list()
                    on_detail = False

    prog = load_progress()
    prog["seen_ids"] = sorted(seen_ids)
    prog["attachment_ids"] = sorted(attachment_ids)
    save_progress(prog)
    if given_up:
        print(f"  [{page_label}] WARNING: {len(given_up)} email(s) gave up "
              f"(will retry on next run): {sorted(given_up)[:10]}")
    return exported, len(given_up)


async def process_page(page: int, skip: int = 0, limit: int | None = None,
                      skip_ids: set[str] | None = None,
                      seen_ids: set[str] | None = None,
                      exported_ids: set[str] | None = None,
                      attachment_ids: set[str] | None = None,
                      take: int | None = None) -> int:
    """Export all matching emails on the current page via list-only enumeration.

    ``seen_ids`` / ``exported_ids`` / ``attachment_ids`` accumulate across pages
    (passed in by main) so an interrupted run resumes without redoing work.
    ``skip_ids`` are ids to leave alone this pass. ``take`` (``--select``) slices
    the page chronologically: >0 = newest N, <0 = oldest N (the "last N on the
    page" the user means).
    """
    with _perf_phase("page.collect", page=page):
        if take is not None:
            all_rows = await _collect_all_rows()
            all_rows.sort(key=lambda r: int(r[1]))
            if take >= 0:
                rows = all_rows[-take:]       # newest N
            else:
                rows = all_rows[:abs(take)]   # oldest N
            eid_range = (f"eid {rows[0][1]}..{rows[-1][1]}" if rows
                         else "eid - (none collected)")
            print(f"  [Page {page}] --select: page has {len(all_rows)} ids, "
                  f"taking {len(rows)} (take={take}, {eid_range})")
        else:
            rows = await _collect_all_rows()
            print(f"  [Page {page}] {len(rows)} ids enumerated (list-only)")

    # Apply a resume skip count at the front of the page.
    if skip:
        rows = rows[min(skip, len(rows)):]

    if not rows:
        print(f"  [Page {page}] No emails found on this page")
        return 0
    if limit is not None:
        rows = rows[:limit]
    with _perf_phase("page.total", page=page, count=len(rows)):
        return await export_rows(rows, str(page), skip_ids, seen_ids,
                                 exported_ids, attachment_ids,
                                 use_next=(take is None))


def archive_old_exports():
    """Move existing download files into an archive/ subdirectory so the
    rename logic starts with a clean slate."""
    archive_dir = os.path.join(PROGRESS_DIR, "archive")
    for f in sorted(os.listdir(PROGRESS_DIR)):
        if f.endswith(('.html', '.eml', '.htm')) and f != 'export_progress.json':
            src = os.path.join(PROGRESS_DIR, f)
            os.makedirs(archive_dir, exist_ok=True)
            dst = os.path.join(archive_dir, f)
            base, ext = os.path.splitext(f)
            counter = 1
            while os.path.exists(dst):
                dst = os.path.join(archive_dir, f"{base}_{counter}{ext}")
                counter += 1
            os.rename(src, dst)
    import shutil
    for sub in (EML_DIR, HTML_DIR, ATTACH_DIR,
                DRAFTS_EML_DIR, DRAFTS_HTML_DIR, DRAFTS_ATT_DIR):
        if os.path.isdir(sub):
            shutil.rmtree(sub)
    n = len(os.listdir(archive_dir)) if os.path.isdir(archive_dir) else 0
    if n:
        print(f"  Archived {n} existing file(s) to {archive_dir}/")


def find_disk_partials() -> list[str]:
    """Return name prefixes present in only one of eml/ or html/ (i.e. partials)."""
    def bases(d):
        s = set()
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.lower().endswith(('.eml', '.html', '.htm')):
                    s.add(os.path.splitext(f)[0])
        return s
    return sorted(bases(EML_DIR) ^ bases(HTML_DIR))


def cleanup_strays():
    """Remove top-level .eml/.html/.zip files in PROGRESS_DIR that are byte-identical
    to a file already filed under eml/, html/ or attachments/*/. These are real
    exports an earlier run downloaded but never relocated. Non-duplicate strays are
    moved to unsorted/ for manual review (never silently deleted)."""
    import hashlib
    def hashes_in(d):
        h = set()
        if os.path.isdir(d):
            for f in os.listdir(d):
                fp = os.path.join(d, f)
                if os.path.isfile(fp):
                    try:
                        h.add(hashlib.sha256(open(fp, 'rb').read()).hexdigest())
                    except OSError:
                        pass
        return h
    kept = hashes_in(EML_DIR) | hashes_in(HTML_DIR)
    if os.path.isdir(ATTACH_DIR):
        for sub in os.listdir(ATTACH_DIR):
            kept |= hashes_in(os.path.join(ATTACH_DIR, sub))
    removed = moved = 0
    for f in os.listdir(PROGRESS_DIR):
        fp = os.path.join(PROGRESS_DIR, f)
        if not os.path.isfile(fp):
            continue
        if not f.lower().endswith(('.eml', '.html', '.htm', '.zip')):
            continue
        try:
            h = hashlib.sha256(open(fp, 'rb').read()).hexdigest()
        except OSError:
            continue
        if h in kept:
            try:
                os.remove(fp)
                removed += 1
            except OSError:
                pass
        else:
            ud = os.path.join(PROGRESS_DIR, 'unsorted')
            os.makedirs(ud, exist_ok=True)
            dest = os.path.join(ud, f)
            if not os.path.exists(dest):
                try:
                    os.rename(fp, dest)
                    moved += 1
                except OSError:
                    pass
    if removed:
        print(f"  Cleaned {removed} stray duplicate file(s)")
    if moved:
        print(f"  Moved {moved} unmatched stray file(s) to unsorted/ for review")


async def main():
    global _BOGUS_IDS, _FAIL_COUNTS, _PENDING, SEARCH_TERM, LOCAL_ACCOUNT
    _t0 = time.time()
    # Resolve personal credentials (env -> local saved config -> prompt). Done at
    # runtime, not import, so library imports never trigger a prompt.
    SEARCH_TERM = _resolve_credential(
        "OPEXPORT_SEARCH_TERM", "search_term",
        "Enter the mail.com search term (email/address/domain to find):",
        "them@example.com")
    LOCAL_ACCOUNT = _resolve_credential(
        "OPEXPORT_LOCAL_ACCOUNT", "local_account",
        "Enter the logged-in mail.com address (you):",
        "you@example.com")
    FRESH = '--fresh' in sys.argv
    NEW = '--new' in sys.argv
    LIMIT = None
    for _i, _a in enumerate(sys.argv):
        if _a.startswith('--limit'):
            if '=' in _a:
                LIMIT = int(_a.split('=', 1)[1])
            elif _i + 1 < len(sys.argv):
                LIMIT = int(sys.argv[_i + 1])
            break
    SELECT = None
    for _i, _a in enumerate(sys.argv):
        if _a.startswith('--select'):
            if '=' in _a:
                SELECT = _a.split('=', 1)[1]
            elif _i + 1 < len(sys.argv):
                SELECT = sys.argv[_i + 1]
            break
    KEEPDAEMON = '--keepdaemon' in sys.argv
    FROM = None
    for _i, _a in enumerate(sys.argv):
        if _a.startswith('--from'):
            val = _a.split('=', 1)[1] if '=' in _a else (sys.argv[_i + 1] if _i + 1 < len(sys.argv) else None)
            if val and '.' in str(val):
                pg, idx = val.split('.', 1)
                if pg.isdigit() and idx.isdigit():
                    FROM = (int(pg), int(idx) - 1)  # 1-indexed → 0-indexed skip
            break
    TO = None
    for _i, _a in enumerate(sys.argv):
        if _a.startswith('--to'):
            val = _a.split('=', 1)[1] if '=' in _a else (sys.argv[_i + 1] if _i + 1 < len(sys.argv) else None)
            if val and '.' in str(val):
                pg, idx = val.split('.', 1)
                if pg.isdigit() and idx.isdigit():
                    TO = (int(pg), int(idx))  # 1-indexed (inclusive)
            break
    for _i, _a in enumerate(sys.argv):
        if _a.startswith('--count'):
            if '=' in _a:
                LIMIT = int(_a.split('=', 1)[1])
            elif _i + 1 < len(sys.argv):
                LIMIT = int(sys.argv[_i + 1])
            break
    # Create this run's isolated output folder and record its status. Never
    # deletes prior output (failed runs stay inspectable).
    prepare_run_dir()
    global _perf_log
    _perf_log = PerfLog(os.path.join(PROGRESS_DIR, "perf_log.jsonl"))
    # ── Folder conflict resolution ──────────────────────────────────────
    # If the run folder already has content, the user should decide how to
    # proceed instead of silently resuming (which is easy to miss when you
    # expected a clean run). --fresh and --select skip the prompt (fresh is
    # already explicit opt-in; --select implies a targeted extraction).
    if not FRESH and not SELECT and not NEW and not FROM:
        _non_empty = (
            (os.path.isdir(EML_DIR) and any(f.endswith('.eml') for f in os.listdir(EML_DIR)))
            or (os.path.isdir(HTML_DIR) and any(f.endswith('.html') for f in os.listdir(HTML_DIR)))
            or (os.path.isfile(PROGRESS_FILE) and load_progress().get("exported", 0) > 0)
        )
        if _non_empty:
            print(f"\n  Destination folder '{RUN_NAME}' already has exported emails.")
            print(f"  Options:")
            print(f"    c  Continue (resume) — keep existing progress, pick up where left off")
            print(f"    f  Fresh start       — archive existing files, reset progress")
            print(f"    n  New folder        — exit and re-run with a different OPEXPORT_RUN")
            print(f"    a  Abort             — exit immediately")
            _choice = ""
            while _choice not in ("c", "f", "n", "a"):
                _choice = input("  Choose [c/f/n/a]: ").strip().lower()
            if _choice == "f":
                print("  Fresh start: archiving existing files, starting from scratch")
                archive_old_exports()
                FRESH = True
            elif _choice == "n":
                _new_run = f"{RUN_NAME}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                print(f"  Restarting with OPEXPORT_RUN={_new_run}")
                _env = os.environ.copy()
                _env["OPEXPORT_RUN"] = _new_run
                os.execvpe(sys.executable,
                          [sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
                          _env)
            elif _choice == "a":
                print("  Aborted by user")
                sys.exit(0)
            # 'c': fall through to normal resume
    prog = load_progress()
    os.makedirs(EML_DIR, exist_ok=True)
    os.makedirs(HTML_DIR, exist_ok=True)
    os.makedirs(ATTACH_DIR, exist_ok=True)
    # Drafts are real emails too, but land in their own folder.
    os.makedirs(DRAFTS_EML_DIR, exist_ok=True)
    os.makedirs(DRAFTS_HTML_DIR, exist_ok=True)
    os.makedirs(DRAFTS_ATT_DIR, exist_ok=True)
    # Drop redundant Message-ID duplicate files (misnamed partials from the
    # fragile page-state naming) so a re-run doesn't keep retrying an email we
    # already have intact under a different name.
    n = reconcile_duplicates()
    if n:
        print(f"  Reconciled {n} duplicate file(s)")
        prog = load_progress()
    m = reconcile_names()
    if m:
        print(f"  Re-aligned {m} artifact name(s) to their .eml")
        prog = load_progress()
    start_page = prog.get("last_page", 1)
    already = prog.get("exported", 0)
    search_done = prog.get("search_done", False)
    max_pages = prog.get("max_pages", 0)
    skip_on_page = prog.get("skip_on_page", 0)

    # Load retry-pending state BEFORE the FRESH/NEW/RETRY chain so retry mode can
    # be triggered by pending ids. _BOGUS_IDS is the within-run guard and is always
    # reset empty at the start of a run.
    if not FRESH:
        _PENDING = set(prog.get("bogus_ids", []))
        _FAIL_COUNTS = dict(prog.get("fail_counts", {}))
    else:
        _PENDING = set()
        _FAIL_COUNTS = {}
    _BOGUS_IDS = set()

    # --from / --count / --to override start position and limit.
    if FROM:
        start_page, skip_on_page = FROM
        print(f"  --from {start_page}.{skip_on_page + 1} -> start_page={start_page}, "
              f"skip first {skip_on_page} on page")
    if TO is not None and not FROM:
        print(f"  --to without --from is ignored (no starting position)")
    if TO is not None and FROM:
        start_pg, start_skip = FROM
        end_pg, end_idx = TO
        # Count emails from start_skip+1 on start_page through end_idx on end_page
        if start_pg == end_pg:
            total = end_idx - start_skip  # same page
        else:
            remaining_start = PAGE_SIZE - start_skip
            middle_pages = max(0, end_pg - start_pg - 1) * PAGE_SIZE
            end_count = end_idx  # 1-indexed inclusive
            total = remaining_start + middle_pages + end_count
        LIMIT = total
        print(f"  --to {end_pg}.{end_idx} -> {total} emails total")
    if LIMIT is not None:
        print(f"  --limit (--count) set to {LIMIT}")

    # Stable, permanent per-email identifiers, persisted across runs.
    seen_ids: set[str] = set(prog.get("seen_ids", []))
    exported_ids: set[str] = set(prog.get("exported_ids", []))
    attachment_ids: set[str] = set(prog.get("attachment_ids", []))

    disk_partials = find_disk_partials()
    print(f"Progress: {already} exported, page {start_page}, search_done={search_done}")
    if disk_partials:
        print(f"  NOTE: {len(disk_partials)} email(s) on disk are PARTIAL "
              f"(one of .eml/.html missing).")

    # Resume / recover policy (progress.json is NEVER deleted by default):
    #  - --fresh     : archive existing files and start from scratch (the only
    #    destructive option; explicit opt-in).
    #  - --new       : export ONLY emails never seen before. Scans every page, skips
    #    ids already seen-and-done (seen_ids & exported_ids), but still retries
    #    previously-seen PARTIALS and any brand-new emails.
    #  - --keepdaemon: skip killing stale daemon processes on start (preserves a
    #    logged-in session for faster re-runs during development).
    #  - disk partials present : RECOVER mode -> re-process every page so the
    #    missing format of each partial gets retried (idempotent: completed
    #    emails are simply re-saved/overwritten).
    #  - otherwise : normal resume from the saved last_page.
    if FRESH:
        print("  --fresh: archiving existing files, starting from scratch")
        archive_old_exports()
        start_page = 1
        search_done = False
        max_pages = 0
        seen_ids = set()
        exported_ids = set()
        attachment_ids = set()
        _BOGUS_IDS = set()
        _FAIL_COUNTS = {}
        _PENDING = set()
    elif NEW:
        print("  --new: exporting only emails not seen in a previous run "
              "(re-scanning all pages)")
        start_page = 1
        search_done = False
        max_pages = 0
    elif disk_partials or (exported_ids - attachment_ids) or _PENDING:
        # Incomplete work exists somewhere in the mailbox, possibly on an EARLIER
        # page than last_page. Re-scan EVERY page so nothing is missed; fully-done
        # emails are still skipped via skip_ids, so only the incomplete ones are
        # actually reprocessed.
        reason = "partial formats on disk" if disk_partials else "failed attachment exports"
        print(f"  Retry mode: re-scanning ALL pages to finish incomplete exports "
              f"({reason}; progress.json preserved)")
        start_page = 1
        search_done = False
        max_pages = 0

    # Which ids to leave alone on this pass. An email is only skipped when it is
    # fully done AND its attachments were obtained, so a failed attachment export
    # is retried on a later run (the id stays out of attachment_ids).
    #   --new  -> skip ids already seen AND fully exported AND attachments done
    #             (still redo previously-seen PARTIALS and any brand-new emails)
    #   normal -> skip ids fully exported AND attachments done (resume / recover)
    # Retry-pending ids (_PENDING) are intentionally NOT subtracted here: a normal
    # run re-attempts them (they're "missed"). _BOGUS_IDS is the within-run guard
    # (empty at start); an id only joins it after failing BOGUS_THRESHOLD times in
    # THIS run, so the rest of the run can proceed.
    if NEW:
        skip_ids = (seen_ids & exported_ids & attachment_ids) - _BOGUS_IDS
    else:
        skip_ids = (exported_ids & attachment_ids) - _BOGUS_IDS

    # Disk-aware skip: only skip ids whose BOTH .eml AND .html are actually on
    # disk for their recorded prefix. An id logged as exported but whose file was
    # clobbered/collided in an earlier (broken) run must still be reprocessed.
    # If exported_map lacks an entry for an id, _is_done_on_disk returns False
    # and the id is reprocessed (idempotent: complete emails are simply re-saved).
    exported_map = prog.get("exported_map", {})
    skip_ids = {eid for eid in skip_ids if _is_done_on_disk(eid, exported_map)}

    os.makedirs(EML_DIR, exist_ok=True)
    os.makedirs(HTML_DIR, exist_ok=True)
    os.makedirs(ATTACH_DIR, exist_ok=True)
    os.makedirs(DRAFTS_EML_DIR, exist_ok=True)
    os.makedirs(DRAFTS_HTML_DIR, exist_ok=True)
    os.makedirs(DRAFTS_ATT_DIR, exist_ok=True)

    print(f"  Output (run) folder: {PROGRESS_DIR}")
    # Daemon lifecycle: by default we always kill stale daemon processes on
    # every run so a crashed previous session doesn't leave a half-broken daemon
    # that re-spawns a headless browser or answers pings but fails on real work.
    # Use --keepdaemon to skip the kill (preserves your logged-in session when
    # re-running for testing).
    with _perf_phase("session.setup"):
        if not KEEPDAEMON:
            kill_existing_daemons()
        s, ping_out = await run("print('ping')")
        if 'ping' in ping_out:
            print("  Reusing existing daemon (already running)")
        else:
            await step_start_daemon()
        if await _already_logged_in():
            print("  Already logged in; skipping navigation + login wait")
        else:
            await step_navigate()
            if not await step_wait_login():
                print("  ABORT: could not confirm login; stop here")
                return
        # Speed up every subsequent page load (incl. per-email detail views) by
        # blocking images/CSS/fonts/media at the browser level. One-time, session-wide.
        await _install_resource_blocking()

    # Always check if browser is actually on the search results page.
    # Even with --fresh, the daemon may still be on search results from a prior run.
    on_search_results = False
    s, st = await run(
        "state=await browser.get_state_as_text(); "
        "print('SR', 'Search results' in state)"
    )
    on_search_results = 'SR True' in st

    if not on_search_results:
        with _perf_phase("session.search"):
            ok = await step_do_search()
        if ok:
            prog = load_progress()
            prog["search_done"] = True
            s, out = await run("""
state = await browser.get_state_as_text()
import re
m = re.search(r'min=1 max=(\\d+)', state)
print(f"PAGES={m.group(1) if m else '0'}")
""")
            m = re.search(r'PAGES=(\d+)', out)
            if m:
                prog["max_pages"] = int(m.group(1))
            save_progress(prog)
            max_pages = prog["max_pages"]
            print(f"  Search results: {max_pages} pages")
        else:
            print("  WARNING: search failed, continuing anyway")

    if max_pages == 0:
        max_pages = 259
        print(f"  Using default: {max_pages} pages")

    # --select: restrict to specific pages / first-last-N of each page.
    # Syntax: "PAGE:COUNT,..."  COUNT>0 = first N, COUNT<0 = last N.
    # "last" as a page token means the final page (the oldest email).
    select_map: dict[int, int] = {}
    if SELECT:
        for tok in SELECT.split(','):
            tok = tok.strip()
            if not tok or ':' not in tok:
                continue
            pg, cnt = tok.split(':', 1)
            pg, cnt = pg.strip(), int(cnt.strip())
            key = max_pages if pg == 'last' else int(pg)
            select_map[key] = cnt
        print(f"  --select active: pages={sorted(select_map)} "
              f"(max_pages={max_pages})")

    # Mark downloads already produced in this session as seen, so the per-email
    # tracker only reports files created by the click we just performed.
    await _prime_downloads()

    total_new = 0
    total_given_up = 0
    global _SESSION_LOST

    async def _recover_session(target_page: int) -> bool:
        """Restart daemon, re-login, re-search, and navigate to *target_page*."""
        nonlocal max_pages
        with _perf_phase("session.recovery", target_page=target_page):
            print(f"\n  {'!'*60}")
            print(f"  ! SESSION RECOVERY — target page {target_page}")
            print(f"  {'!'*60}\n")
            kill_existing_daemons()
            ready = await step_start_daemon()
            if not ready:
                print("  [recovery] FAILED: daemon did not start")
                return False
            await step_navigate()
            if not await step_wait_login():
                print("  [recovery] FAILED: login not confirmed")
                return False
            ok = await step_do_search()
            if not ok:
                print("  [recovery] WARNING: search not confirmed, continuing")
            _p = load_progress()
            if _p.get("max_pages", 0):
                max_pages = _p["max_pages"]
            await goto_page(target_page)
            print(f"  [recovery] session recovered, on page {target_page}")
            return True

    for page in range(start_page, max_pages + 1):
        if SELECT is not None and page not in select_map:
            continue

        for _attempt in range(2):
            _SESSION_LOST = False
            _CONSEC_FAILS = 0

            print(f"\n{'='*50}")
            print(f"PAGE {page}")
            print(f"{'='*50}")
            if page > 1:
                await goto_page(page)
            if TOTAL > 0 and SELECT is None:
                if page < max_pages:
                    _exp = PAGE_SIZE
                else:
                    _exp = TOTAL - PAGE_SIZE * (max_pages - 1)
                    _exp = max(_exp, 1)
                await _ensure_page_row_count(_exp, str(page))
            skip = skip_on_page if page == start_page else 0
            take = select_map.get(page) if SELECT is not None else None
            row_limit = max(0, LIMIT - total_new) if LIMIT is not None else None
            n, gu = await process_page(page, skip=skip, limit=row_limit,
                                        skip_ids=skip_ids,
                                        seen_ids=seen_ids, exported_ids=exported_ids,
                                        attachment_ids=attachment_ids, take=take)
            print(f"  \u2192 Page {page}: exported {n}")
            total_new += n
            total_given_up += gu

            if _SESSION_LOST:
                if _attempt == 0:
                    print(f"  PAGE {page}: session lost, recovering...")
                    ok = await _recover_session(page)
                    if not ok:
                        print(f"  PAGE {page}: recovery failed, skipping")
                        break
                else:
                    _p = load_progress()
                    save_progress(_p)
                    print(f"  PAGE {page}: unrecoverable after recovery, "
                          f"saving progress and skipping")
                    break
            else:
                break  # page completed successfully

        if _CONSEC_FAILS >= int(os.environ.get("OPEXPORT_MAX_FAILURES", "5")):
            print(f"  {_CONSEC_FAILS} consecutive failures across pages, aborting run")
            break

        if LIMIT is not None and SELECT is None and total_new >= LIMIT:
            print(f"  --limit {LIMIT} reached ({total_new} exported); stopping early")
            break

    cleanup_strays()
    prog = load_progress()
    prog["seen_ids"] = sorted(seen_ids)
    prog["exported_ids"] = sorted(exported_ids)
    prog["attachment_ids"] = sorted(attachment_ids)
    prog["last_page"] = max_pages
    save_progress(prog)
    n = build_email_index()
    print(f"  Email index written: {n} email(s) -> "
          f"{os.path.join(PROGRESS_DIR, 'email_index.txt')}")
    # Cross-check the AUTHORITATIVE disk state, not just progress.json.partials
    # (migration intentionally resets partials to [], and a manual save or a
    # clobbered file can leave a disk-level partial that progress.json never
    # recorded). Report the union so the final message always tells the truth.
    disk_partials_end = find_disk_partials()
    prog = load_progress()
    recorded_partials = prog.get("partials", [])
    open_partials = sorted(set(recorded_partials) | set(disk_partials_end))
    files = count_downloaded_files()
    with _perf_phase("session.summary", total_new=total_new, files=files):
        incomplete = bool(open_partials) or bool(_PENDING) or (total_given_up > 0) \
            or (TOTAL and len(seen_ids) < TOTAL)
        if incomplete:
            print(f"\n{'!'*50}")
            print("⚠ BACKUP INCOMPLETE — not all emails were exported.")
            print(f"{'!'*50}")
            print(f"  Partial/missing files : {len(open_partials)}")
            print(f"  Retry-pending (failed) : {len(_PENDING)}")
            print(f"  Could not be opened   : {total_given_up}")
            if TOTAL and len(seen_ids) < TOTAL:
                print(f"  Emails never seen     : {TOTAL - len(seen_ids)} "
                      f"(likely hidden by viewport/zoom)")
        print(f"\n{'='*50}")
        print(f"DONE! This run: +{total_new} | Total files: {files}")
        if open_partials:
            print(f"  {len(open_partials)} email(s) still PARTIAL (one of .eml/.html "
                  f"missing on disk, or gave up):")
            for p in open_partials[:20]:
                print(f"    - {p}")
            if len(open_partials) > 20:
                print(f"    ... and {len(open_partials) - 20} more")
        else:
            print(f"  All processed emails complete (both .eml and .html present).")
        print(f"  Seen ids: {len(seen_ids)} | Fully exported ids: {len(exported_ids)} "
              f"| With attachments: {len(attachment_ids)}")
        if _PENDING:
            print(f"  Retry-pending (failed {BOGUS_THRESHOLD}x, retried on next run): "
                  f"{len(_PENDING)} id(s)")
            for b in sorted(_PENDING)[:20]:
                print(f"    - {b}")
            if len(_PENDING) > 20:
                print(f"    ... and {len(_PENDING) - 20} more")
        # ── Advice when the backup is incomplete ──
        if incomplete:
            print(f"\n  To finish a complete backup:")
            print(f"    • Re-run to retry the failed emails (auto-retries them):")
            print(f"        {sys.executable} {os.path.basename(sys.argv[0])}")
            print(f"    • Connection health: {_RTT_FAILS} daemon round-trip(s) failed"
                  f" this run. Ensure the openbrowser-ai daemon is alive and the"
                  f" network is stable (see perf_log.jsonl).")
            try:
                _tp = time.monotonic()
                await run("1+1")
                print(f"      Live daemon ping: OK ({time.monotonic() - _tp:.1f}s)")
            except Exception as _e:
                print(f"      Live daemon ping: FAILED ({_e})")
            print(f"    • Zoom out: reduce browser zoom / enlarge the viewport so all"
                  f" {PAGE_SIZE} emails per page are visible (fewer on the last page)."
                  f" Rows virtualized away are missed — if emails were never seen,"
                  f" zoom out and re-run.")
        print(f"Progress saved to: {PROGRESS_FILE}")
        print(f"  Script wall-clock: {time.time() - _t0:.1f}s")
        # Run finished without crashing: mark it completed so it is NOT treated as a
        # failed run by the maintenance/cleanup tool. Failed runs keep status
        # 'running' (or 'failed') and stay on disk for inspection.
        _mark_run_status("completed")
    _perf_log.summary(total_pages=max_pages, total_exported=len(exported_ids),
                       total_new=total_new, seen=len(seen_ids),
                       partials=open_partials)
    _perf_log.close()

    # Offer to re-run when the backup is incomplete. Direct TTY only; suppressed
    # when spawned by the interactive TUI (OPEXPORT_SUBPROCESS=1) so the prompt
    # doesn't appear after the TUI has returned to its menu.
    _incomplete = bool(open_partials) or bool(_PENDING) or (total_given_up > 0) \
        or (TOTAL and len(seen_ids) < TOTAL)
    if _incomplete and sys.stdin.isatty() and not os.environ.get("OPEXPORT_SUBPROCESS"):
        try:
            ans = input("\nRe-run now to retry failed emails? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans in ("y", "yes"):
            _argv = [a for a in sys.argv if a not in ("--fresh", "--new")]
            os.execv(sys.executable, [sys.executable, *_argv])

if __name__ == "__main__":
    asyncio.run(main())
