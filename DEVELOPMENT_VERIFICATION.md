# Development verification · November 4, 2026

## Executed checks

- Full Python regression: **436 passed**. Full output is in verification/development-python-tests.txt.
- Controlled DOM/fetch tests: **35 passed**, covering actual UI JavaScript with synthetic responses. These are not real-browser assertions.
- Real CLI / loopback HTTP acceptance: **passed**. Started the actual launcher with `--demo`, imported every packaged example, reconciled separate sample evidence, recorded individual/bulk decisions, produced JSON and Excel drafts, downloaded the Excel bytes, stopped the process, restarted against the same SQLite directory, and confirmed persisted uploads, comparison revisions, decisions, draft bytes, downloads and history.
- JavaScript syntax and Python compilation: passed.
- Six static responsive/layout guards: passed. CSS checks do not establish actual phone layout.
- Original preservation: the prior ZIP hash and all **103 files** in the original SQLite copy match their retained version. Development work is in a separate directory.

The final Python log is the authoritative aggregate result. Tests include inherited features, SQLite durability/concurrency/rollback, structural and source checks for supported report templates, daily/shift scope, missing values, formula protection, bounded malicious input handling, synthetic review gates, exact version binding, individual and multi-proposal review, atomic rollback, stale-parent blocking and synthetic Excel export integrity. Existing test modules intentionally reuse some regression cases across model/HTTP fixtures; the count is test executions, not unique business requirements.

## Observed sample outcomes

| Example | Known values compared | Result |
| --- | ---: | --- |
| PPG active tabs | 4 | No numeric differences; rejected values remain unknown |
| PPG mismatch | 4 | One certified-quantity difference, 24 vs 23 |
| Pratt nine-sheet template | 195 | No numeric/audit differences; Main and Multi-pack measures remain separate |
| Plain CSV | Internal checks only | Missing, invalid, negative, fractional and inconsistent quantity findings |

In the actual HTTP Excel correction run, the accepted proposal changed **1st Shift!K17 from 24 to 23**. Every other cell value/formula matched the original, both rejected cells stayed blank, the new source-difference count was zero, the new file had a different SHA-256, and customer release remained held. Formula expressions were preserved; caches were cleared and full recalculation requested. The original remained byte-identical and was locked as superseded.

The structured JSON example had three proposals. The acceptance run applied two explicitly accepted proposals to a new draft and retained the third unresolved proposal. Rejected, held, unreviewed, stale or invalid selections were refused, and real approval remained unavailable.

The XLSX multi-proposal regressions use an isolated, exactly hash-allowlisted synthetic fixture with two changes. They exercise atomic batch acceptance, rollback if the second audit write fails, mixed rejection blocking, accepted-only subset application and supersession. They do not modify the four packaged examples.

## Not executed

**Real Chromium / device visual and interaction QA: NOT RUN.** Chromium could not start because the execution environment denied its local IPC socket, both in normal execution and the permitted escalated attempt. No page opened and no browser assertions ran. No restriction bypass was attempted. The reproducible script tests desktop and phone viewports on a suitable authorized machine; it is included as a test plan, not evidence of a browser pass.

No authenticated reviewer sign-in, actual Chris approval, public/phone access, deployment, remote database, live source retrieval, production writeback, customer release or external communication was tested or enabled. All new testing used synthetic data only.

## Reproduce

With requirements installed, from the package root:

```sh
python -m unittest discover -s tests
node --test tests/test_*ui.js
python -m hosting.run_development_smoke
python tests/browser_development_demo.py
```

The final command requires Playwright and a working Chromium runtime. It is optional for local startup but required before asserting real browser/device acceptance. `--prepare-only` validates fixtures without launching a browser and intentionally reports NOT RUN.

No secrets, real client workbooks, source snapshots, runtime databases or browser screenshots are included in this package. Historical verification files are retained as history; files prefixed `development-` describe this revision.
