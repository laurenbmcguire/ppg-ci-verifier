# Historical reference only: do not deploy this SQLite copy

This document describes the original Postgres/Vercel adapter, not this copy. Its runtime Postgres connector and migration are disabled here, and Vercel configuration is renamed `.disabled`. Use `SQLITE_LOCAL_SETUP.md`.

# Vercel adaptation and spreadsheet upload

This is an isolated adaptation of the November 3 synthetic verifier. The original
ZIP and original `app/` implementation are unchanged. Updated hosting/UI code is
in `api/`, `hosting/`, and `web/`.

## Current status

Implemented and locally tested; **not deployed**. No Vercel project, access change,
database, credentials, paid service, live report connector, or SMS integration has
been created by this adaptation. The connected Vercel app still requires the
user's consent to access the intended team. A dedicated Postgres database and its
project connection also need authorization before provisioning or configuring
persistent credentials.

## Spreadsheet upload

- Accepts UTF-8 CSV and XLSX, 1 byte to 2 MiB.
- Generic files check first-row headers, blank/missing cells, row widths, formulas, and exact
  conventional `inspected`, `accepted`, `rejected`, and `quantity` columns.
- Compares supported totals only when a `Total` or `Grand Total` row is explicit.
- Does not guess column meanings or evaluate formulas. A spreadsheet alone cannot
  establish accuracy or completeness against an independent source.
- PPG and Pratt recognized templates use explicit sheet/header/table mappings and separate hash-bound source comparisons. See BRANDED_REPORT_CHECKS.md for supported revisions and limits.
- Results distinguish internal-only from separate evidence comparison; all remain unapproved and release held. Empty or unmapped quantity checks never pass.
- Originals are hash-addressed and unchanged. Reuploading identical bytes is
  idempotent. Changed bytes under the same filename create a new numbered version.
- Limits: 10,000 total rows, 100 columns, 100,000 cells, 20 MiB expanded XLSX,
  1,000 ZIP members, maximum 200:1 per-member compression ratio, 200 displayed
  findings. Additional findings remain counted. No raw cell values are emitted in
  the finding list.
- Rejects macros, encrypted workbooks, embedded active content, external workbook
  relationships (including hyperlinks), DTD/entities, active/external formulas,
  malformed archives, and unsafe ZIP paths. CSV formula-like cells are flagged.
- File chooser, per-version findings, and upload history are in the new top card.
  The older editable synthetic spreadsheet remains a separate demonstration.

## Persistence and private access

Vercel has an ephemeral filesystem. This implementation **does not** save durable
state in `/tmp`. Every request restores a request-local workspace, runs the
retained SQLite review/intake logic, and commits a complete bounded snapshot to
Postgres under a row lock. Success is sent only after that transaction commits.
Concurrent workers therefore serialize changes. Failed operations roll back.

This is a small shared pilot workspace for authorized preview users, not a
multi-tenant production system or tamper-proof archive. Snapshot maximum is
16 MiB and 2,000 files. Originals, review artifacts, audit state, spreadsheet
uploads, and preview outboxes are included. Database backup/retention policies
must be configured separately before real business use.

All routes, including assets, are behind the Python handler. Only explicitly
allowlisted UI files can be served. Production deployments fail closed. There is
no local-filesystem fallback in Vercel. A missing database returns 503 rather than
pretending that work was saved. API bodies are bounded and result serialization
is checked below 3 MiB **before** commit to stay within Vercel's 4.5 MB limit.

## Authorized deployment steps remaining

1. Approve the connected Vercel app's access to the intended team. Use a fresh
   single-use consent link if the earlier link has expired.
2. Confirm the exact isolated project name and create it with standard Vercel
   Authentication protection. Do not overwrite the existing PPG projects.
3. Authorize provisioning/connecting a dedicated Postgres database, including any
   provider terms or costs and creation/configuration of persistent credentials.
   Keep DATABASE_URL in the platform's secure environment settings, preview only;
   never paste it in chat, source control, or client JavaScript.
4. Apply `hosting/schema.sql` to that dedicated database, or run
   `python3 hosting/migrate.py` with DATABASE_URL securely supplied. Use a dedicated
   least-privilege application role. The runtime needs SELECT/INSERT/UPDATE on
   ci_verifier_state; it does not need schema administration rights.
5. Deploy first with CI_VERCEL_PROTECTED_PREVIEW unset. Verify Vercel Authentication
   blocks anonymous access on deployment and branch URLs, including API routes.
   This is mandatory: the environment flag below asserts platform protection and
   does not replace it. Do not create public bypass/share links for this app.
6. Set the nonsecret preview-only flag CI_VERCEL_PROTECTED_PREVIEW=1, then redeploy
   to **preview**, not production. VERCEL_URL/VERCEL_BRANCH_URL are the only allowed
   hosted request domains. No custom domain or public production alias is enabled.
7. Test the actual Postgres transaction path and authorized hosted UI with synthetic
   CSV/XLSX fixtures. Recheck anonymous rejection, persistence after redeploy,
   parallel edits/imports, conflict rollback, and original-file integrity.

No user membership or Ron access has been added. Access for additional people
requires an explicit authorized sharing/access action. No SMS send exists.

## Local development and verification

Install the pinned dependencies from requirements.txt. Run the complete updated
UI on loopback with:

    python3 hosting/local_server.py --port 8765 --data outputs/local-vercel-demo

This explicit development server saves a local snapshot file and labels it as
local development, not Postgres. It refuses to run on Vercel. The original
`python3 app/server.py` remains available but serves the older UI without upload.

Run automated tests and syntax checks:

    python3 -m unittest discover -s tests -v
    node --check web/ui.js
    node --check web/report-ui.js
    node --check web/spreadsheet-ui.js

Tests cover the retained state machines, upload/parser safety, HTTP authorization,
immutable originals, versioning/idempotency, concurrency via a fake Postgres
transaction adapter, rollback, and response-limit regression. They do not prove
live Postgres or deployed Vercel behavior. The cloud browser could not reach this
command environment's loopback server, so browser interaction/rendering remains
unverified until deployment or an accessible development environment is ready.

## References

- https://vercel.com/docs/functions/runtimes/python
- https://vercel.com/docs/functions/limitations
- https://vercel.com/docs/deployment-protection/methods-to-protect-deployments/vercel-authentication
- https://vercel.com/docs/storage
- https://www.psycopg.org/psycopg3/docs/basic/transactions.html
- https://www.postgresql.org/docs/current/explicit-locking.html
