#!/usr/bin/env python3
import sys, os, re, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_emails as E

async def main():
    E.kill_existing_daemons()
    await E.step_start_daemon()
    await E.step_navigate()
    await E.step_wait_login()

    s, st = await E.run("state=await browser.get_state_as_text(); print('SR','Search results' in state)")
    if 'SR True' not in st:
        ok = await E.step_do_search(); print("re-search", ok); await asyncio.sleep(2)

    s, out = await E.run("""
import re
state=await browser.get_state_as_text(); lines=state.split('\\n')
for i,l in enumerate(lines):
    if 'list-mail-item' in l and 'id=id' in l:
        near=lines[max(0,i-2):min(len(lines),i+12)]
        if any('them@example.com' in nl for nl in near):
            m=re.search(r'\\[(\\d+)\\]', l)
            if m: print('ITEM:'+m.group(1)); break
""")
    item = re.search(r'ITEM:(\d+)', out).group(1)
    print("open", item); print(await E.run(f"print(await click({item}))")); await asyncio.sleep(2.5)

    ok, prefix = await E.export_single_email()
    print("EXPORT OK:", ok, "PREFIX:", prefix)
    print("EML DIR:", sorted(os.listdir(E.EML_DIR)) if os.path.isdir(E.EML_DIR) else "none")
    print("HTML DIR:", sorted(os.listdir(E.HTML_DIR)) if os.path.isdir(E.HTML_DIR) else "none")

asyncio.run(main())
