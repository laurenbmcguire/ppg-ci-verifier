# Hosted preview on Vercel (added Nov 4, 2026)

This copy now has two run modes. Local SQLite behavior is unchanged.

| | Local (unchanged) | Vercel protected preview |
|---|---|---|
| Launch | `python hosting/sqlite_server.py --demo` | `vercel deploy` (preview target only) |
| Storage | `~/.ci-verifier-sqlite-copy/workspace.sqlite3` | `hosting/postgres_storage.py`: one locked Postgres row |
| Gate | loopback host + loopback peer | `VERCEL=1`, `VERCEL_ENV=preview`, `CI_VERCEL_PROTECTED_PREVIEW=1`, Host must equal `VERCEL_URL`/`VERCEL_BRANCH_URL` |
| Access control | same computer only | Vercel Authentication (SSO protection = all) on the project |
| Demo endpoints | `--demo` | disabled (never on Vercel) |

## Live URL

**https://ppg-ci-verifier-git-preview-lauren-mc-guire-llc.vercel.app** — sign in with the Vercel account (Lauren McGuire LLC team). Anonymous visitors are redirected to the Vercel login. Verified Nov 4, 2026: `/api/health` → `"storage": "vercel-postgres"`, first snapshot committed as revision 1.

## What is deployed

- Vercel project: `ppg-ci-verifier` (team Lauren McGuire LLC, Hobby plan, $0).
- `vercel.json` rewrites every path to `api/index.py` (Python 3.12 per `.python-version`).
- Env vars (preview target only): `CI_VERCEL_PROTECTED_PREVIEW=1` (plain) and `DATABASE_URL` (sensitive, never printed).
- Production deployments are refused by `hosted_preview()`; keep deploying to preview.

## Database (free)

Supabase free plan allows 2 active projects and both were in use, so the app uses an **isolated schema** inside the existing Supabase project `project-hub` (ref `pzveyaatkfsruhtxondj`, us-east-1):

