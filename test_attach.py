#!/usr/bin/env python3
"""Focused test: export the LAST 3 emails on page 1 (the ones with attachments).

Verifies the UI attachment-chip download path end to end. Reuses an existing
daemon or starts a fresh one and waits for login.
"""
import asyncio, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_emails as E


async def page1_sender_count() -> int:
    code = r"""
import re
state = await browser.get_state_as_text()
lines = state.split('\n')
seen = set()
pairs = []
for i, l in enumerate(lines):
    if 'list-mail-item' in l and 'id=id' in l:
        near = lines[max(0,i-2):min(len(lines),i+12)]
        if any('__TERM__' in nl for nl in near):
            m = re.search(r'\[(\d+)\]', l)
            e = re.search(r'id=id(\d+)', l)
            if m and e and e.group(1) not in seen:
                seen.add(e.group(1))
                pairs.append(1)
print('N', len(pairs))
""".replace('__TERM__', E.SEARCH_TERM)
    s, out = await E.run(code)
    m = re.search(r'N (\d+)', out)
    return int(m.group(1)) if m else 35


async def main():
    s, ping = await E.run("print('ping')")
    if 'ping' not in ping:
        await E.step_start_daemon()
    await E.step_navigate()
    await E.step_wait_login()
    ok = await E.step_do_search()
    print("search_ok:", ok)
    await E._prime_downloads()
    await E.goto_page(1)
    await asyncio.sleep(2)
    n = await page1_sender_count()
    print("page1 senders:", n)
    skip = max(0, n - 3)
    print(f"Exporting last 3 (skip={skip}) ...")
    exported, _ = await E.process_page(1, skip=skip, limit=3)
    print(f"DONE: exported {exported} attachment-bearing email(s)")
    print(f"  eml/:  {E.EML_DIR}")
    print(f"  html/: {E.HTML_DIR}")
    print(f"  attachments/: {E.ATTACH_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
