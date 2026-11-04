# LOCAL SQLITE COPY / prototype

For the new synthetic review app and Accept/Reject workflow, start with **START_HERE.md** and launch with `--demo`.

This isolated copy uses Python's built-in SQLite. It is a **same-computer, synthetic-data prototype**, not an online deployment. Your original verifier and its saved ZIP are separate and unchanged. No remote or phone access is provided. There is no trusted reviewer login or application approval identity; the report approval endpoint remains unavailable. No live connectors or external sending are enabled.

## Start on your computer

Requires Python 3.10+ and the spreadsheet dependencies in `requirements.txt`. The SQLite backend itself has no additional dependency. Install dependencies into a virtual environment if needed:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python hosting/sqlite_server.py

Open `http://127.0.0.1:8766` on **that same computer**. Keep the terminal running. Stop using Ctrl+C. The compatibility command `python hosting/local_server.py` uses this same SQLite backend.

The default durable database is `~/.ci-verifier-sqlite-copy/workspace.sqlite3`. Restart with the same data directory to retain your changes. To choose a different local persistent directory:

    python hosting/sqlite_server.py --data /absolute/path/to/persistent/local-data --port 8766

Do not choose `/tmp`, a container's disposable filesystem, a network share, or a cloud-sync folder for durable storage. The application cannot guarantee that a user-selected filesystem persists or honors SQLite's locking/fsync requirements. A local filesystem with reliable locking and persistent storage is required. Never expose or forward the port; loopback hosting is not multi-user authentication.

## What is stored and committed

Existing unrelated databases are refused before journal or schema changes. A dedicated SQLite application ID and expected schema identify this copy’s database; only an empty unmarked database can be initialized. No existing database is migrated.

A single SQLite row holds a bounded ZIP snapshot of the workspace, including the existing per-feature SQLite databases and original uploaded files. Every request uses one `BEGIN IMMEDIATE` transaction spanning restore, all feature operations, immutable-original validation, snapshot update, and commit. Separate processes serialize using SQLite locks, with a five-second busy timeout. The API reports success only after the transaction commits. The store enables WAL journaling and `synchronous=FULL` on every connection. A killed process or exception before commit leaves the prior committed snapshot intact. Machine/power-loss guarantees still depend on the operating system and storage hardware honoring fsync.

Scratch files are temporary and **not** the durable source. A SHA-256 digest detects snapshot corruption. Compressed and expanded snapshots are bounded at 16 MiB, with at most 2,000 files, allowlisted paths, and no symlinks. Existing `original/` and `uploads/` bytes cannot be modified or removed in a successful application transaction. These are application invariants, not a tamper-proof audit archive: the computer's owner can change the database outside the application. Each local data directory is one workspace, and repeated reads do not increase its revision unless feature initialization changes data.

A busy timeout returns an error without confirming success; reload before retrying. Do not retry blindly after a network interruption because a committed response can be lost. Full snapshots and serialized requests intentionally favor simple correctness over throughput; this is not a scalable multi-user service.

## Safe backup and restore

SQLite WAL may contain committed data not yet copied into the main database. **Do not copy just the live `workspace.sqlite3` file.** Use SQLite's backup API:

    python hosting/sqlite_backup.py /absolute/path/to/new-backup.sqlite3

For a custom directory, append `--data /absolute/path/to/persistent/local-data`. The tool creates a new file, never overwrites an existing backup, includes committed WAL changes, and checks SQLite integrity. Keep backups private. To restore, stop every server using the target directory, choose a **new empty local directory**, copy the backup there as `workspace.sqlite3`, then launch with `--data` pointing to that directory. Do not overwrite an active database or reuse stale WAL/SHM sidecars.

## Hosting boundaries

`DATABASE_URL` is ignored by this copy's local SQLite store and API. There is no Postgres fallback and no remote database connection. Recognized Vercel/serverless environment markers fail closed, even if the Host header claims localhost. Requests also require a loopback-bound server and loopback peer. The launcher binds only `127.0.0.1`. The Vercel configuration is renamed `.disabled`; remote migration and the old `app/server.py` launcher refuse to run. Historical hosting documentation is inherited reference material, **not instructions to deploy this copy**. Vercel/serverless local disk is not durable storage for this application.

If online access is needed later, choose and separately authorize a durable hosted design with trusted authentication, concurrency, backups and appropriate data controls. This copy does not create credentials, connect existing databases, deploy, or transmit real user data.

## Verification

    python -m unittest discover -s tests -p 'test_sqlite_copy.py' -v

Tests use synthetic fixtures and temporary local directories. They cover independent-process concurrency and restart, lock timeouts, process termination, rollback, corruption and immutable-original protections, backups, serverless refusal, ignored DATABASE_URL, and an HTTP spreadsheet upload retained across server restarts.
