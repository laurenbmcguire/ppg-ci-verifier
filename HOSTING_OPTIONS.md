# Where this SQLite copy can run

This package is a local prototype. It has not been deployed, and it is not a phone-accessible website. No existing remote database, Vercel project, credentials, or account settings were changed to build it.

## Current choice: same-computer SQLite

Python includes SQLite. The copy stores its workspace in a local SQLite database and does not require a database service, account, subscription, or credential. There is no new service charge for this implementation. The computer and its disk still need to remain available; keep backups. A temporary cloud workspace is not a persistent hosted service.

Use the loopback launcher and the instructions in SQLITE_LOCAL_SETUP.md. Do not expose its port. This prototype does not include trusted user authentication or business approval identity, and it is restricted to synthetic test data.

## Why not put this local database on Vercel?

Vercel Functions have ephemeral, non-shared local storage. A file may disappear between invocations and is not a common database across instances. Fluid Compute does not provide a guarantee of persistent SQLite storage. This copy rejects recognized serverless environments rather than silently storing approvals or uploads in disposable files.

Official source: https://vercel.com/kb/guide/is-sqlite-supported-in-vercel

## If phone and laptop access are needed later

Two possible designs require separate selection and authorization:

1. A private application server with a persistent local disk, SQLite on that same server, real user authentication, HTTPS, backups, and access controls. Hosting and operation costs depend on the selected provider. A shared network drive or cloud-sync folder is not a substitute for a database server.
2. A protected Vercel app using a remote SQLite-compatible database service through its supported network client. Turso currently offers a $0/month tier and an HTTP-based Python client suitable for stateless compute. This requires a separately authorized database/account, persistent credential, adapter implementation and testing, and review of provider terms, access, limits, retention, and costs. No service has been created or connected. Existing Vercel hosting charges or usage are not included in the database tier's price.

As checked November 4, 2026, Turso's published free tier lists 5 GB storage, 500 million monthly rows read, and 10 million monthly rows written. Plans, limits, and prices can change; re-check before selecting a service. Do not enable paid plans, automatic extra usage, or new credentials without approval.

Official sources:
- https://www.sqlite.org/whentouse.html
- https://www.sqlite.org/useovernet.html
- https://turso.tech/pricing?frequency=monthly
- https://docs.turso.tech/sdk/python/quickstart

SQLite permits one writer at a time. This copy serializes complete workspace transactions and has bounded snapshot storage; it is a small prototype, not a scalable multi-user service. A remote database alone would not provide trusted reviewer identity or authorize real client-data processing.
