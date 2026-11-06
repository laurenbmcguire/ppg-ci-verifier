"""Fix with AI: picked issues (or all) are all resolved — cells changed, rows taken out or moved, or confirmed fine."""
import base64, json, os, re, sys, unittest, urllib.request
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import test_autopilot
from eos_fixture import build, ROWS
from hosting import ai_review


def fake_post(body, opener=None, timeout=None):
    prompt = body['messages'][0]['content']
    issues = json.loads(re.search(r'<issues_to_resolve>\n(.*?)\n</issues_to_resolve>', prompt, re.S).group(1))
    out = dict(cell_changes=[], remove_rows=[], move_rows=[], confirmed_fine=[])
    for i in issues:
        if i.get('code') == 'duplicate_row':
            out['remove_rows'].append(dict(ref=i['ref'], sheet=i['sheet'], row=i['row'], reason='extra copy', confidence='sure'))
        elif '8O139189' in i.get('text', '') and i.get('row'):
            if not any(c['cell'] == f"D{i['row']}" for c in out['cell_changes']):
                out['cell_changes'].append(dict(ref=i['ref'], sheet=i['sheet'], cell=f"D{i['row']}", before='8O139189', after='80139189', reason='letter O for zero', confidence='sure'))
            else:
                out['confirmed_fine'].append(dict(ref=i['ref'], reason='same cell as the typo fix'))
        else:
            out['confirmed_fine'].append(dict(ref=i['ref'], reason='checked against the other rows; the value is right'))
    return {'model': 'fake', 'content': [{'type': 'tool_use', 'name': 'resolve_issues', 'input': out}]}


class AIFix(test_autopilot.Autopilot):
    test_only_sure_fixes_and_never_sends = test_remove_duplicate_rows = test_paused_by_default = None   # reuse the server setup only

    def setUp(self):
        super().setUp()
        os.environ['PPG_AUTOFORMAT'] = '0'; self.addCleanup(os.environ.pop, 'PPG_AUTOFORMAT', None)
        rows = [tuple(r) for r in ROWS]
        rows[3] = (rows[3][0], '8O139189') + rows[3][2:]
        rows[6] = rows[2]
        st, up = self.call('/api/spreadsheets/import', {'filename': '2230-OWENS-BASCO - EOS - 2026-10-07.xlsx', 'content_base64': base64.b64encode(build(rows)).decode()})
        self.h = up['item']['file_hash']

    def issues(self, h):
        with urllib.request.urlopen(self.base + '/api/spreadsheet?id=' + h, timeout=30) as r:
            return [i for i in json.loads(r.read())['review']['issues'] if i['severity'] != 'info']

    def test_fix_picked_then_all(self):
        before = self.issues(self.h)
        typo = [i['key'] for i in before if '8O139189' in i['text']]
        self.assertTrue(typo)
        with patch.object(ai_review, '_post', fake_post):
            st, out = self.call('/api/spreadsheets/ai-fix', {'file_hash': self.h, 'keys': typo})
            self.assertEqual(st, 200, out)
            s = out['ai_fixed']
            self.assertEqual([(c['before'], c['after']) for c in s['changed']], [('8O139189', '80139189')])
            self.assertEqual(s['left'], [])
            h2 = out['item']['file_hash']; self.assertNotEqual(h2, self.h)
            self.assertFalse([i for i in self.issues(h2) if '8O139189' in i['text']])
            st, out = self.call('/api/spreadsheets/ai-fix', {'file_hash': h2})            # everything that is left
            self.assertEqual(st, 200, out)
            s = out['ai_fixed']
            self.assertTrue(s['removed']); self.assertEqual(s['left'], [])
            self.assertEqual(self.issues(out['item']['file_hash']), [])                   # nothing left: fixed or confirmed fine

    def test_automatic_batches_keep_one_record(self):
        keys = [i['key'] for i in self.issues(self.h)]
        self.assertGreater(len(keys), 1)
        with patch.object(ai_review, '_post', fake_post):
            st, out = self.call('/api/spreadsheets/ai-fix', {'file_hash': self.h, 'keys': keys[:1], 'auto': True})
            self.assertEqual(st, 200, out); first = out['ai_fixed']; self.assertTrue(first['auto'])
            h2 = out['item']['file_hash']
            rest = [i['key'] for i in self.issues(h2)]
            st, out = self.call('/api/spreadsheets/ai-fix', {'file_hash': h2, 'keys': rest, 'auto': True})
            self.assertEqual(st, 200, out); s = out['ai_fixed']
            self.assertTrue(s['auto']); self.assertEqual(s['rounds'], 2)
            self.assertEqual(s['asked'], 1 + len(rest)); self.assertEqual(s['from_hash'], self.h)
            self.assertEqual(s['changed'][:len(first['changed'])], first['changed'])   # round one is still listed
            self.assertEqual(self.issues(out['item']['file_hash']), [])

    def test_nothing_picked_and_ai_off(self):
        st, out = self.call('/api/spreadsheets/ai-fix', {'file_hash': self.h, 'keys': []})
        self.assertEqual(st, 409); self.assertIn('Pick', out['error'])
        st, out = self.call('/api/spreadsheets/ai-fix', {'file_hash': self.h})
        self.assertEqual(st, 409)                                                        # no AI key in tests: a clear message, nothing changed


if __name__ == '__main__':
    unittest.main()
