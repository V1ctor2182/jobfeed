"""Gate for applier_helpers: the registry is consistent and every helper passes its own fixture page.

The browser defaults to snap Chromium. Set APPLIER_HELPER_BROWSER to another Chromium binary if needed.
With APPLIER_HELPER_GATE=1, a missing browser fails instead of skipping.
"""
import os
from pathlib import Path
import shutil
import unittest

from pipeline.applier_helpers import HELPERS, for_site, problems, registry, script

BROWSER = os.environ.get('APPLIER_HELPER_BROWSER', '/snap/bin/chromium')
GATE = os.environ.get('APPLIER_HELPER_GATE') == '1'


class RegistryTests(unittest.TestCase):
    def test_registry_is_consistent(self):
        self.assertEqual(problems(), [])

    def test_helpers_are_offered_by_site(self):
        names = {h['name'] for h in for_site('acme.wd1.myworkdayjobs.com')}
        self.assertTrue(all(any(s == '*' or 'myworkdayjobs.com'.endswith(s.lstrip('.')) for s in e['sites']) for e in registry() if e['name'] in names))

    def test_script_rejects_unknown_helpers(self):
        with self.assertRaisesRegex(ValueError, 'unknown helper'):
            script('../../etc/passwd', {})


@unittest.skipUnless(GATE or shutil.which(BROWSER) or Path(BROWSER).exists(), f'browser {BROWSER} not found')
class HelperFixtureTests(unittest.TestCase):
    def test_every_helper_passes_its_fixture(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=BROWSER, args=['--headless=new'])
            try:
                self._check_all(browser)
            finally:
                browser.close()

    def _check_all(self, browser):
        for entry in registry():
            with self.subTest(helper=entry['name']):
                page = browser.new_page()
                page.goto((HELPERS / entry['fixture']).resolve().as_uri())
                result = page.evaluate(script(entry['name'], entry['test']['args']))
                state = page.evaluate('window.readState()')
                page.close()
                self.assertTrue(isinstance(result, dict) and result.get('ok'), f'helper reported {result}')
                self.assertEqual({k: state.get(k) for k in entry['test']['expect']}, entry['test']['expect'])


if __name__ == '__main__':
    unittest.main()
