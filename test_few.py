#!/usr/bin/env python3
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_emails as E

async def main():
    s, out = await E.run("print('ping')")
    if 'ping' not in out:
        print("DAEMON NOT ALIVE"); return
    s, st = await E.run("state=await browser.get_state_as_text(); print('SR','Search results' in state)")
    if 'SR True' not in st:
        ok = await E.step_do_search(); print("re-search", ok); await asyncio.sleep(2)
    n, _ = await E.process_page(1, limit=4)
    print("EXPORTED:", n)
    print("EML:", sorted(os.listdir(E.EML_DIR)))
    print("HTML:", sorted(os.listdir(E.HTML_DIR)))
    if os.path.isdir(E.ATTACH_DIR):
        print("ATTACH SUBDIRS:", sorted(os.listdir(E.ATTACH_DIR)))

asyncio.run(main())
