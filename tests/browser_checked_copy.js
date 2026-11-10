// Standalone synthetic-only full production app. No database, provider, or user profile.
// Run from repository root with PLAYWRIGHT_MODULE and CHROMIUM_EXECUTABLE set.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const ROOT = path.resolve(__dirname, '..');
const EVIDENCE = path.resolve(ROOT, '..', 'checked-copy-evidence');
fs.mkdirSync(EVIDENCE, {recursive: true});
const HASH = 'a'.repeat(64), CHECK = 'b'.repeat(64), ORIGINAL = 'c'.repeat(64);
const MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
// Valid minimal synthetic XLSX ZIP (one inline-string cell), built offline with fixed timestamps.
const BYTES = Buffer.from('UEsDBBQAAAAIAAAAIVwcdPw42AAAAPYBAAATAAAAW0NvbnRlbnRfVHlwZXNdLnhtbK2RvU4DMQzHX+WUtWpcGDqgXhfKCgy8gJvz9aLLl2K3XN++uVAYUIGFyUr+Hz9L3rydE3EzeRe4VYNIegBgM5BH1jFRKEofs0cpz3yAhGbEA8H9arUGE4NQkKXMHWq72VGPRyfN01S+2cbQqkyOVfP4YZxZrcKUnDUoRYdT6L5RlleCLsnq4cEmXhSDgpuEWfkZcM29nChn21Hzilme0RcXTA7eYx73MY7695IbW8a+t4a6aI6+RDSnTNjxQCTe6Tq1RxsWf/OrmaGOu39e5Kv/cw+o595eAFBLAwQUAAAACAAAACFc/luGcooAAADwAAAACwAAAF9yZWxzLy5yZWxzjc8xDsIwDAXQq1Q+QF0YGFDaiaUr4gImddqqTRw5QZTbk7EgBkbrf70vmyuvlGcJaZpjqja/htTClHM8IyY7sadUS+RQEifqKZdTR4xkFxoZj01zQt0b0Jm9WfVDC9oPB6hur8j/2OLcbPki9uE55B8TX40ik46cW9hWfIoud5GlLihgZ/Djwe4NUEsDBBQAAAAIAAAAIVwRk2uRlQAAAOcAAAAPAAAAeGwvd29ya2Jvb2sueG1sjY9NDoJADIWvMukBLLhwQYCVG9aeYByKM4H5SVuj3l4CsnfVvvfS76XtK/N8z3k277gk6cCrlgZRnKdo5ZQLpTWZMkerq+QHSmGyo3gijQueq+qC0YYEO6Hhfxh5moKja3bPSEl3CNNiNeQkPhSBvt0a5DdNspE6uH2SetLgwGz2MHZQg+EmrAsPYw3Yt3hc4vFc/wVQSwMEFAAAAAgAAAAhXLZUqs+OAAAA8QAAABoAAAB4bC9fcmVscy93b3JrYm9vay54bWwucmVsc43PPQ7CMAwF4KtUOUDdMjCgJhNLV8QFotRtojY/so2A2xMxoCIxMFl+lr4nDxfcrISc2IfCzSNuibXyIuUEwM5jtNzmgqle5kzRSl1pgWLdaheEQ9cdgfaGMsPebMZJKxqnXjXXZ8F/7DzPweE5u1vEJD8q4J5pZY8oFbW0oGj1iRjeo2+rqsAM8PWheQFQSwMEFAAAAAgAAAAhXPgVesiSAAAAwwAAABgAAAB4bC93b3Jrc2hlZXRzL3NoZWV0MS54bWxNjs0NwzAIhVexPEBJeuihsi1V6gaZwHJobSX+EUZKs31JDlUPIPjgPTBbpaVHRFafvJZudWRud4AeImbfL7VhkcmrUvYsLb2hN0I/n6K8wnUYbpB9KtqZkz09e2eoboqsHoWGo3iMWrHVqayp4MQkPHVn2E174YicgpKLYcFZhdp2A+wMHBsQJMRN8p89/P52X1BLAQIUAxQAAAAIAAAAIVwcdPw42AAAAPYBAAATAAAAAAAAAAAAAACAAQAAAABbQ29udGVudF9UeXBlc10ueG1sUEsBAhQDFAAAAAgAAAAhXP5bhnKKAAAA8AAAAAsAAAAAAAAAAAAAAIABCQEAAF9yZWxzLy5yZWxzUEsBAhQDFAAAAAgAAAAhXBGTa5GVAAAA5wAAAA8AAAAAAAAAAAAAAIABvAEAAHhsL3dvcmtib29rLnhtbFBLAQIUAxQAAAAIAAAAIVy2VKrPjgAAAPEAAAAaAAAAAAAAAAAAAACAAX4CAAB4bC9fcmVscy93b3JrYm9vay54bWwucmVsc1BLAQIUAxQAAAAIAAAAIVz4FXrIkgAAAMMAAAAYAAAAAAAAAAAAAACAAUQDAAB4bC93b3Jrc2hlZXRzL3NoZWV0MS54bWxQSwUGAAAAAAUABQBFAQAADAQAAAAA', 'base64');
const ORIGINAL_BYTES = Buffer.from('synthetic immutable original');
const NAME = 'synthetic-checked-v2.xlsx';
let mode = 'ready', rejectDownload = false, requests = [], unexpected = [];
function report() {
  const checked = {ready: true, status: 'checked', file_hash: HASH, check_hash: CHECK,
    expires_at: new Date(Date.now() + (mode === 'expiry' ? 2500 : 120000)).toISOString(),
    remaining_detected_errors: 0, set_aside_unresolved: 0, informational: 0, coverage_complete: true,
    download: `/api/spreadsheets/download?id=${HASH}&checked=1&check=${CHECK}`, reasons: []};
  if (mode === 'mismatch') checked.check_hash = 'd'.repeat(64);
  if (mode === 'not-ready') Object.assign(checked, {ready: false, status: 'needs_review', remaining_detected_errors: 1, reasons: ['Synthetic finding requires review']});
  return {file_hash: HASH, check_hash: CHECK, filename: 'synthetic-current-v2.xlsx', revision: 2,
    checker_version: 'synthetic-checker', assignment: {status: 'matched', client: 'Synthetic client', client_key: 'synthetic'},
    source_reconciliation: {bound_to: {file_hash: HASH, checker_version: 'synthetic-checker'}, snapshot_id: 'synthetic-snapshot', site_id: 'synthetic-site', list_id: 'synthetic-list', coverage: {complete: true}, verified: true, count_reconciliation: {status: 'verified', record_counts: {matched_unique_items: 1, report_rows: 1}}},
    root_hash: ORIGINAL, received_at: new Date().toISOString(), checked_copy: checked,
    fixed_from: {file_hash: ORIGINAL, revision: 1, changes: [{sheet: 'Synthetic', cell: 'A2', before: 'Before', after: 'After'}]},
    sheets: [{name: 'Synthetic', tables: []}], holds: [], sent: [],
    review: {status: 'clear', job: 'SYNTHETIC', day: '2026-11-10', issues: [], dismissed: [], counts: {high: 0, medium: 0, low: 0},
      page: {complete: true, size: 100, issues_total: 0, dismissed_total: 0, issues_returned: 0, dismissed_returned: 0, binding: {file_hash: HASH, check_hash: CHECK, run: {check_hash: CHECK}, rev: 0, issues_total: 0, dismissed_total: 0}}, coverage: {truncated: false, not_listed: 0},
      decisions: {rev: 0}, run: {check_hash: CHECK}, sharepoint: {status: 'match', source_verified: true}}};
}
const json = (res, data, status = 200) => {res.writeHead(status, {'Content-Type': 'application/json'}); res.end(JSON.stringify(data));};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const entry = {method: req.method, path: url.pathname, query: url.search}; requests.push(entry);
  if (req.method !== 'GET' && !['/api/clients', '/api/spreadsheets/fixes', '/api/spreadsheets/sheet'].includes(url.pathname)) {
    unexpected.push(entry); return json(res, {error: 'Unexpected synthetic action'}, 409);
  }
  const assets = {'/app': ['web/app.html', 'text/html'], '/app.js': ['web/app.js', 'text/javascript'], '/app.css': ['web/app.css', 'text/css'], '/assets/ppg-client-intelligence.png': ['web/assets/ppg-client-intelligence.png', 'image/png']};
  if (/^\/assets\/fonts\/dm-sans-latin-(400|600|700)-normal\.woff2$/.test(url.pathname)) {res.writeHead(200, {'Content-Type': 'font/woff2'}); return res.end(fs.readFileSync(path.join(ROOT, 'web', url.pathname)));}
  if (assets[url.pathname]) {const [file, type] = assets[url.pathname]; res.writeHead(200, {'Content-Type': type}); return res.end(fs.readFileSync(path.join(ROOT, file)));}
  if (url.pathname === '/api/health') return json(res, {storage: 'synthetic', ai_review_configured: false});
  if (url.pathname === '/api/me') return json(res, {user: {name: 'Synthetic reviewer', role: 'reviewer'}});
  if (url.pathname === '/api/clients') return json(res, {clients: []});
  if (url.pathname === '/api/spreadsheets') return json(res, {spreadsheets: [report()], working_copies: true, count: 1});
  if (url.pathname === '/api/spreadsheets/working-copy') return json(res, {status: 'current', requested_hash: HASH, working_hash: HASH, history: [{file_hash: ORIGINAL, filename: 'synthetic-original.xlsx'}]});
  if (url.pathname === '/api/spreadsheet') return json(res, report());
  if (url.pathname === '/api/spreadsheets/fixes') return json(res, {proposals: [], ai: {status: 'not_run'}});
  if (url.pathname === '/api/spreadsheets/sheet') return json(res, {sheet: 'Synthetic', width: 2, height: 12, rows: [['Label', 'Count'], ['After', 3], ...Array.from({length: 10}, () => ['', ''])]});
  if (url.pathname === '/api/spreadsheets/download') {
    if (url.searchParams.get('checked') === '1') {
      assert.equal(url.searchParams.get('id'), HASH); assert.equal(url.searchParams.get('check'), CHECK);
      if (rejectDownload) return json(res, {error: 'Synthetic check expired. Re-check this copy before downloading.'}, 409);
      res.writeHead(200, {'Content-Type': MIME, 'Content-Disposition': `attachment; filename="${NAME}"`, ...(mode==='old-server'?{}:{'X-PPG-Checked-Copy': mode==='malformed-receipt'?'{bad':JSON.stringify({protocol:'ppg-checked-copy-v1',ready:true,file_hash:HASH,check_hash:mode==='wrong-receipt'?'d'.repeat(64):CHECK,expires_at:report().checked_copy.expires_at})}), 'Cache-Control':'private, no-store'}); return res.end(BYTES);
    }
    res.writeHead(200, {'Content-Type': MIME, 'Content-Disposition': 'attachment; filename="synthetic-original.xlsx"'}); return res.end(ORIGINAL_BYTES);
  }
  unexpected.push(entry); json(res, {error: 'Unexpected fixture route'}, 404);
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE});
  try {
    for (const width of [1200, 390]) {
      const context = await browser.newContext({viewport: {width, height: 900}, acceptDownloads: true});
      const page = await context.newPage(), errors = [], external = [], downloads = [];
      page.on('pageerror', error => errors.push(String(error)));
      page.on('download', download => downloads.push(download));
      await context.route('**/*', route => {if (route.request().url().startsWith(base + '/')) return route.continue(); external.push(route.request().url()); return route.abort();});
      const button = () => page.getByRole('button', {name: 'Download checked copy', exact: true});
      let navigation = 0;
      const open = async next => {mode = next; rejectDownload = false; await page.goto(base + '/app?fixture=' + (++navigation) + '#r=' + HASH, {waitUntil: 'networkidle'}); await page.locator('.fd-saved-summary').waitFor({timeout: 6000}).catch(async error => {console.error(JSON.stringify({errors, requests, body: await page.locator('body').innerText()})); throw error;}); if (width < 600) await page.locator('.fd-mobile-panel').click();};
      await open('ready'); await button().waitFor();
      assert.match(await page.locator('#fd-checked-copy').innerText(), /0 remaining detected errors/);
      assert.match(await page.locator('.fd-saved-summary').innerText(), /Original preserved/);
      const ordinary = await page.locator(`a[href="/api/spreadsheets/download?id=${HASH}"]`).count(); assert.ok(ordinary > 0);
      await page.locator('#fd-checked-copy').scrollIntoViewIfNeeded();
      await page.screenshot({path: path.join(EVIDENCE, `checked-copy-ready-${width}.png`), fullPage: true});
      const start = requests.length;
      const responseEvent = page.waitForResponse(response => response.url().includes('checked=1'));
      const event = page.waitForEvent('download'); await button().click(); const downloaded = await event;
      const downloadResponse = await responseEvent; assert.equal(downloadResponse.status(), 200);
      assert.equal(downloadResponse.headers()['content-type'], MIME);
      assert.equal(downloaded.suggestedFilename(), NAME);
      assert.deepEqual(fs.readFileSync(await downloaded.path()), BYTES);
      assert.equal(requests.slice(start).filter(r => r.method !== 'GET').length, 0);
      const originalResponse = await context.request.get(base + '/api/spreadsheets/download?id=' + ORIGINAL);
      assert.deepEqual(await originalResponse.body(), ORIGINAL_BYTES);
      rejectDownload = true;
      const beforeFailure = downloads.length; await button().click();
      await page.getByText('Synthetic check expired. Re-check this copy before downloading.', {exact: false}).first().waitFor();
      await page.waitForTimeout(100); assert.equal(downloads.length, beforeFailure, '409 JSON must not download');
      await page.screenshot({path: path.join(EVIDENCE, `checked-copy-rejected-${width}.png`), fullPage: true});
      for(const receiptMode of ['old-server','malformed-receipt','wrong-receipt']) {
        await open(receiptMode); await button().waitFor(); const prior=downloads.length, before=requests.length;
        await button().click(); await page.getByText('The server did not confirm a current checked copy', {exact:false}).first().waitFor();
        assert.equal(downloads.length,prior,receiptMode+' must not save ordinary or unconfirmed bytes');
        assert.equal(requests.slice(before).filter(r=>r.path==='/api/spreadsheets/download').length,1,'No automatic retry');
      }
      await open('not-ready'); assert.equal(await button().count(), 0, 'Fresh unready payload must replace success');
      await open('mismatch'); assert.equal(await button().count(), 0, 'Scope mismatch must not retain success');
      await open('expiry'); await button().waitFor(); const beforeExpiry = requests.length;
      await page.waitForTimeout(2800);
      assert.equal(await button().count(), 0, 'Expired claim must disappear without network');
      assert.doesNotMatch(await page.locator('body').innerText(), /0 remaining detected errors/, 'All visible checked claims must expire, including the header');
      assert.equal(requests.length, beforeExpiry, 'Expiry must not issue any network request');
      assert.doesNotMatch(await page.locator('body').innerText(), /FINAL version|No errors left to fix/);
      assert.match(await page.locator('.fd-sheetbar-what').innerText(), /Saved working copy/);
      const recorded = page.locator('.fd-comparison').first();
      await recorded.evaluate(node => node.open = true);
      assert.match(await recorded.innerText(), /Recorded comparison: Matches SharePoint/);
      assert.match(await recorded.innerText(), /not current checked-copy eligibility/);
      await page.screenshot({path: path.join(EVIDENCE, `checked-copy-expired-${width}.png`), fullPage: true});
      assert.deepEqual(errors, []); assert.deepEqual(external, []); assert.deepEqual(unexpected, []);
      console.log(JSON.stringify({passed: true, width, full_app: true, synthetic_only: true, exact_download_bytes: true, filename: NAME, content_type: MIME, failed_json_not_downloaded: true, expiry_no_network: true, stale_scope_rejected: true, post_apply_current_payload: true, ordinary_links_preserved: ordinary, non_get_requests: requests.filter(r => r.method !== 'GET'), unexpected_requests: unexpected}));
      await context.close();
    }
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
