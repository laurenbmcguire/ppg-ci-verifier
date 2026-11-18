# Last-scan drafts: pilot handoff

Branch `claude/durable-report-runner-20261118`, local commits only. This is the operator's one-page view of **what
exists in this branch's code**, and the least that has to be supplied before each next step.

**Scope.** No real-source validation evidence is recorded for this isolated runner branch; separate reporting work
is outside this document. "Not recorded here" below means exactly that, and says nothing about what has been done
elsewhere. Evidence that already exists from that work can be brought in through the intake table.

**Not checked for this document:** any deployment, the hosted app, any schedule, any credential, any real list.
"No production wiring" below describes the reviewed branch, not any live environment.
**Nothing here is an approval.** No real client data, job number or file belongs in this document or in the repository.

**The requirement.** A draft is due **one hour after the last scan**. That is Lauren's explicit requirement, not a
placeholder, and nothing below reopens it. What has to be verified for it to work is which column is the actual
scan, the shift and lunch policy, and how late-arriving rows are handled.

## Three tracks, kept separate

| | A. Local, source-backed draft compared by a person | B. Automated one-hour runner | C. Approved automated delivery to Lauren |
|---|---|---|---|
| What it is | A person runs a read-only draft for **one** job, date and shift and gives it, with its checklist, to Lauren beside the report built the usual way. | The runner notices one hour without a scan and builds the held draft itself. | A provider carries the draft or a notice to Lauren, the user-authorized private recipient. **Never to a client.** |
| In this branch's code | Draft builder with the pinned native template, photos, issue checklist and source binding; a manual schema capture; an offline readiness check and inventory. | Entry point, runner, durable store, authority contracts, drift checks. **No production wiring:** no route, schedule or production table creation. (Tests and the inventory script import it; tests create disposable tables.) | A send door, an outcome contract and a reconciliation boundary. **Provider registry is empty; no sender or status adapter is implemented.** |
| Exercised in this branch on | Synthetic rows only. | Synthetic fixtures and fakes only (store also on local PostgreSQL). | Fakes only. |
| Needs first | Intake rows 1 to 9 below. | Everything in A, plus rows 10 to 13. | Everything in B, plus row 14. |
| Depends on the next track? | **No.** | No. | n/a |

**A does not wait for B or C.** A useful real draft in Lauren's hands needs correct source meaning and a person to
run and compare it. It does not need a scheduler, an approval authority or a provider. B automates A once A has
been right repeatedly; C is last. Release to a client is Lauren's decision in every track and is never automated.

## Track A: what it takes, and its bounds

1. **One list, chosen by the root owner.** Exact site id and list GUID from the registry of record (names and URLs
   are refused).
2. **Schema capture by hand** (`docs/LAST_SCAN_SCHEMA_CAPTURE_RUNBOOK.md`): GET only, columns only, no rows, no
   writes; artifact kept outside the repository.
3. **Source meaning settled by people** (rows 1 to 3): which column is the actual scan time, how shift values map,
   lunch and late-arrival handling. Nothing in code decides these, and none is ever marked verified automatically.
4. **One draft for one past shift** whose report was already built the usual way, run by the root owner with
   read-only access. *How the rows are read for this has no production wiring on this branch*: the capture code
   takes a transport the owner must supply, and the administrator review workflow described in
   `docs/SOURCE_FIRST_DAILY_PILOT.md` is the other candidate. Which one is used, and whether either is deployed, is
   an owner decision and not recorded here.
5. **Side-by-side comparison** by the report owner (rows 4 to 6), recorded as differences, not as pass/fail by the
   person who configured it.

**The draft is always delivered.** Whatever draft is available goes to Lauren with its checklist, privately, even
when a photo is missing, a shift value is unmapped or a difference is unexplained. Each gap is flagged on the
checklist; nothing is filled in, and no blank becomes a zero. Lauren decides hold or release. Gaps stop the pilot
from being *accepted*; they never withhold the draft.

Bounds for A: read-only toward the source; one list, one date, one shift; no schedule; no automated send; blank
quantities shown as unknown, never zero; the existing reporting process continues unchanged and remains the report
of record.

## Minimum evidence intake

"Supplied by" and "verified by" are **roles**, never the same person for one row, and not assigned here.
Roles: *root owner* (owns the app and the read-only access), *site lead* (knows the floor and the tablets for that
job), *report owner* (builds and sends the end-of-shift report today), *independent reviewer* (did not configure
it), *business approver*, *hosting owner*, *provider*. The last column is what this branch has on record, not what
anyone knows.

