#!/usr/bin/env python3
"""
Inspect and clear retry-pending ("blacklisted") emails recorded in the current
run's export_progress.json, and re-run the export to retry them.

A normal run already re-attempts retry-pending emails automatically, so this
tool is mainly for inspecting *why* an email was deferred, or for clearing a
stuck/stale entry that keeps failing (e.g. after you fixed the underlying
cause — wrong zoom, dead daemon, etc.).

Usage:
    python3 unblacklist.py                 # --list (default)
    python3 unblacklist.py --list
    python3 unblacklist.py --remove eidA,eidB
    python3 unblacklist.py --remove-all
    python3 unblacklist.py --rerun         # spawn export_emails.py to retry them
    OPEXPORT_RUN=run1 python3 unblacklist.py --list
"""
import os
import sys
import json
import subprocess

import export_emails as E

PROGRESS_FILE = E.PROGRESS_FILE
PROGRESS_DIR = E.PROGRESS_DIR
DIAG_DIR = os.path.join(PROGRESS_DIR, "diagnostics")
SRC_DIR = os.path.dirname(os.path.abspath(__file__))


def load_prog():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_prog(prog):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(prog, f, indent=2)


def diag_reason(eid):
    p = os.path.join(DIAG_DIR, f"{eid}.json")
    if not os.path.exists(p):
        return "(no diagnostic captured)"
    try:
        with open(p) as f:
            d = json.load(f)
        return d.get("diag", {}).get("reason", "(unknown)")
    except Exception:
        return "(unreadable diagnostic)"


def do_list():
    prog = load_prog()
    pending = prog.get("bogus_ids", []) or []
    fail = prog.get("fail_counts", {}) or {}
    if not pending:
        print("No retry-pending (blacklisted) emails. Backup is clean for retry.")
        return
    print(f"Retry-pending emails ({len(pending)}) in:\n  {PROGRESS_DIR}\n")
    for eid in pending:
        print(f"  - {eid}")
        print(f"      fail_count={fail.get(eid, '?')}  reason={diag_reason(eid)}")


def do_remove(ids):
    prog = load_prog()
    pending = set(prog.get("bogus_ids", []) or [])
    fail = prog.get("fail_counts", {}) or {}
    removed = []
    for eid in ids:
        if eid in pending:
            pending.discard(eid)
            fail.pop(eid, None)
            removed.append(eid)
            for ext in (".json", ".ax.txt"):
                fp = os.path.join(DIAG_DIR, f"{eid}{ext}")
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
    prog["bogus_ids"] = sorted(pending)
    prog["fail_counts"] = fail
    save_prog(prog)
    if removed:
        print(f"Cleared {len(removed)} retry-pending id(s): {', '.join(removed)}")
    else:
        print("None of the given ids were retry-pending; nothing changed.")


def do_rerun():
    prog = load_prog()
    pending = prog.get("bogus_ids", []) or []
    if not pending:
        print("Nothing retry-pending. Running a normal resume anyway.")
    args = [sys.executable, os.path.join(SRC_DIR, "export_emails.py")]
    print(f"Re-running export to retry {len(pending)} pending email(s)...")
    subprocess.run(args, cwd=SRC_DIR)


def main():
    argv = sys.argv[1:]
    if "--list" in argv or not any(
        a in argv for a in ("--remove", "--remove-all", "--rerun")
    ):
        do_list()
        return
    if "--remove-all" in argv:
        do_remove(list(load_prog().get("bogus_ids", []) or []))
        return
    for i, a in enumerate(argv):
        if a == "--remove":
            ids = []
            if i + 1 < len(argv):
                ids = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
            do_remove(ids)
            return
    if "--rerun" in argv:
        do_rerun()
        return


if __name__ == "__main__":
    main()
