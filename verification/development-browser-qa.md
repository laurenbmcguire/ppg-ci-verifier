# Development browser QA status

Real Chromium QA: **NOT RUN / environment blocked**. This is not a browser or phone pass.

Two launch attempts (default execution, then permitted escalated execution) both failed before opening any page with:

`FATAL:chrome/browser/process_singleton_posix.cc:297 Check failed: . socket() failed: Operation not permitted (1)`

Launch code: `playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])`.
Both attempts started a temporary loopback-only ThreadingHTTPServer and Chromium in the same Python execution. No external browser, user computer, real data, accounts, secrets, deployment, or published screenshots were used. Attempts stopped after the persistent environment restriction. No workaround bypass was attempted.

## Reproduce on an authorized machine supporting Chromium IPC

From the development copy root:

`PYTHONPATH=/workspace/scratch/543c0009556c/verifier_work/vercel_deps:. python tests/browser_development_demo.py`

The test uses temporary SQLite storage, actual packaged synthetic fixtures (hash-verified against the development index), and desktop 1440x1000 plus phone 390x844 viewports. Planned assertions cover CSV native file chooser, XLSX upload, JSON source comparisons and all fixture expected counts/findings, individual Accept/Reject/Hold, atomic bulk Accept selected / Reject selected / Hold selected (two proposal IDs in one request, both response decisions checked, unselected decision unchanged), rejected-selection protection, approved-only new draft, refresh persistence, historical locks, overlapping navigation, horizontal overflow, and console/network errors. Assertions beyond fixture preparation have NOT executed in this environment.

Fixture preparation was run successfully with `python tests/browser_development_demo.py --prepare-only`. That intentionally exits 2 and records NOT RUN in development-browser-results.json.

## Checks that did execute

- `node --test tests/test_spreadsheet_ui.js`: 6/6 controlled DOM/fetch tests passed. See development-spreadsheet-dom-tests.txt. This is not a real browser test.
- `python tests/test_development_layout_static.py`: **6/6 passed** after development-examples.js was added. Checks viewport metadata, responsive CSS guards, constrained inputs/hashes, touch-target minimums, and local script presence. See development-layout-static-tests.txt for current results. CSS presence cannot establish actual layout or lack of horizontal overflow.
- `python -m py_compile tests/browser_development_demo.py`: passed.

## Added XLSX bridge coverage (not run)

The reproducible browser script now also plans to open the actual uploaded PPG mismatch workbook through `/api/spreadsheets/demo/review`, test per-item Demo Accept change / Demo Reject change, reject draft creation from a rejected checkbox selection, explicitly select and accept a batch, reselect accepted changes to create a held unapproved Excel draft, download and inspect the changed cell, verify the original fixture hash is unchanged, reload, and verify the superseded old file has no decision/selection/apply controls. The latest-draft navigation control remains available. This browser coverage has **NOT RUN**; backend/HTTP validation is separate evidence and is not a substitute for browser execution. No browser relaunch was attempted.
