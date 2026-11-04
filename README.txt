LOCAL SQLITE COPY / PROTOTYPE
=============================

This is an isolated local-only copy, not an online deployment.
Use synthetic files only. No trusted reviewer login or app approval identity.
No phone/remote access. The original verifier and saved original ZIP are unchanged.

Start from this folder:
    python hosting/sqlite_server.py

Open on the same computer:
    http://127.0.0.1:8766

Try Spreadsheet upload with samples/sqlite-upload-demo.csv. It contains only
synthetic rows. Its checks do not establish independent source verification.

Your durable SQLite database defaults to:
    ~/.ci-verifier-sqlite-copy/workspace.sqlite3

Restarting with the same data directory keeps your work. Stop with Ctrl+C.
Read SQLITE_LOCAL_SETUP.md for dependencies, safe backups, limits, and tests.
Historical Vercel deployment documents are reference only, not setup for this copy.
