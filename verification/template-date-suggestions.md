# Template-aware Date proposals

## Scope

The shared Daily Inspection Summary layout labels B4 as Date, H4 as Shift, and M4 as Area. Its values are in row 5. The earlier generic horizontal-detail inference proposed C4 even when B5 already contained the date.

- That exact layout now reads B5 and proposes B5 only when it is genuinely blank. The mapping is independent of client or job.
- Populated values, invalid dates, numbers, Boolean values, and formulas are not blank and are not replaced by this suggestion.
- Existing merged-cell protection rejects a covered B5 and permits only a valid anchor. An explicitly accepted synthetic correction changed B5 only.
- Incomplete or changed versions of this summary layout and Date/Shift/Area header rows do not fall back to a horizontal date proposal. Other legacy horizontal layouts retain their existing inference, with an additional check that the proposed target is actually empty.
- The text now says “Proposed” rather than “Filled in.” Generating proposals does not edit workbook bytes, review decisions, source evidence, or source holds. Known-detail proposals remain ineligible for bulk recommended application.

This is not a general template detector or date validator. Conservative fallback can omit suggestions for ambiguous horizontal layouts that contain Shift/Area labels or a populated cell beneath Date. Unrecognized layouts without these signals retain the prior horizontal behavior.

## Verification

Synthetic data only; no live report writes, external source captures, deployment, push, or release.

- Fourteen new regression tests cover populated and blank dates; merged/unmerged headers; formulas; invalid values; ambiguous layouts; existing horizontal offsets; proposal wording; read-only generation; preserved source holds; rejected bulk application; and explicit B5-only application.
- Final established verifier environment: **199 passed, 38 subtests passed**. This includes the focused date/source-safety modules and all 69 failures from an earlier run using an incomplete environment. The same focused modules independently passed **130 tests, 25 subtests**.
- Broader collectable Python run: **1658 passed, 35 skipped, 321 subtests passed, 1 failed**. The remaining access test returned 403 rather than 404 because that run omitted CI_LOCAL_DEV=1. It passed in the final established-environment batch above and in the isolated unchanged caacf9a baseline with the required local-mode flags. This is not claimed as an uninterrupted full-suite pass.
- All JavaScript regression tests: **232 passed**. These are controlled tests, not real-browser acceptance.
- Documented synthetic CLI/HTTP smoke: **passed**, including selected correction, unchanged originals, held release, process restart, and persisted audit. The first attempt hit macOS's /tmp symlink safeguard; the successful run used canonical /private/tmp.
- Python compilation, focused Ruff fatal-error checks, and git diff whitespace checks: **passed**.
- Read-only code review: no blocking findings.

The full Python suite cannot collect unchanged tests/test_site_timezones.py because its imported hosting.site_timezones module is absent in the base commit caacf9a. This unrelated module was excluded from broader runs, not removed or repaired here. PostgreSQL tests remained skipped because no disposable database was configured. No real-browser or live-report acceptance is claimed.

The final focused run used the established test-venv interpreter with a clean environment, dedicated home/temp directories, CI_LOCAL_DEV=1, VERCEL=0, PYTHONDONTWRITEBYTECODE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, and no database URL or production credentials.
