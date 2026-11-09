# Ask Dex report questions

Ask Dex answers questions about the saved, currently displayed report version. It uses the existing Anthropic configuration (`ANTHROPIC_API_KEY`, optionally `AI_CHAT_MODEL` or `AI_REVIEW_MODEL`). The pane identifies Claude/Anthropic and the extracted report context before sending. It never requests credentials in the UI.

The `/api/spreadsheets/chat` route now uses `hosting/report_chat.py`, not the legacy cell-edit chat. It checks report access and the displayed check hash before loading context, then rechecks authorization, the report, assignment and source holds after the provider returns. No chat response is persisted, applied or treated as an approval. In-memory history resets on another report/check and sign-in. Legacy edit/undo fields cannot affect corrections.

Context is limited to extracted cells, metadata and check/source/review evidence for the selected report, with explicit incomplete-evidence warnings. There are no model tools or external-link fetches. User and provider text use text nodes, not HTML. Message length, conversation length, context and answer length are bounded. Blank values remain unknown. Provider failures return sanitized errors and keep report data unchanged.

## Local verification

- 49 focused Python tests passed, including invalid input, unavailable/malformed provider responses, denied/revoked access, changed check/assignment and byte-identical originals.
- All 110 Node tests passed.
- Actual headless Chromium tests passed at 1200 and 390 pixels against isolated synthetic storage and a fake provider. Tests covered disclosure, question/answer, safe rendering of malicious-looking text, failure/retry, pending navigation, Back, phone overflow and unchanged report count/bytes/decisions. Exactly eight fake chat calls, zero external traffic. Unrelated AI-layout work was disabled only in this harness.
- No real provider invocation or deployment is established by these results. The final integration must rerun the aggregate and separately verify provider connectivity without customer data.

Reproduce from this checkout with the project test environment:

    CI_LOCAL_DEV=1 VERCEL=0 ../test-venv/bin/python -m pytest -q tests/test_report_chat.py tests/test_ai_fixes.py tests/test_ai_proposal_guards.py tests/test_request_api_guards.py tests/test_access.py -p no:cacheprovider
    node --test tests/test_*.js
    PLAYWRIGHT_MODULE=<installed playwright-core> CHROMIUM_EXECUTABLE=<installed headless Chromium> ../test-venv/bin/python tests/browser_report_chat.py
