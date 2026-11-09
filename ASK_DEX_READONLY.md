# Ask Dex report questions

Ask Dex answers questions about the displayed saved report using the existing Anthropic configuration. It identifies Claude/Anthropic and the relevant evidence shared before Send. It never asks for credentials in chat.

`/api/spreadsheets/chat` uses `hosting/report_chat.py`, not the legacy edit chat. It checks access and the exact displayed file/check before reading evidence, binds ongoing history to source/review context, and rechecks authorization and evidence after the provider. No answer is persisted, applied or treated as approval. Edit/undo fields cannot affect corrections.

Context contains a small report summary, relevant findings, referenced/finding cells and same-column headers, not full worksheets. Unknown values remain unknown. Text is rendered as text, not HTML. Model tools are absent. Errors are sanitized; originals and decisions remain unchanged. Conversations reset on report/context changes and sign-in. Unsent drafts remain isolated in memory when navigating away and back.

See `ASK_DEX_PRIVACY.md` for parsing opt-in, minimized payload limits, historical source evidence compatibility and the bounded call-site audit.

Local tests use fabricated data, fake providers and isolated storage. Headless Chromium covers desktop and phone disclosure, Send/answer, malicious-looking output, failure/retry, navigation, Back and unchanged reports. These tests do not establish real provider connectivity or production deployment. The release integrator reruns the combined aggregate, including disposable PostgreSQL, and separately controls the approved fixed-synthetic connectivity test.
