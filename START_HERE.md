# Verifier development review app

This is a separate SQLite development copy of the verifier, with representative **synthetic PPG and Pratt report templates**. It provides a working local review process. It is not a hosted service, does not authenticate Chris or any other reviewer, and cannot release a customer report or write to a client source.

## Run on a laptop

Requires Python 3.10+ and a persistent local disk. From this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python hosting/sqlite_server.py --demo
```

Open **http://127.0.0.1:8766 on that same computer**. Keep the terminal running. On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.

`--demo` explicitly enables simulated decisions. Without it, the new sample decision endpoints stay disabled. The default local database is `~/.ci-verifier-sqlite-copy/workspace.sqlite3`. Use `--data /path/to/a/dedicated/persistent/folder` to keep this development run separate from another local run. Existing unrelated databases are refused, not migrated. Never forward this port or treat local loopback access as multi-user authentication.

## Try the Excel process

1. Choose **PPG: find one source mismatch**. The app imports an unchanged sample workbook and compares separate, invented source evidence.
2. The report has two rows, four known quantities compared, and one difference: **1st Shift, K17, certified quantity 24 → 23**. Rejected quantities are unknown, not zero.
3. In **Sample spreadsheet change review**, use **Demo Accept change** or **Demo Reject change** for one item. Checkboxes and **Demo Accept selected changes / Demo Reject selected changes** support explicit batches. Every decision is tied to the exact file, source and comparison version.
4. Select an accepted proposal, then choose **Create Excel draft from selected accepted changes**. Unreviewed, rejected or held changes cannot be applied.
5. Download the new **unapproved synthetic Excel draft**. Its original file is retained. The new workbook is rechecked, its known source differences fall to zero, and its unknown values remain unknown. The original review is marked superseded and can no longer accept changes.
6. Restart the app using the same data directory. The uploads, decisions, corrected draft, download and history remain available.

Formula expressions and unrelated cells are preserved. Formula caches are cleared and full recalculation is requested, so Excel must recalculate on opening. This app does not execute formulas. A difference count of zero does not establish completeness, source authenticity or business approval.

## Try the batch example

Under **Structured report review example**, select **Load synthetic workflow**. It has three proposals for a JSON report: one row quantity, a daily total and a shift total.

Accept or reject items individually, or select multiple checkboxes and use **Demo Accept selected / Demo Reject selected**. Decisions clear the selections. Select the accepted items you want in the next draft, enter a new version and apply them. Applying two of the three proposals deliberately leaves the third for review. This structured example creates JSON report versions; it is separate from the Excel draft process above.

## Other sample reports

- **PPG: known values agree**: 2 rows, 4 known quantities compared, no numeric differences. Missing rejects and coverage limitations remain visible.
- **Pratt: audit-template checks**: 9 sheets, 5 completed detail rows and 195 numeric/audit comparisons. Main audits and Multi-pack children remain distinct measures. Automatic Pratt workbook corrections are not implemented.
- **CSV: find count issues**: missing, invalid, negative, fractional and inconsistent count findings.

The sample evidence is independently authored fictional fixture data, not independently authenticated business data. Automatic Excel corrections are deliberately limited to exact packaged synthetic PPG workbooks and their audited descendants. Arbitrary uploads can be checked but cannot use these demo correction endpoints.

## Tested and not tested

See **DEVELOPMENT_VERIFICATION.md** for exact test counts and results. Actual local HTTP upload, comparison, decision, Excel/JSON draft creation, download and full process restart were exercised successfully. Controlled DOM/fetch tests exercised the real UI JavaScript, including stale results, repeated clicks and interrupted actions.

**Real browser/phone visual QA was not run:** this cloud environment blocks Chromium's local IPC socket before it opens a page. Responsive styles and static layout guards were checked, but those are not a real-device pass. A reproducible browser script is included for an authorized machine that supports Chromium.

## Access and production boundaries

- The cloud run's localhost address is not accessible from your phone or laptop. Running this package locally lets you use it on that same laptop only.
- Phone/laptop shared access still needs separately authorized hosting, durable storage and trusted reviewer sign-in. This package does not connect the existing Vercel database, create credentials, change security settings or incur hosting charges.
- No real reports or live source snapshots are included. No production source changes, report delivery, messaging or business signoff occurred.
- For storage, backups and inherited feature details, see SQLITE_LOCAL_SETUP.md. Older Vercel documentation is historical reference, not deployment instructions for this SQLite copy.
