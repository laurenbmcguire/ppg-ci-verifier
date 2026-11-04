"""Static layout safeguards, NOT browser/device rendering or visual QA."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

class DevelopmentLayoutStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = (ROOT / 'web/style.css').read_text()
        cls.html = (ROOT / 'web/index.html').read_text()

    def test_mobile_viewport_declared(self):
        self.assertRegex(self.html, r'<meta[^>]*name="viewport"[^>]*width=device-width')

    def test_mobile_breakpoint_and_minimum_grid_tracks(self):
        self.assertIn('@media(max-width:760px)', self.css)
        self.assertIn('minmax(0,1fr)', self.css)
        self.assertRegex(self.css, r'@media\(max-width:760px\).*?main\{[^}]*min-width:0')

    def test_long_workflow_hashes_and_input_width_guarded(self):
        self.assertRegex(self.css, r'#workflow-detail,#workflow-list\{[^}]*min-width:0[^}]*overflow-wrap:anywhere')
        self.assertRegex(self.css, r'#workflow-detail input\[type="text"\]\{[^}]*max-width:100%')
        self.assertRegex(self.css, r'#workflow-detail pre\{[^}]*max-width:100%[^}]*overflow-wrap:anywhere')

    def test_upload_inputs_and_tables_have_overflow_containment(self):
        self.assertRegex(self.css, r'#spreadsheet-detail input\[type=file\]\{[^}]*max-width:100%')
        self.assertRegex(self.css, r'\.table-wrap\{[^}]*overflow:auto')

    def test_demo_buttons_wrap_and_have_touch_height(self):
        self.assertRegex(self.css, r'#all-report-workflow button\{[^}]*min-height:44px[^}]*white-space:normal')
        self.assertRegex(self.css, r'@media\(max-width:760px\)\{#workflow-detail \.report-finding button\{[^}]*width:100%')

    def test_local_scripts_exist(self):
        scripts = re.findall(r'<script src="/([^"?]+)"', self.html)
        self.assertIn('workflow-ui.js', scripts)
        for script in scripts:
            self.assertTrue((ROOT / 'web' / script).is_file(), script)

if __name__ == '__main__':
    unittest.main(verbosity=2)