| # | Evidence | Track | Supplied by | Verified by | Acceptable artifact | Not acceptable | Recorded for this branch |
|---|---|---|---|---|---|---|---|
| 1 | **Actual scan time: which column, and what fills it** | A | site lead | independent reviewer | Written statement naming the column and how it is filled (tablet at scan, or typed), plus one real row, viewed on the approved machine, where it visibly differs from Created and Modified; who, when, where the record is kept. | Column name alone; Created, Modified, Date or a business date; a "verified" flag; inference from sample values. | Nothing recorded. |
| 2 | **Shift mapping** | A | site lead | report owner | Every value that appears in the shift column for the pilot dates, each mapped to a report shift or to "no report"; how overnight work is dated. | Shift inferred from the clock time of a scan; a default list of shifts. | Nothing recorded. |
| 3 | **Lunch, pauses and late rows around the one-hour rule** | A, B | site lead | report owner | Scheduled end per shift and date with timezone; longest normal pause (lunch, breaks, changeover) and what should happen when it exceeds an hour; whether tablets work offline and the latest a row has arrived after its scan; what a later row should do to a draft already made. | Changing the one-hour rule to fit; treating one hour without a scan as proof the shift is complete. | Nothing recorded. As built: a later row supersedes the draft and restarts the hour; a stated shift end holds the draft until then. |
| 4 | **Report template and header** | A | report owner | independent reviewer | The exact native template in use for that client, kept outside the repository; header values (paying client, worksite, part name) confirmed in writing; one already-sent report for the comparison shift. | A recreated or "similar" template; header values copied from a previous draft. | No comparison recorded. |
| 5 | **Photos** | A | root owner | report owner | One real row with a defect photo read through the read-only access; which photos the report owner would have chosen. | Assuming photos are readable because rows are. | Nothing recorded. |
| 6 | **Issue checklist and unknowns** | A | report owner | independent reviewer | For the comparison shift: each checklist line marked real problem, expected, or noise; each blank quantity confirmed as unknown at the source. | Filling a blank; treating blank NOK as zero; dropping a line to make the draft clean. | Nothing recorded. |
| 7 | **Registry identity** | A | root owner | independent reviewer | Registry-of-record entry with client key, exact site id, list GUID and job, and who confirmed it. | Site or list name; a seed entry. | Nothing recorded. |
| 8 | **Schema capture** | A | root owner | independent reviewer | Capture artifact per the runbook, off-repository; the short fact list the runbook asks for (completed or refused where, paging, properties needing review). | Screenshot of column settings; a capture by anyone else or by a job. | No capture recorded. |
| 9 | **Read-only access proof** | A | root owner | independent reviewer | Statement of what the existing registration can read, last time it worked, expiry; **no key or token anywhere**. | Widening permissions to make a run work. | Nothing recorded. |
| 10 | **Coordination with existing scheduled builds** | B | root owner | report owner | List of jobs the existing scheduled task already builds, and for each: stays there (marked externally built here) or moves, decided in writing. | Editing or pausing that schedule as part of this work. | Nothing recorded. |
| 11 | **Hosting, schedule, store** | B | hosting owner | independent reviewer | Reviewed route change; approved table migration and connection-limit decision; who calls it, how often, with what identity. | Reusing an open-app trigger; a shared static token. | No production wiring in this branch. |
| 12 | **Identity, approval, capture authority, schema probe** | B | hosting owner | independent reviewer | A real system for each, outside the runner's write access, meeting the version 1 contracts in `docs/LAST_SCAN_REHEARSAL.md`. | Test fakes; records kept where the runner or config editor can write. | Interfaces and test fakes only. Not designed here on purpose. |
| 13 | **Recovery and operators** | B | business approver | root owner | Named operator roles, how they sign in, where held and uncertain items are seen, who may reconcile. | "Someone will check." | No operator screen in this branch. |
| 14 | **Provider evidence, for delivery to Lauren** | C | provider | independent reviewer | In writing: one delivery per idempotency key; a final per-attempt status distinct from "not seen yet"; a closed attempt rejected if late; the deadline it enforces; how recipient and bytes are proven; then a reviewed code change listing the contract. | Marketing claims; a successful test send. | No provider listed in code. |

The offline inventory (`docs/LAST_SCAN_REHEARSAL.md`, "Readiness inventory") turns a non-secret packet of this
intake into a blocker list. It cannot verify any row and never reports readiness.

## Reading rules that must not be bent

- **Duplicate-looking rows are review candidates, not proof of double billing.** Two rows with the same identifier
  may be a rescan, a correction, a second part or a sync artefact. List them; a person decides.
- **A clock time proves neither the shift nor the one-hour timing.** Shift comes from the shift column and row 2;
  "last scan" comes only from the column settled in row 1.