- schema `ci_verifier`, table `ci_verifier.ci_verifier_state` (from `hosting/schema.sql`)
- login role `ci_verifier`: `search_path=ci_verifier`, no rights on `public`, only `SELECT/INSERT/UPDATE` on that one table, connection limit 10
- connection via the Supabase session pooler `aws-0-us-east-1.pooler.supabase.com:5432` (IPv4), user `ci_verifier.<project-ref>`, `sslmode=require` (the `aws-1` pooler does not know this project's tenant)
- nothing pre-existing in that project was altered; dropping schema `ci_verifier` and role `ci_verifier` removes everything

To move to a dedicated database later (e.g. Neon free tier from the Vercel Marketplace), apply `hosting/schema.sql` there and change `DATABASE_URL`; the whole workspace is one row, so copy `payload`/`revision` across.

## Uploads: no file-size limit (Nov 4, 2026)

- The browser hashes the file (SHA-256), sends it in 3 MiB parts to `/api/spreadsheets/upload/part`, then calls `/api/spreadsheets/upload/finish`; the server reassembles, verifies the hash, and runs the same checks as before. Missing parts or a hash mismatch save nothing.
- Hosted mode stores originals in `ci_verifier.ci_verifier_files` (write-once: the app role has no UPDATE/DELETE) and parts in `ci_verifier.ci_verifier_upload_parts` (`hosting/files.py`). The per-request workspace snapshot now holds only metadata, so its 16 MiB cap no longer bounds uploads.
- Local SQLite mode is unchanged: originals and parts stay inside the snapshot, so the local copy is still bounded by that 16 MiB cap.
- Parser budgets (`hosting/spreadsheets.py`): Excel's own 1,048,576 rows × 16,384 columns, plus 2,000,000 cells per workbook (the template checkers hold sheets in memory). XML safety preflight now streams (`iterparse`) instead of loading each sheet's XML whole.
- Remaining real ceilings: Supabase free plan = 500 MB database total; Vercel function 300 s / memory tier. Very large workbooks take longer to check.

## Redeploying after changes

Source of truth is GitHub: `laurenbmcguire/ppg-ci-verifier` (private). `main` is Vercel's production branch, and the app refuses production, so deployments come from the `preview` branch:

```sh
python -m unittest discover -s tests        # 443 tests
git push origin main                        # keep history on main
git push origin main:preview                # this is what deploys
```

Then create the deployment from the `preview` ref (Vercel dashboard → Deploy, the Vercel MCP `create_deployment` with gitSource ref `preview`, or `vercel deploy` from a checkout). A stray deploy of `main` is harmless: it returns 403 on every route.

`CI_DEBUG_LOG=1` (preview env var) writes storage exception type/location and the underlying driver message to Vercel runtime logs — never request bodies. Remove it when diagnostics are no longer needed.

Then open the deployment URL while signed in to Vercel. Anonymous visitors get the Vercel sign-in wall; `/api/health` should report `"storage": "vercel-postgres"`.

## Tests added

`tests/test_hosted_preview.py` covers the host gate (only deployment URLs, only preview, only with the flag) and Postgres commit semantics (commit, rollback on caller error, commit failure never reported as success, missing URL fails closed). One inherited assertion in `tests/test_vercel.py` that required a preview host to always be denied was updated to the new behavior.

## AI review (possible issues)

Each uploaded report is also read by Claude, which lists possible issues (duplicates, typos, odd numbers,
missing info, totals, outliers) for a person to double-check. Suggestions never change the file, the
automatic checks or the release status. **Run AI review** in a report's detail re-runs it.

Turn it on: Vercel → ppg-ci-verifier → Settings → Environment Variables (Preview):

| Name | Value | Type |
|---|---|---|
| `ANTHROPIC_API_KEY` | an API key from console.anthropic.com | **Sensitive** |
| `AI_REVIEW_MODEL` | optional, default `claude-sonnet-5-5` | Plain |

Then redeploy the `preview` branch. The report's cell contents are sent to the Anthropic API for each review.

## Common data issues checked

Besides the quantity, duplicate, typo, date and totals checks, every report is checked for these common
data-entry and spreadsheet problems (`hosting/common_issues.py`):

| Check | Example |
|---|---|
| Excel error values | `#REF!`, `#N/A`, `#DIV/0!` in a cell or formula result |
| Hidden / garbled characters | non-breaking spaces, zero-width characters, `Ã©`-style encoding damage |
| Hidden sheets, rows, columns with data | data that is counted but not visible (empty template formulas are ignored) |
| Same value written differently | `Holes / Glue` vs `holes/glue`; `8010-808539` vs `8010808539` (likely duplicate scan) |
| Placeholders | `TBD`, `xxx`, `same as above`, `0000`, `9999` |
| Scientific-notation codes | `8.01081E+09` (Excel dropped digits) |
| Value in the wrong column | part number in the Identifier column, identifier or Excel date serial in a quantity column |
| Quantity outliers | an extra or missing zero (`340` among `34`s) |
| Blank in a filled column | date / shift / auditor / area missing on one row |
| Zero inspected | a row with a part/identifier but 0 inspected |
| Dates typed as text | among real dates |
| Comment counts | `3 holes 2 glue` but Rejected is 4 |
| Condition columns | LF/RF/LR/RR marked SUSPECT vs the suspect quantity; unexpected condition values |
| Labeled totals | `Total … Inspected` cell vs the rows, including different per-vehicle units |

Column detection also understands descriptive headers (`Quantity Inspected 8 Wheel Weights per Vehicle`,
`Suspect Wheel Position Quantity`, `VIN Inspected`, `LF Condition`) and unlabeled `=SUM()` total rows.

## Compared with the usual day

After each upload the report's scans (rows), parts inspected, rejects and reject rate are compared with the
same job's usual day: the median of up to 15 earlier days, taken from reports saved in the verifier and, when
there are fewer than 5, from the job's earlier SharePoint daily exports. At least 3 earlier days are needed.
Flagged: scans or inspected below half or above double the usual (high below a quarter / above 4×), and a
reject rate that moves 15+ points and doubles or halves. Shown in the pop-up ("Usual day") and the report detail.
