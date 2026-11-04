# Hosted preview on Vercel (added Nov 4, 2026)

This copy now has two run modes. Local SQLite behavior is unchanged.

| | Local (unchanged) | Vercel protected preview |
|---|---|---|
| Launch | `python hosting/sqlite_server.py --demo` | `vercel deploy` (preview target only) |
| Storage | `~/.ci-verifier-sqlite-copy/workspace.sqlite3` | `hosting/postgres_storage.py`: one locked Postgres row |
| Gate | loopback host + loopback peer | `VERCEL=1`, `VERCEL_ENV=preview`, `CI_VERCEL_PROTECTED_PREVIEW=1`, Host must equal `VERCEL_URL`/`VERCEL_BRANCH_URL` |
| Access control | same computer only | Vercel Authentication (SSO protection = all) on the project |
| Demo endpoints | `--demo` | disabled (never on Vercel) |

## What is deployed

- Vercel project: `ppg-ci-verifier` (team Lauren McGuire LLC, Hobby plan, $0).
- `vercel.json` rewrites every path to `api/index.py` (Python 3.12 per `.python-version`).
- Env vars (preview target only): `CI_VERCEL_PROTECTED_PREVIEW=1` (plain) and `DATABASE_URL` (sensitive, never printed).
- Production deployments are refused by `hosted_preview()`; keep deploying to preview.

## Database (free)

Supabase free plan allows 2 active projects and both were in use, so the app uses an **isolated schema** inside the existing Supabase project `project-hub` (ref `pzveyaatkfsruhtxondj`, us-east-1):

- schema `ci_verifier`, table `ci_verifier.ci_verifier_state` (from `hosting/schema.sql`)
- login role `ci_verifier`: `search_path=ci_verifier`, no rights on `public`, only `SELECT/INSERT/UPDATE` on that one table, connection limit 10
- connection via the Supabase session pooler on port 5432 (IPv4), `sslmode=require`
- nothing pre-existing in that project was altered; dropping schema `ci_verifier` and role `ci_verifier` removes everything

To move to a dedicated database later (e.g. Neon free tier from the Vercel Marketplace), apply `hosting/schema.sql` there and change `DATABASE_URL`; the whole workspace is one row, so copy `payload`/`revision` across.

## Redeploying after changes

```sh
cd <this folder>
python -m unittest discover -s tests        # 443 tests
vercel deploy                               # preview; never `--prod`
```

Then open the deployment URL while signed in to Vercel. Anonymous visitors get the Vercel sign-in wall; `/api/health` should report `"storage": "vercel-postgres"`.

## Tests added

`tests/test_hosted_preview.py` covers the host gate (only deployment URLs, only preview, only with the flag) and Postgres commit semantics (commit, rollback on caller error, commit failure never reported as success, missing URL fails closed). One inherited assertion in `tests/test_vercel.py` that required a preview host to always be denied was updated to the new behavior.