- **Created, Modified and dates are not scan times**, and no mapping is ever marked verified by code.
- **Blank is unknown**, shown as unknown, and totals that depend on it are withheld.
- **One hour after the last scan makes the draft due; it does not prove the shift is complete.** A later scan
  supersedes the draft.
- **A hash, flag or signature in a file proves nothing about where the file came from.**

## Acceptance and stop criteria

| Track | Accept the step when | Stop the step (and what still happens) |
|---|---|---|
| A | Rows 1 to 9 exist with two roles each; for at least three past shifts covering each shift in use, every difference between draft and sent report is explained in writing by the report owner; no unknown was filled. | **Not accepted, draft still delivered:** a missing photo, an unmapped shift value, an unexplained difference, or a scan column not yet shown to differ from Created/Modified. The available draft and checklist go to Lauren with the gap flagged, no invented zeros; she decides hold or release. **Stop the run itself** only if it would need a write to the source, a permission change, or a change to an existing schedule. |
| B | A has been accepted; rows 10 to 13 exist; a dry run on the real configuration reports its snapshot digest and that exact digest is approved in a real authority; a watched run on one list matches A's result. | Any gate is satisfied by a fake or a packet; the existing schedule would have to change; the store migration or connection limit is undecided; the host clock or schema drifts. Track A continues by hand meanwhile. |
| C | B has been accepted; row 14 is reviewed and listed in code; recipient is Lauren only. | Any uncertain delivery is held for a person and never resent automatically; the draft stays readable privately. Delivery to a client is not part of this track at all. |

The number above ("three shifts") is a proposal for the business approver to set, not an existing rule.

## Rollback and read-only bounds

- **A:** nothing to roll back. Nothing is written to the source, the app or a schedule. Delete the private draft and
  the off-repository capture if the pilot stops.
- **B (when it exists):** set `enabled` to anything other than exactly `true`; the entry point then refuses every
  run. Withdraw the approval at the authority. The tables hold only runner state and held drafts; removing them is
  a separate approved step. Withdrawal at an authority **after** the last re-check and before a read or send is not
  seen until the next re-check: it is not atomic.
- **C (when it exists):** remove the contract from the code registry; every send is then refused. A message already
  handed to a provider cannot be recalled; an uncertain one stays uncertain until the provider states its outcome
  and a person reconciles it. No exactly-once claim is made.
- **Always:** read-only toward SharePoint; no credential or permission change; existing scheduled builds are
  coordinated (row 10), never modified; nothing is delivered to a client by this code.

## Concrete blockers now

1. No real-source validation evidence is recorded for this branch: the actual-scan column (row 1) and the shift
   mapping (row 2) have to be supplied and verified, from existing work or new.
2. Around the one-hour requirement, the lunch and pause policy and late-arrival handling (row 3) are not recorded.
3. No comparison of a draft from this branch with a sent report is recorded; template, header, photo access and
   checklist review (rows 4 to 6) have to be supplied.
4. The row-reading path for Track A has no production wiring on this branch, and whether the alternative workflow
   is deployed is not recorded here (step 4).
5. Registry of record, read-only access statement and a schema capture (rows 7 to 9) are not recorded for this branch.
6. For B: this branch has no production route, schedule or table creation, and only interfaces and fakes for the
   identity system, approval authority, capture authority, collector, schema probe and operator screen. For C: no
   provider is listed in code and no sender or status adapter is implemented.

## Unknown or unimplemented, stated once

All real collector, identity, approval, provider and host wiring; credential status. These bounds in the code are
placeholders awaiting an owner: 24 hours schema age, 7 days approval validity, 5 minutes skew, 60 seconds send and
reconcile limits, 300 seconds request window. **The one-hour wait after the last scan is not a placeholder.** The
source can change between probe, read, claim and send; a host clock fast, or slow within the skew, is undetected;
revocation at an authority is non-atomic with the read or send.

## Where the detail lives

- `docs/LAST_SCAN_ACTIVATION_CHECKLIST.md`: order of activation and every gate, for Track B and C.
- `docs/LAST_SCAN_SCHEMA_CAPTURE_RUNBOOK.md`: the by-hand schema capture (row 8).
- `docs/LAST_SCAN_SOURCE_MAPPING_READINESS.md`: what the offline mapping check blocks on (rows 1, 2, 7).
- `docs/LAST_SCAN_REHEARSAL.md`: synthetic rehearsal, gap matrix, authority and provider contracts, inventory.
- `docs/LAST_SCAN_DRAFT_PIPELINE.md`: draft rules and timing as built.
- `docs/SOURCE_FIRST_DAILY_PILOT.md`: the administrator review workflow named in Track A step 4.
