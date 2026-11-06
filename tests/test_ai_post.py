import io, json, os, unittest
from unittest.mock import patch
from hosting import ai_review


class Opener:
    def __init__(self, replies): self.replies, self.sent = list(replies), []
    def __call__(self, request, timeout=None):
        self.sent.append(json.loads(request.data)); return io.BytesIO(json.dumps(self.replies.pop(0)).encode())


TOOL = {'name': 'resolve_issues', 'input_schema': {'type': 'object'}}
USE = {'content': [{'type': 'tool_use', 'name': 'resolve_issues', 'input': {'ok': 1}}]}


class ForcedToolIsNeverSent(unittest.TestCase):
    def setUp(self):
        p = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}); p.start(); self.addCleanup(p.stop)

    def test_forced_tool_becomes_auto_with_instruction(self):
        o = Opener([USE])
        out = ai_review._post(dict(model='m', system='S', tools=[TOOL], tool_choice={'type': 'tool', 'name': 'resolve_issues'}, messages=[{'role': 'user', 'content': 'x'}]), o)
        self.assertEqual(out, USE); self.assertEqual(len(o.sent), 1)
        self.assertEqual(o.sent[0]['tool_choice'], {'type': 'auto'}); self.assertIn('calling the resolve_issues tool', o.sent[0]['system'])

    def test_asks_again_when_the_tool_is_not_called(self):
        o = Opener([{'content': [{'type': 'text', 'text': 'thinking out loud'}]}, USE])
        out = ai_review._post(dict(model='m', system='S', tools=[TOOL], tool_choice={'type': 'any'}, messages=[{'role': 'user', 'content': 'x'}]), o)
        self.assertEqual(out, USE); self.assertEqual(len(o.sent), 2)
        self.assertEqual([m['role'] for m in o.sent[1]['messages']], ['user', 'assistant', 'user'])
        self.assertTrue(all(b['tool_choice'] == {'type': 'auto'} for b in o.sent))


if __name__ == '__main__':
    unittest.main()
