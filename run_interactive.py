#!/usr/bin/env python3
"""Interactive TUI launcher for export_emails.py.

Provides a guided terminal UI for all OPEXPORT_* env vars and CLI flags,
with preset save/load support. Run:

    uv run run_interactive.py
    uv run run_interactive.py --dry-run   # preview command, don't execute

Requires: `uv` (manages the environment; `uv run` installs questionary)
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import questionary
    from questionary import Choice
except ImportError:
    print("questionary not installed. Run: pip install questionary")
    sys.exit(1)

SRC_DIR = Path(__file__).resolve().parent
EXPORT_SCRIPT = SRC_DIR / "export_emails.py"
PRESET_DIR = Path.home() / ".config" / "opexport" / "presets"

# ── Defaults ──

DEFAULT_ENV: dict[str, str] = {
    "OPEXPORT_ROOT": str(Path.home() / "Downloads" / "openbrowser-daemon"),
    "OPEXPORT_RUN": "",
    "OPEXPORT_SEARCH_TERM": "them@example.com",
    "OPEXPORT_SEARCH_FIELD": "All headers",
    "OPEXPORT_SEARCH_FOLDER": "All folders",
    "OPEXPORT_LOCAL_ACCOUNT": "you@example.com",
    "OPEXPORT_HEADLESS": "false",
    "OPEXPORT_DEBUG": "true",
    "OPEXPORT_WAIT": "10",
    "OPEXPORT_SLEEP": "0.3",
    "OPEXPORT_OPEN_POLL": "0.3",
    "OPEXPORT_BLOCK_RESOURCES": "image,media,font",
    "OPEXPORT_PAGE_SIZE": "50",
    "OPEXPORT_TOTAL": "0",
    "OPEXPORT_LOGIN_TIMEOUT": "0",
    "OPEXPORT_MAX_FAILURES": "5",
}

DEFAULT_FLAGS: dict[str, str | bool] = {
    "--fresh": False,
    "--new": False,
    "--keepdaemon": False,
    "--limit": "",
    "--select": "",
    "--from": "",
    "--count": "",
}

SEARCH_FIELD_CHOICES = ["All headers", "Sender", "Recipient", "Subject"]


# ── Preset helpers ──

def _ensure_preset_dir():
    PRESET_DIR.mkdir(parents=True, exist_ok=True)


def list_presets() -> list[dict]:
    _ensure_preset_dir()
    presets = []
    for f in sorted(PRESET_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            data.setdefault("name", f.stem)
            presets.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return presets


def save_preset(name: str, env: dict, flags: dict) -> Path:
    _ensure_preset_dir()
    path = PRESET_DIR / f"{name}.json"
    data = {
        "name": name,
        "env": env,
        "flags": {k: v for k, v in flags.items() if v},
        "created": datetime.now().isoformat(timespec="seconds"),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(data, indent=2))
    return path


# ── Prompt sections ──

def section_destination(current: dict) -> dict:
    env = current.get("env", {})
    root = questionary.text(
        "Output root directory",
        default=env.get("OPEXPORT_ROOT", DEFAULT_ENV["OPEXPORT_ROOT"]),
    ).ask()
    if root is None:
        return current
    env["OPEXPORT_ROOT"] = root

    run = questionary.text(
        "Run name (leave blank for auto-timestamped)",
        default=env.get("OPEXPORT_RUN", ""),
    ).ask()
    if run is None:
        return current
    env["OPEXPORT_RUN"] = run

    return {"env": env, "flags": current.get("flags", dict(DEFAULT_FLAGS))}


def section_search(current: dict) -> dict:
    env = current.get("env", {})
    from export_emails import _load_credential, _save_credential
    saved_term = _load_credential("search_term")

    term = questionary.text(
        "Search term (email / address / domain)",
        default=saved_term or env.get("OPEXPORT_SEARCH_TERM", DEFAULT_ENV["OPEXPORT_SEARCH_TERM"]),
    ).ask()
    if term is None:
        return current
    env["OPEXPORT_SEARCH_TERM"] = term
    if term:
        _save_credential("search_term", term)

    field = questionary.select(
        "Search field",
        choices=SEARCH_FIELD_CHOICES,
        default=env.get("OPEXPORT_SEARCH_FIELD", DEFAULT_ENV["OPEXPORT_SEARCH_FIELD"]),
    ).ask()
    if field is None:
        return current
    env["OPEXPORT_SEARCH_FIELD"] = field

    folder = questionary.text(
        "Search folder (e.g. 'All folders', 'Inbox', 'Sent', type custom name)",
        default=env.get("OPEXPORT_SEARCH_FOLDER", DEFAULT_ENV["OPEXPORT_SEARCH_FOLDER"]),
    ).ask()
    if folder is None:
        return current
    env["OPEXPORT_SEARCH_FOLDER"] = folder

    return {"env": env, "flags": current.get("flags", dict(DEFAULT_FLAGS))}


def section_mode(current: dict) -> dict:
    flags = current.get("flags", dict(DEFAULT_FLAGS))

    checked = questionary.checkbox(
        "Mode flags (space to toggle, enter to confirm)",
        choices=[
            Choice("--fresh    Archive existing files, start from scratch", value="fresh", checked=bool(flags.get("--fresh"))),
            Choice("--new      Export only emails never seen before (re-scans all pages)", value="new", checked=bool(flags.get("--new"))),
            Choice("--keepdaemon  Preserve logged-in daemon session (skip kill on start)", value="keepdaemon", checked=bool(flags.get("--keepdaemon"))),
        ],
    ).ask()
    if checked is None:
        return current
    flags["--fresh"] = "fresh" in checked
    flags["--new"] = "new" in checked
    flags["--keepdaemon"] = "keepdaemon" in checked

    limit_val = questionary.text(
        "--limit N  Stop after N newly exported emails (blank = no limit)",
        default=str(flags.get("--limit") or ""),
    ).ask()
    if limit_val is None:
        return current
    flags["--limit"] = limit_val.strip()

    select_val = questionary.text(
        '--select PAGE:COUNT,...  Targeted extraction (blank = all pages)\n'
        '  e.g. 1:-3,2:5,last:-1  (COUNT>0 = newest N, <0 = oldest N)',
        default=str(flags.get("--select") or ""),
    ).ask()
    if select_val is None:
        return current
    flags["--select"] = select_val.strip()

    from_val = questionary.text(
        '--from PAGE.INDEX  Start at a specific email (blank = default)\n'
        '  e.g. 1.25 = page 1, email #25',
        default=str(flags.get("--from") or ""),
    ).ask()
    if from_val is None:
        return current
    flags["--from"] = from_val.strip()

    count_val = questionary.text(
        '--count N  Export N emails from start (blank = no limit)\n'
        '  alias for --limit; use with --from for a range',
        default=str(flags.get("--count") or ""),
    ).ask()
    if count_val is None:
        return current
    flags["--count"] = count_val.strip()

    return {"env": current.get("env", dict(DEFAULT_ENV)), "flags": flags}


def section_account(current: dict) -> dict:
    env = current.get("env", {})
    from export_emails import _load_credential, _save_credential
    saved_acct = _load_credential("local_account")

    acct = questionary.text(
        "Your mail.com address (controls filename party / sent-vs-received)",
        default=saved_acct or env.get("OPEXPORT_LOCAL_ACCOUNT", DEFAULT_ENV["OPEXPORT_LOCAL_ACCOUNT"]),
    ).ask()
    if acct is None:
        return current
    env["OPEXPORT_LOCAL_ACCOUNT"] = acct
    if acct:
        _save_credential("local_account", acct)

    return {"env": env, "flags": current.get("flags", dict(DEFAULT_FLAGS))}


def section_tuning(current: dict) -> dict:
    env = current.get("env", {})

    headless = questionary.confirm(
        "Headless mode? (No = visible browser for interactive login)",
        default=env.get("OPEXPORT_HEADLESS", "false").lower() == "true",
    ).ask()
    if headless is None:
        return current
    env["OPEXPORT_HEADLESS"] = "true" if headless else "false"

    debug = questionary.confirm(
        "OPEXPORT_DEBUG (forces visible browser, overrides headless)?",
        default=env.get("OPEXPORT_DEBUG", "true").lower() == "true",
    ).ask()
    if debug is None:
        return current
    env["OPEXPORT_DEBUG"] = "true" if debug else "false"

    wait = questionary.text(
        "OPEXPORT_WAIT  Max seconds to wait for a Save download (default 10)",
        default=env.get("OPEXPORT_WAIT", DEFAULT_ENV["OPEXPORT_WAIT"]),
    ).ask()
    if wait is None:
        return current
    env["OPEXPORT_WAIT"] = wait

    sleep = questionary.text(
        "OPEXPORT_SLEEP  Fixed settle seconds between actions (default 0.3)",
        default=env.get("OPEXPORT_SLEEP", DEFAULT_ENV["OPEXPORT_SLEEP"]),
    ).ask()
    if sleep is None:
        return current
    env["OPEXPORT_SLEEP"] = sleep

    open_poll = questionary.text(
        "OPEXPORT_OPEN_POLL  Poll interval while waiting for detail view (default 0.3)",
        default=env.get("OPEXPORT_OPEN_POLL", DEFAULT_ENV["OPEXPORT_OPEN_POLL"]),
    ).ask()
    if open_poll is None:
        return current
    env["OPEXPORT_OPEN_POLL"] = open_poll

    block = questionary.text(
        "OPEXPORT_BLOCK_RESOURCES  Blocked resource types (default image,media,font; 'none' to disable)",
        default=env.get("OPEXPORT_BLOCK_RESOURCES", DEFAULT_ENV["OPEXPORT_BLOCK_RESOURCES"]),
    ).ask()
    if block is None:
        return current
    env["OPEXPORT_BLOCK_RESOURCES"] = block

    page_size = questionary.text(
        "OPEXPORT_PAGE_SIZE  Expected rows per non-last page (default 50)",
        default=env.get("OPEXPORT_PAGE_SIZE", DEFAULT_ENV["OPEXPORT_PAGE_SIZE"]),
    ).ask()
    if page_size is None:
        return current
    env["OPEXPORT_PAGE_SIZE"] = page_size

    total = questionary.text(
        "OPEXPORT_TOTAL  Total search result count (0 = skip enforcement)",
        default=env.get("OPEXPORT_TOTAL", DEFAULT_ENV["OPEXPORT_TOTAL"]),
    ).ask()
    if total is None:
        return current
    env["OPEXPORT_TOTAL"] = total

    login_to = questionary.text(
        "OPEXPORT_LOGIN_TIMEOUT  Max seconds to wait for login (0 = wait forever)",
        default=env.get("OPEXPORT_LOGIN_TIMEOUT", DEFAULT_ENV["OPEXPORT_LOGIN_TIMEOUT"]),
    ).ask()
    if login_to is None:
        return current
    env["OPEXPORT_LOGIN_TIMEOUT"] = login_to

    max_fail = questionary.text(
        "OPEXPORT_MAX_FAILURES  Consecutive failures before aborting (default 5)",
        default=env.get("OPEXPORT_MAX_FAILURES", DEFAULT_ENV["OPEXPORT_MAX_FAILURES"]),
    ).ask()
    if max_fail is None:
        return current
    env["OPEXPORT_MAX_FAILURES"] = max_fail

    return {"env": env, "flags": current.get("flags", dict(DEFAULT_FLAGS))}


SECTIONS = [
    ("1. Destination", section_destination),
    ("2. Search Criteria", section_search),
    ("3. Run Mode", section_mode),
    ("4. Account", section_account),
    ("5. Tuning", section_tuning),
]


# ── Build & run ──

def build_env_and_args(config: dict) -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    for k, v in config.get("env", {}).items():
        if v:
            env[k] = v
    # Rely on uv for the child too: `uv run` always uses the project's managed
    # environment (the interpreter that actually has the `openbrowser` package),
    # regardless of how THIS launcher was started. Fall back to sys.executable only
    # if uv is somehow unavailable.
    uv_bin = shutil.which("uv")
    if uv_bin:
        args = [uv_bin, "run", str(EXPORT_SCRIPT)]
    else:
        args = [sys.executable, str(EXPORT_SCRIPT)]
    flags: dict = config.get("flags", {})
    if flags.get("--fresh"):
        args.append("--fresh")
    if flags.get("--new"):
        args.append("--new")
    if flags.get("--keepdaemon"):
        args.append("--keepdaemon")
    limit = flags.get("--limit", "")
    if limit:
        args.extend(["--limit", str(limit)])
    select = flags.get("--select", "")
    if select:
        args.extend(["--select", str(select)])
    frm = flags.get("--from", "")
    if frm:
        args.extend(["--from", str(frm)])
    count = flags.get("--count", "")
    if count:
        args.extend(["--count", str(count)])
    return env, args


def print_summary(config: dict):
    env = config.get("env", {})
    flags: dict = config.get("flags", {})
    run_name = env.get("OPEXPORT_RUN") or "(auto-timestamped)"
    root = env.get("OPEXPORT_ROOT", DEFAULT_ENV["OPEXPORT_ROOT"])

    print()
    print("╔══════════════════════════════════════════╗")
    print("║        Configuration Summary             ║")
    print("╠══════════════════════════════════════════╣")
    print(f"║  Destination                              ║")
    print(f"║    Root : {root}")
    print(f"║    Run  : {run_name}")
    print(f"║                                          ║")
    print(f"║  Search                                   ║")
    print(f"║    Term : {env.get('OPEXPORT_SEARCH_TERM', '—')}")
    print(f"║    Field: {env.get('OPEXPORT_SEARCH_FIELD', '—')}")
    print(f"║    Folder: {env.get('OPEXPORT_SEARCH_FOLDER', '—')}")
    print(f"║                                          ║")
    active_flags = [k for k, v in flags.items() if v and k.startswith("--") and isinstance(v, bool)]
    extra = []
    if flags.get("--limit"):
        extra.append(f"--limit {flags['--limit']}")
    if flags.get("--select"):
        extra.append(f"--select {flags['--select']}")
    if flags.get("--from"):
        extra.append(f"--from {flags['--from']}")
    if flags.get("--count"):
        extra.append(f"--count {flags['--count']}")
    print(f"║  Mode flags: {' '.join(active_flags + extra) or '(none)'}")
    print(f"║                                          ║")
    print(f"║  Account: {env.get('OPEXPORT_LOCAL_ACCOUNT', '—')}")
    print(f"║                                          ║")
    print(f"║  Tuning                                   ║")
    print(f"║    Headless: {env.get('OPEXPORT_HEADLESS', '—')}")
    print(f"║    Debug:    {env.get('OPEXPORT_DEBUG', '—')}")
    print(f"║    Wait:     {env.get('OPEXPORT_WAIT', '—')}")
    print(f"║    Sleep:    {env.get('OPEXPORT_SLEEP', '—')}")
    print(f"║    Block:    {env.get('OPEXPORT_BLOCK_RESOURCES', '—')}")
    print(f"║    Max fails:{env.get('OPEXPORT_MAX_FAILURES', '—')}")
    print("╚══════════════════════════════════════════╝")
    print()


def run_export(env: dict, args: list[str]):
    dry_run = "--dry-run" in sys.argv
    cmd_str = " ".join(args)
    print()
    print(f"  Command: {cmd_str}")
    env_overrides = {k: v for k, v in env.items() if k.startswith("OPEXPORT_") and v != os.environ.get(k)}
    if env_overrides:
        print(f"  Env overrides: {json.dumps(env_overrides, indent=2)}")
    print()
    if dry_run:
        print("  --dry-run: command printed above, exiting.")
        return

    print("  Starting export...")
    sys.stdout.flush()
    # Mark this as a subprocess spawn so export_emails.py suppresses its own
    # interactive re-run prompt (the TUI stays in control of the terminal).
    sub_env = dict(env)
    sub_env["OPEXPORT_SUBPROCESS"] = "1"
    result = subprocess.run(args, env=sub_env, cwd=SRC_DIR)
    if result.returncode != 0:
        print(f"\n  Export exited with code {result.returncode}")
    else:
        print(f"\n  Export completed successfully.")


# ── main ──

def main():
    if not sys.stdin.isatty():
        print("run_interactive.py requires an interactive terminal (TTY).")
        print("Usage: uv run run_interactive.py  (opens a guided TUI)")
        sys.exit(1)
    dry_run = "--dry-run" in sys.argv
    presets = list_presets()

    # ── Preset picker ──
    use_preset = None
    if presets:
        choices = [Choice("  New configuration  (start from defaults)", value=None)]
        choices += [Choice(f"  {p['name']}", value=p) for p in presets]
        selected = questionary.select(
            "Load a saved preset or create a new configuration?",
            choices=choices,
        ).ask()
        if selected is None:
            return
        use_preset = selected

    # ── Configure ──
    if use_preset:
        config = {
            "env": dict(use_preset.get("env", {})),
            "flags": dict(use_preset.get("flags", {})),
        }
    else:
        config = {"env": dict(DEFAULT_ENV), "flags": dict(DEFAULT_FLAGS)}
        for title, section_fn in SECTIONS:
            print(f"\n─── {title} ───")
            result = section_fn(config)
            if result is None:
                return
            config = result

    # ── Review loop ──
    while True:
        print_summary(config)
        actions = ["Confirm & Run", "Save as preset"]
        for idx, (title, _) in enumerate(SECTIONS, 1):
            actions.append(f"Edit section {idx} ({title.split('. ', 1)[1]})")
        actions.append("Cancel")

        choice = questionary.select(
            "What now?",
            choices=actions,
        ).ask()
        if choice is None or choice == "Cancel":
            print("Cancelled.")
            return

        if choice == "Confirm & Run":
            env, args = build_env_and_args(config)
            run_export(env, args)
            return

        if choice == "Save as preset":
            name = questionary.text("Preset name (no spaces)").ask()
            if name and name.strip():
                save_preset(name.strip(), config.get("env", {}), config.get("flags", {}))
                print(f"  Saved preset '{name.strip()}' to {PRESET_DIR / (name.strip() + '.json')}")
            else:
                print("  Save cancelled.")
            continue

        # Edit a section
        for idx, (title, section_fn) in enumerate(SECTIONS, 1):
            if choice == f"Edit section {idx} ({title.split('. ', 1)[1]})":
                print(f"\n─── {title} ───")
                result = section_fn(config)
                if result is None:
                    return
                config = result
                break


if __name__ == "__main__":
    main()
