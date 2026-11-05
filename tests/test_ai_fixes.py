import io, json, os, sys, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(__file__))
from eos_fixture import build, ROWS
from hosting.spreadsheet_inbox import SpreadsheetInbox, make_ai_fixes
from hosting.fixes import apply_fixes, _sources, _cell_value

NAME = '2230-OWENS-BASCO - EOS - 10.07.26.xlsx'


class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def opener_for(fixes, skipped=()):
    body = {'model': 'test-model', 'content': [{'type': 'tool_use', 'name': 'propose_cell_fixes', 'input': {'fixes': fixes, 'skipped': list(skipped)}}]}
    return lambda request, timeout=None: FakeResponse(json.dumps(body).encode())


class AIFixTests(unittest.TestCase):
    def setUp(self):
        rows = [list(r) for r in ROWS]
        rows[1][0] = '8O10808540'
        self.raw = build([tuple(r) for r in rows])

    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'})
    def test_valid_fixes_kept_bad_ones_dropped_and_applied(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = SpreadsheetInbox(__import__('pathlib').Path(d))
            item = inbox.ingest(NAME, self.raw)
            fixes = [
                dict(sheet='1st Shift', cell='E17', before='8O10808540', after='8010808540', reason='letter O', confidence='high'),
                dict(sheet='1st Shift', cell='E18', before='WRONG', after='x', reason='before mismatch', confidence='medium'),
                dict(sheet='No Sheet', cell='A1', before='', after='x', reason='bad sheet', confidence='medium'),
                dict(sheet='1st Shift', cell='E17', before='8O10808540', after='dup', reason='same cell twice', confidence='medium'),
            ]
            record = inbox.ai_fixes(item['file_hash'], opener=opener_for(fixes, [dict(issue='Reject rate high', reason='needs a person')]))
            self.assertEqual(record['status'], 'done')
            self.assertEqual([f['cell'] for f in record['fixes']], ['E17'])
            self.assertEqual(record['dropped'], 3)
            self.assertEqual(record['skipped'][0]['issue'], 'Reject rate high')
            props = inbox.fix_proposals(item['file_hash'])
            self.assertEqual(props['ai']['count'], 1)
            # the rule-based typo fix already covers E17, so the AI one is not listed twice
            self.assertEqual(sum(1 for p in props['proposals'] if p['cell'] == 'E17'), 1)
            fixed = apply_fixes(NAME, self.raw, [dict(record['fixes'][0], kind='auto')])
            self.assertEqual(_cell_value(_sources(NAME, fixed)[0], '1st Shift', 'E17'), '8010808540')

    def test_not_configured_reports_status(self):
        with patch.dict(os.environ, {}, clear=True):
            out = make_ai_fixes({'filename': NAME, 'findings': []}, self.raw, [dict(severity='high', text='x')])
        self.assertEqual(out['status'], 'not_configured')


def chat_opener(reply, changes, undo=()):
    body = {'model': 'test-model', 'content': [{'type': 'tool_use', 'name': 'respond_to_reviewer', 'input': {'reply': reply, 'changes': changes, 'undo': list(undo)}}]}
    return lambda request, timeout=None: FakeResponse(json.dumps(body).encode())


class ChatTests(unittest.TestCase):
    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'})
    def test_chat_turn_validates_changes(self):
        from hosting.spreadsheet_inbox import chat_turn
        raw = build(ROWS)
        item = {'filename': NAME, 'findings': [], 'sheets': [], 'totals': {}}
        out = chat_turn(item, raw, [], [{'role': 'user', 'content': 'set area'}], [],
                        opener=chat_opener('Done.', [dict(sheet='1st Shift', cell='C16', before='', after='VPC', reason='blank'),
                                                     dict(sheet='1st Shift', cell='K17', before='27', after='99', reason='formula')],
                                           [dict(sheet='1st Shift', cell='E17')]))
        self.assertEqual(out['status'], 'done')
        self.assertEqual(out['reply'], 'Done.')
        self.assertEqual([c['cell'] for c in out['changes']], ['C16'])
        self.assertEqual(out['dropped'][0]['why'], 'formula cell')
        self.assertEqual(out['undo'], [dict(sheet='1st Shift', cell='E17')])

    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'})
    def test_plain_text_answer_is_accepted(self):
        from hosting.spreadsheet_inbox import chat_turn
        body = {'model': 'm', 'content': [{'type': 'text', 'text': 'It is on hold because of the totals.'}]}
        out = chat_turn({'filename': NAME, 'findings': []}, build(ROWS), [], [{'role': 'user', 'content': 'why hold?'}], [],
                        opener=lambda r, timeout=None: FakeResponse(json.dumps(body).encode()))
        self.assertEqual(out['reply'], 'It is on hold because of the totals.')
        self.assertEqual(out['changes'], [])


class SummaryTests(unittest.TestCase):
    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'})
    def test_ids_map_back_and_every_change_is_grouped(self):
        from hosting import ai_review
        body = {'model': 'm', 'content': [{'type': 'tool_use', 'name': 'write_summary', 'input': {
            'headline': 'Two things to fix.', 'next_steps': ['Apply', 'Send'],
            'error_groups': [{'title': 'Typo', 'severity': 'high', 'count': 9, 'explanation': 'x', 'issue_ids': ['i1', 'i9']}],
            'change_groups': [{'title': 'Fix typo', 'explanation': 'y', 'risk': 'safe', 'change_ids': ['c1', 'c1', 'c7']}]}}]}
        out = ai_review.summarize_fixes({'filename': NAME}, [{'key': 'k1', 'text': 'a'}], [{'id': 'f1'}, {'id': 'f2'}], {},
                                        opener=lambda r, timeout=None: FakeResponse(json.dumps(body).encode()))
        self.assertEqual(out['error_groups'][0]['issue_keys'], ['k1'])
        self.assertEqual(out['error_groups'][0]['count'], 1)
        self.assertEqual(out['change_groups'][0]['fix_ids'], ['f1'])
        self.assertEqual(out['change_groups'][1]['fix_ids'], ['f2'])  # leftovers land in "Other changes"
