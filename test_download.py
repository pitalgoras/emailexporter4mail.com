#!/usr/bin/env python3
"""Test: on the currently open (attachment-bearing) email, find and click the
'Download (to hard disc)' buttons and report what files land on disk.

Reuses the already-running, logged-in daemon. Does NOT navigate or log in.
"""
import sys, os, time, asyncio

sys.path.insert(0, os.path.expanduser(
    "~/.local/share/uv/tools/openbrowser-ai/lib/python3.14/site-packages"
))
import warnings
warnings.filterwarnings("ignore", message="Failed to set up openbrowser logging")
from openbrowser.daemon.client import execute_code_via_daemon
import export_emails as E

async def run(code):
    resp = await execute_code_via_daemon(code)
    out = (resp.output or "") + ("\n" + resp.error if resp.error else "")
    return resp.success, out

async def main():
    # Prime: mark any pre-existing downloads as seen.
    await E._collect_downloads()
    indiv, zipbtn = await E._find_download_targets()
    print("DOWNLOAD TARGETS  indiv=", indiv, " zip=", zipbtn)
    if not indiv and zipbtn is None:
        print("NO BUTTONS - nothing to do")
        return
    prefix = "TESTATT_" + time.strftime("%H%M%S")
    n = await E._download_ui_attachments(prefix)
    print("DOWNLOADED:", n)
    dest = os.path.join(E.ATTACH_DIR, prefix)
    if os.path.isdir(dest):
        print("FILES:", sorted(os.listdir(dest)))
    else:
        print("NO DEST DIR")
    print("TEST DONE")

if __name__ == "__main__":
    asyncio.run(main())
