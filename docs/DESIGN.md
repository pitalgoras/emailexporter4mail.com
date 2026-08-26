# Design notes — recovery, `--new`, and the download pipeline

This document captures the **design rationale** behind the trickier parts of
`export_emails.py`. It is meant to be read alongside the code and the README.

---

## 1. Why a stable id (`id=id…`) is the right key

mail.com renders every row of the search-results list as:

```
[NNNN]<list-mail-item id=id<number> />
```

That `<number>` is a **permanent, unique identifier** for the email. It does not
change between runs, between pages, or when the list is re-rendered. This makes
it a far better resume key than "page + position" (which shifts as the mailbox
changes) or the on-disk filename (which is derived *from* the export).

Consequences:

- **Recovery from a crashed run** = "re-process everything, but skip any id in
  `exported_ids`." No need to remember how far we got on a page.
- **`--new` (only emails since last session)** = "re-scan every page, but export
  only ids **not** in `seen_ids`." New arrivals simply are not in the set yet.
- Because we always re-collect the ids from the live page (never trusting a
  cached list), the sets stay correct even if emails were deleted/received.

### Set semantics

| Set          | Meaning                                                      | Populated by                          | Cleared by   |
|--------------|--------------------------------------------------------------|---------------------------------------|--------------|
| `exported_ids` | ids with **both** `.html` and `.eml` saved                 | `process_page` on full success        | `--fresh`    |
| `seen_ids`     | every id encountered on any run                            | `process_page` for *every* row        | `--fresh`    |
| `partials`     | name-prefixes missing one format (reporting only)          | `process_page` on a PARTIAL           | on completion|

`--new` skip rule: `seen_ids & exported_ids` (fully-done, already seen → skip;
seen-but-partial → still retry; never-seen → export).
Normal skip rule: `exported_ids` (fully-done → skip; everything else → process).

---

## 2. Collection is "all rows on the page", not "rows near the term"

A search-results page is *already filtered* to `SEARCH_TERM`, so **every**
`list-mail-item` on it is a match. The collector therefore gathers **all**
`list-mail-item id=id` rows and ignores proximity to the search-term text.

An earlier heuristic that required the search term to appear within 12 lines of
a row was **timing-dependent**: right after a search/navigation the list could
render before the term text propagated, yielding **zero** matches and an empty
page. Dropping the heuristic fixed it.

Collection also **retries up to 3× with a short wait** because the list can
appear a moment after navigation.

---

## 3. Download detection — folder-watching, not the browser tracker

The naive pipeline was:

```
click Save(.html)  ->  poll browser.downloaded_files  ->  match by extension
```

Problem: for this daemon `browser.downloaded_files` was unreliable — the file
landed in `PROGRESS_DIR` but the tracker often did **not** report it. The poll
then saw "no file", `_try_save` concluded the click failed, and **re-clicked**,
producing 3× duplicates of every email.

The robust pipeline (current):

```
before = _dir_snapshot()          # {filename: mtime} of PROGRESS_DIR
click Save(.html)
got   = _wait_for_new_file(before, '.html', timeout=15)  # poll dir for a NEW
                                                       #   file ending .html
return os.path.join(PROGRESS_DIR, got)
```

`_wait_for_new_file` matches on `filename.endswith(suffix)`, so a Chrome partial
(`foo.html.crdownload`) is ignored until the completed `foo.html` appears. The
first successful detection returns immediately — **no re-click, no duplicates**.

Attachments (`_download_one`) use the same directory-diff technique instead of
the browser tracker.

`_collect_downloads()` / `DOWNLOAD_SEEN` (browser-tracker based) are retained for
backwards-compat but are no longer on the hot path.

---

## 4. "Done" means BOTH formats

`export_single_email()` returns `(saved_html, saved_eml, name_prefix)`. A result
is only a success when **both** are truthy. One-format results are PARTIALs:

- recorded in `progress.json["partials"]`,
- retried within the same run (per-email, up to `MAX_RETRIES`),
- and retried again on any later run (RECOVER mode), never dropped.

This guarantees `eml/` and `html/` stay in lock-step.

---

## 5. Page-level resilience

Within `process_page`, after a pass:

- if a real export happened (`did_work`) and a row failed, the list is reloaded
  and the remaining rows retried;
- if **nothing** was exported that pass (the first item keeps failing), the page
  is abandoned to avoid an infinite reload loop (`"stuck reloading, stopping
  page"`);
- `exported_ids` / `seen_ids` are persisted on **every** successful export, so an
  interruption mid-page loses at most the current email.

---

## 6. Attachment handling

mail.com exposes **two distinct controls**, and which one appears is the signal
for how many attachments there are:

- a single **Download (to hard disc)** button → exactly **one** attachment;
- a **Download all files (ZIP)** button → **two or more** attachments;
- neither present → **no** attachments.

So the ZIP button's presence is the discriminator (never count individual
buttons to decide "multi"). When the ZIP button is present we download it once,
extract in place (original filenames preserved), and keep the ZIP renamed to
`attachments/<name>/<name>.zip`. Otherwise we click the single button (with one
retry). The UI is the reliable source — several `.eml` files are
multipart/alternative with no attachment parts, yet the page shows buttons.

### Correct download relocation (bug fixed)

`_download_one()` watches `PROGRESS_DIR` for a new file and **moves it with its
full path** (`os.path.join(PROGRESS_DIR, got)`). An earlier bug renamed using a
bare filename (relative to the script CWD), so the rename failed with
`No such file` and the attachment was left as a top-level stray — surfacing as a
misleading `[attach] NO DOWNLOAD for button […]`. The poll also now **ignores
`.crdownload`/`.part` temp files**, waiting for the completed file.

### Failed-attachment recovery

An email is only "fully done" when **both** formats *and* its attachments were
obtained. `export_single_email()` returns an `att_ok` flag; `process_page` adds
the id to `attachment_ids` only when `att_ok` is true. The run's `skip_ids`
therefore requires `exported_ids & attachment_ids`, so an email whose
attachments failed stays out of `attachment_ids` and is **re-processed on the
next run** (re-saving the formats and re-fetching attachments) until complete.
`progress.json` tracks `attachment_ids` alongside `exported_ids`/`seen_ids`.

---

## 7. Stray cleanup

`cleanup_strays()` runs at the end of every run:

1. Hash every file already correctly filed under `eml/`, `html/`, `attachments/*/`.
2. For each top-level `.eml`/`.html`/`.zip` in `PROGRESS_DIR`:
   - byte-identical to a filed file → **delete** (true duplicate);
   - otherwise → move to `unsorted/` for manual review (never silently deleted).

This recovers the "downloaded but never filed" files left by older runs without
risking data loss.

---

## 8. Run modes flowchart

```
start
  │
  ├─ --fresh ─────────────► archive all, clear ids, page 1, re-search
  ├─ --new ───────────────► page 1, re-search, skip = seen_ids & exported_ids
├─ disk partials / attachment gaps ─► RETRY: re-scan ALL pages, skip = exported_ids & attachment_ids
└─ else (all complete) ───────────► resume from saved last_page, skip = exported_ids & attachment_ids

for each page:
    collect all id=id rows (retry if empty)
    for each row not in skip_ids:
        open email
        export .html + .eml (+attachments)
        success  -> add id to exported_ids, persist
        partial  -> record in partials, retry
        fail     -> reload page, retry (bounded)
cleanup_strays()
persist seen_ids, exported_ids, last_page
```
