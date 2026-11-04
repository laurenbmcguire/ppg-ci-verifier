# Local SQLite copy verification

Verified 2026-11-04 with synthetic fixtures only. No remote database, existing production database, credentials, or real data was used.

- **377 Python tests passed:** 353 inherited feature/protocol tests plus 24 new SQLite acceptance tests. The inherited protected-Vercel-host assertion was intentionally changed to require refusal in this local-only copy. The legacy transaction fake now lives under `tests/legacy_storage_fake.py`; it has no network connector and is not a production storage backend.
- **6 Node DOM/fetch tests passed:** inherited spreadsheet comparison UI race and pending-state behavior.
- Actual `hosting/sqlite_server.py` launcher smoke passed; health reports `local-sqlite`, prototype/local-only, no live connectors, no sending, and no reviewer identity.
- Vercel launcher refusal and disabled legacy migration/server entrypoints passed.

Commands:

    python -m unittest discover -s tests -p 'test_*.py'
    python -m unittest discover -s tests -p test_sqlite_copy.py -v
    node tests/test_spreadsheet_ui.js

The 24 SQLite tests cover persisted state after independent-process restart, WAL/FULL/busy-timeout configuration, unchanged revision on read, cross-file rollback, four concurrent writer processes completing 48 increments without lost updates, lock timeout, killed processes both during feature operations and after an uncommitted database update, injected commit failure, immutable original/upload bytes and deletion rejection, file/byte limits, symlink rejection, corrupted/type-invalid/oversized snapshots, byte-for-byte preservation of unrelated databases and unknown application IDs, ignored DATABASE_URL, serverless refusal, live-WAL consistent backup/restore, HTTP upload and server restart, HTTP commit-failure response without persisted writes, non-loopback binding refusal, and Host/Origin/no-fallback restrictions.

Logs: `verification/sqlite-copy-python-tests.txt` and `verification/sqlite-copy-acceptance-tests.txt`. Additional independent multi-server HTTP stress results are in `verification/sqlite-process-http.json`.

These are local correctness/prototype tests, not a penetration test, production certification, or proof against a computer owner modifying the database. SQLite crash recovery was tested with process termination; actual hardware power-loss behavior was not physically tested.
