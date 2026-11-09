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

    def test_picked_all_and_automatic_ai_changes_fail_before_model_calls(self):
        before=self.issues(self.h)
        for fields in ({'keys':[before[0]['key']]},{},{'keys':[]},{'auto':True}):
            with self.subTest(fields=fields), patch.object(ai_review,'_post',side_effect=AssertionError('No model calls')) as model:
                st,out=self.call('/api/spreadsheets/ai-fix',dict(file_hash=self.h,**fields))
                self.assertEqual(st,409,out)
                self.assertIn('disabled',out['error'])
                model.assert_not_called()
                self.assertEqual(self.issues(self.h),before)

    def test_opening_legacy_report_never_automatically_rewrites_it(self):
        os.environ.pop('PPG_AUTOFORMAT',None)
        before=self.issues(self.h)
        st,out=self.call('/api/spreadsheets/auto-clean',{'file_hash':self.h})
        self.assertEqual(st,200,out)
        self.assertFalse(out['changed'])
        self.assertEqual(self.issues(self.h),before)

    def test_legacy_fine_as_is_cannot_bypass_fingerprint_review(self):
        before=self.issues(self.h)
        issue=before[0]
        st,out=self.call('/api/spreadsheets/mark-fine',{'file_hash':self.h,'by':'Reviewer','issues':[issue]})
        self.assertEqual(st,409,out)
        self.assertEqual(self.issues(self.h),before)


if __name__ == '__main__':
    unittest.main()
