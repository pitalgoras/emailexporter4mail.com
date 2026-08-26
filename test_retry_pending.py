import asyncio, tempfile, os
import export_emails as E

tmp = tempfile.mkdtemp()
E.PROGRESS_DIR = tmp
E._SAVE_DELAY = 0.0
E.DEBUG = False
E._BOGUS_IDS = set()
E._FAIL_COUNTS = {}
E._PENDING = set()
E._CONSEC_FAILS = 0
E._ON_EMAIL_PAGE = True

fake_prog = {"bogus_ids": [], "fail_counts": {}, "exported": 0, "exported_ids": [],
             "attachment_ids": [], "seen_ids": [], "exported_map": {}}
E.load_progress = lambda: dict(fake_prog)
E.save_progress = lambda p: fake_prog.update(p)

async def noop(*a, **k):
    return None
E._ensure_list_only = noop
E.visible_index_of = lambda *a, **k: 0
async def open_ok(eid):
    return True
E._open_by_eid = open_ok
E._back_to_list = noop
E.goto_page = noop
async def nav_next(*a, **k):
    return 0
E.navigate_next = nav_next

calls = {"n": 0}
async def fail_ese():
    calls["n"] += 1
    return (False, False, "", False, None,
            {"reason": "eml_missing", "date": "x", "subject": "y"})
async def ok_ese():
    calls["n"] += 1
    return (True, True, "p1", True, None,
            {"reason": "ok", "date": "x", "subject": "y"})

def fresh():
    return set(), set(), set()

async def run_rows(ese):
    E.export_single_email = ese
    s, e, a = fresh()
    return await E.export_rows([(0, "eid1", "inbox")], page_label="1",
                               seen_ids=s, exported_ids=e, attachment_ids=a)

# Run 1: always fails -> after BOGUS_THRESHOLD should be retry-pending + diag written
BOGUS = E.BOGUS_THRESHOLD
res1 = asyncio.run(run_rows(fail_ese))
assert E._PENDING == {"eid1"}, f"expected eid1 pending, got {E._PENDING}"
assert os.path.exists(os.path.join(tmp, "diagnostics", "eid1.json")), "diag json missing"
assert os.path.exists(os.path.join(tmp, "diagnostics", "eid1.ax.txt")), "diag ax missing"
print(f"PASS run1: res={res1} pending={E._PENDING} calls={calls['n']} diag_ok=True")

# Run 2: simulate a NEW run start: guard empty, pending kept loaded from prog
E._BOGUS_IDS = set()
res2 = asyncio.run(run_rows(ok_ese))
assert E._PENDING == set(), f"expected pending cleared, got {E._PENDING}"
print(f"PASS run2: res={res2} pending={E._PENDING} (retry succeeded)")

print("ALL TESTS PASSED")
