'use strict';
(() => {
  const button = document.getElementById('run-test');
  const status = document.getElementById('test-status');
  const result = document.getElementById('test-result');
  const key = 'ask-dex-fixed-synthetic-attempt-v2';
  const removal = document.getElementById('verify-removal');
  const removalStatus = document.getElementById('removal-status');
  let attempted = false, finished = false, removalAttempted = false;
  try { const saved = sessionStorage.getItem(key); attempted = saved !== null; finished = saved === 'finished'; removalAttempted = sessionStorage.getItem(key + '-removal') !== null; }
  catch (_) { status.textContent = 'This browser cannot safely track the one-shot test. No request was sent.'; return; }
  button.disabled = attempted;
  removal.disabled = !finished || removalAttempted;
  status.textContent = attempted ? 'An attempt was already started in this browser session. Do not retry; check its recorded result.' : 'Ready. Nothing is sent until you choose the button.';
  button.addEventListener('click', async () => {
    if (attempted) return;
    attempted = true;
    button.disabled = true;
    button.textContent = 'Attempt started';
    try { sessionStorage.setItem(key, 'attempted'); }
    catch (_) { status.textContent = 'The one-shot attempt could not be recorded. No request was sent.'; return; }
    status.textContent = 'Waiting for Claude. Do not reload or retry.';
    try {
      const response = await fetch('/api/admin/ask-dex-connection-test', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: {'Content-Type': 'application/json', 'X-Review-Token': 'same-origin'},
        body: JSON.stringify({test: 'fixed-synthetic-ask-dex-v2'})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error.slice(0, 400) : 'The server refused the test.');
      if (data.synthetic_only !== true || data.read_only !== true || data.persisted !== false || typeof data.reply !== 'string') throw new Error('The server returned an unexpected result.');
      const lines = ['Provider: ' + String(data.provider || 'Unknown').slice(0, 80), 'Model: ' + String(data.model || 'Unknown').slice(0, 120), 'Answer: ' + data.reply.slice(0, 600)];
      for (const field of ['input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens']) {
        const value = data.usage?.[field];
        lines.push(field + ': ' + (Number.isSafeInteger(value) && value >= 0 ? value : 'not returned'));
      }
      result.textContent = lines.join('\n');
      status.textContent = 'Synthetic test completed. No report was changed. Do not repeat this test.';
    } catch (error) {
      status.textContent = 'The attempt did not return a usable result and may have reached the provider. Do not retry.';
      result.textContent = String(error?.message || 'Connection unavailable.').slice(0, 400);
    } finally {
      finished = true;
      try { sessionStorage.setItem(key, 'finished'); } catch (_) { /* No provider retry is enabled. */ }
      removal.disabled = removalAttempted;
    }
  });
  removal.addEventListener('click', async () => {
    if (!finished || removalAttempted) return;
    removalAttempted = true; removal.disabled = true;
    try { sessionStorage.setItem(key + '-removal', 'attempted'); }
    catch (_) { removalStatus.textContent = 'The removal check could not be recorded. No request was sent.'; return; }
    removalStatus.textContent = 'Checking removal once. This cannot invoke the provider.';
    try {
      const response = await fetch('/api/admin/ask-dex-connection-test', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: {'Content-Type': 'application/json', 'X-Review-Token': 'same-origin'}, body: '{}'
      });
      removalStatus.textContent = response.status === 404 ? 'Verified: the temporary test route is absent (404).'
        : response.status === 400 ? 'Not yet removed: this edge still has the temporary route (400). No provider request was made.'
        : response.status === 401 ? 'Sign-in expired (401). Removal is not verified.'
        : 'Removal is not verified. HTTP status: ' + response.status + '. No retry was made.';
    } catch (_) { removalStatus.textContent = 'The removal check did not complete. No retry was made; no provider request can be triggered by this check.'; }
  });
})();
