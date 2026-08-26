#!/usr/bin/env python3
"""Test multi-attachment ZIP download. Reuses the live daemon: navigates to
mail.com, waits for login, opens the first inbox email (newest), and runs the
attachment downloader. Prints the subject + download targets so we can confirm
which email this is.
"""
import sys, os, time, asyncio

sys.path.insert(0, os.path.expanduser(
    "~/.local/share/uv/tools/openbrowser-ai/lib/python3.14/site-packages"
))
import warnings
warnings.filterwarnings("ignore", message="Failed to set up openbrowser logging")
from openbrowser.daemon.client import execute_code_via_daemon
from export_emails import (
    run, step_navigate, step_wait_login, _collect_downloads,
    _find_download_targets, _download_ui_attachments, ATTACH_DIR,
)

async def main():
    await step_navigate()
    await step_wait_login()

    code = """
import re
st = await browser.get_state_as_text()
lines = st.split('\\n')
pairs = []
for i, l in enumerate(lines):
    if 'list-mail-item' in l and 'id=id' in l:
        m = re.search(r'\\[(\\d+)\\]', l)
        e = re.search(r'id=id(\\d+)', l)
        if m and e:
            pairs.append((int(m.group(1)), e.group(1)))
print('PAIRS ' + ','.join(f"{a}:{b}" for a, b in pairs[:6]))
"""
    s, out = await run(code)
    pairs = []
    for ln in out.split('\n'):
        if ln.strip().startswith('PAIRS '):
            for tok in ln.strip()[6:].split(','):
                if ':' in tok:
                    a, b = tok.split(':', 1)
                    if a.isdigit():
                        pairs.append((int(a), b))
            break
    if not pairs:
        print("NO INBOX ITEMS")
        return

    await run(f"print(await click({pairs[0][0]}))")
    await asyncio.sleep(2.5)

    s, st = await run("print(await browser.get_state_as_text())")
    # Heuristic subject: a list-mail-item title line near the top of the detail.
    print("STATE HEAD (first 50 lines):")
    for ln in st.splitlines()[:50]:
        print("  ", ln[:130])

    await _collect_downloads()
    indiv, zipbtn = await _find_download_targets()
    print("TARGETS  indiv=", indiv, " zip=", zipbtn)
    if indiv or zipbtn is not None:
        prefix = "TESTMULTI_" + time.strftime("%H%M%S")
        n = await _download_ui_attachments(prefix)
        print("DOWNLOADED", n)
        dest = os.path.join(ATTACH_DIR, prefix)
        if os.path.isdir(dest):
            print("FILES", sorted(os.listdir(dest)))
    else:
        print("NO ATTACHMENTS in this email")
    print("DONE")

if __name__ == "__main__":
    asyncio.run(main())
