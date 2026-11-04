PPG CLIENT INTELLIGENCE VERIFIER — OCTOBER 23 SOURCE HANDOFF

This is the October 22 revised interface, packaged October 23, 2026 for review.
It is a local draft, not a hosted or collaborative production service.

RUN
1. Extract the entire ZIP into a writable folder on your computer.
2. Install/use Python 3.9 or newer and a modern desktop browser.
3. Open a terminal in the extracted CI_Verifier_Source_Oct23 folder.
4. Run: python3 app/server.py
   On Windows, use: py -3 app/server.py
5. Open http://127.0.0.1:8765/?mode=synthetic in that computer's browser.
   Do not open index.html directly: the Python server is required.
6. Stop with Ctrl-C. Run again to recover that local session.
If port 8765 is occupied: python3 app/server.py --port 8772
Then open http://127.0.0.1:8772/?mode=synthetic.
For a new session: python3 app/server.py --data outputs/new-session --port 8772
No Python packages, API keys or external integrations are required.

The server binds only to loopback. A localhost URL sent to another person does
not connect them to your computer. Sharing this ZIP gives source access, not
shared execution. Ron and Lauren each get separate local data and approvals.
Files created under outputs/ stay on the computer running this copy.

LATEST INTERFACE
- Actual revised PPG Client Intelligence logo at top left; original proportions.
- Fixed right sidebar: Your Decision above one Review Finding at a time.
- Ignore, Accept and Next; Accept shows a checkmark and Done status.
- Accept acknowledges only. It never corrects a cell or grants version approval.
- Finding marks live in browser session storage, bound to mode, source hash,
  revision and working hash. They are not shared, authenticated or server-audited.
- Spreadsheet/document scrolling is independent of the review panel. A small
  window may require panel or horizontal scrolling; desktop is the meeting target.
- Demonstration notice is retained.

DATA BOUNDARY
This handoff contains only an invented October 12 synthetic fixture. It is not
the historical Briggs trial and cannot substantiate historical-data validation.
No client XLSX, historical fixture JSON, screenshot, saved runtime database,
approved report, current-week record, credential or private helper is included.
Original local project and historical source bytes remain unchanged on Ron's Mac.
Historical import/extraction branches remain in the source for technical context,
but historical mode is not registered by this portable server and no input files
are included. The historical link is removed in this copy. Source code mentions
historical field semantics; these are not data supplied for use in this handoff.
The earlier historical trial was first-shift-only and demonstration-only; it
preserved duplicate IDs and flagged ambiguity between rework and acceptance.
No historical quantities should be treated as approved business results.

REVIEW FLOW
Inspect source/cell provenance and judgment flags. Edit a cell, then tab away to
save. Revalidate, select a simulated reviewer and check the review box before
approving the exact version. Any saved change invalidates approval. Source
snapshots and working/approved CSV artifacts remain separate. Download is marked
UNAPPROVED and does not publish. Report/request/timeout buttons are simulations.
Silence or unresolved review leaves the report unpublished. Do not edit SQLite
or overwrite original snapshots to repair corruption; preserve the failed session
and start a separate new one for diagnosis.

NOT IMPLEMENTED
Shared hosting or shared state; enforced identities/RBAC; durable server audit of
finding acknowledgments; production object-lock; full XLSX editing/export;
arbitrary imports; actual AI analysis; voice editing; live notification delivery
or automated reminders; weekly/monthly reports; production approval; publishing;
computer-off cloud service. Naming, reviewer assignments, channel, timeouts and
escalation rules remain proposals. A simulated role selector is not authentication.

VERIFICATION
Fresh October 23: 33 unit tests passed, 10 HTTP checks passed, 8 static UI source
checks passed, JavaScript syntax check passed. Zero failures in completed checks.
See verification/ for exact output. Tests use temporary/isolated synthetic state.
Earlier October 22: browser rendering and Ignore/Accept/Next, repeated Accept,
Done status and fixed sidebar during document scroll were verified on the local
prototype. Those browser checks were NOT rerun on this ZIP on October 23.
Historical tests and two-user/cloud/mobile/Windows execution NOT RUN for this
synthetic-only handoff. Do not transfer earlier historical test claims to it.

Run included unit tests: python3 -m unittest discover -s tests -v
Optional JS syntax check (Node needed only for this check): node --check app/ui.js
HTTP tests mutate an isolated fresh session: start the server with a new --data
directory and unused port, create outputs/ if needed, then run
python3 tests/http_smoke.py http://127.0.0.1:YOUR_PORT
Do not point test scripts at a session you are reviewing.

PACKAGING
Source was copied from the current working project, not the older Library ZIP v2.
Only server mode registration and initial historical-link/banner text were adapted
in the staged copy for synthetic-only operation. UI logic/styles and logo are the
latest source. SHA256SUMS.txt covers every payload file other than itself.
Uploading this ZIP to Library does not prove Lauren can access it: the parent task
must place it in the shared CI destination and verify inherited file access before
notifying her. No hosting, permission change or notification was performed here.
