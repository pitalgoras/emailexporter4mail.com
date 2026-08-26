#!/usr/bin/env python3
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_emails as E

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None

async def main():
    E.kill_existing_daemons()
    await E.step_start_daemon()
    await E.step_navigate()
    await E.step_wait_login()

    s, st = await E.run("state=await browser.get_state_as_text(); print('SR','Search results' in state)")
    if 'SR True' not in st:
        ok = await E.step_do_search(); print("re-search", ok); await asyncio.sleep(2)

    if LIMIT is None:
        n = 0
        prog = load_progress_safe()
        max_pages = prog.get("max_pages", 0) or 259
        for page in range(1, max_pages + 1):
            if page > 1:
                await E.goto_page(page)
            n += (await E.process_page(page))[0]
    else:
        n, _ = await E.process_page(1, limit=LIMIT)

    print("EXPORTED THIS RUN:", n)
    print("EML DIR:", sorted(os.listdir(E.EML_DIR)))
    print("HTML DIR:", sorted(os.listdir(E.HTML_DIR)))

def load_progress_safe():
    try:
        return E.load_progress()
    except Exception:
        return {}

asyncio.run(main())
