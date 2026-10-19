import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from gp_store_verification import classify_app_page, parse_developer_page, scan_developer, verify_app
from audit_domestic_reconciliation import build_failed_gp_review, verify_failed_gp_store_pages
from monitor import extract_gp_developers, normalize_gp_developer_url
from review_failed_gp_entries import apply_verified_identities


URL = 'https://play.google.com/store/apps/details?id=test.app&hl=en&gl=us'
LIVE = ('<meta content="Test - Apps on Google Play" property="og:title">'
        '<div class="Vbfug"><a href="/store/apps/dev?id=123"><span>Current Name</span></a></div>')


class GPVerificationTests(unittest.TestCase):
    def test_display_name_does_not_rewrite_native_id(self):
        url = 'https://play.google.com/store/apps/developer?id=Original'
        self.assertIn('id=Original', normalize_gp_developer_url(url, 'New Name'))

    def test_apply_restores_only_current_verified_existing_app(self):
        from datetime import datetime
        app = {'platform': 'GP', 'pkg_or_id': 'test.app', 'company_cn': 'A',
               'developer': 'Old', 'dev_link': 'old', 'removed': True, 'release_date': '2026/01/01'}
        row = {'id': 'test.app', 'company': 'A', 'store_status': '200',
               'checked_at': datetime.now().isoformat(), 'library_developer': 'Old',
               'library_dev_link': 'old', 'current_developer': 'Current',
               'current_dev_link': 'https://play.google.com/store/apps/dev?id=123'}
        changes, conflicts = apply_verified_identities([app], [row])
        self.assertEqual(len(changes), 1)
        self.assertFalse(conflicts)
        self.assertFalse(app['removed'])
        self.assertEqual(app['company_cn'], 'A')
        self.assertEqual(app['developer'], 'Current')

    def test_apply_does_not_overwrite_intervening_changes_or_404(self):
        from datetime import datetime
        app = {'platform': 'GP', 'pkg_or_id': 'test.app', 'company_cn': 'A',
               'developer': 'User Edited', 'dev_link': 'old', 'removed': False}
        row = {'id': 'test.app', 'company': 'A', 'store_status': '200',
               'checked_at': datetime.now().isoformat(), 'library_developer': 'Old',
               'library_dev_link': 'old', 'current_developer': 'Current', 'current_dev_link': 'new'}
        changes, conflicts = apply_verified_identities([app], [row])
        self.assertFalse(changes)
        self.assertTrue(conflicts)
        row['store_status'] = '404'
        changes, conflicts = apply_verified_identities([app], [row])
        self.assertFalse(changes)
        self.assertFalse(app['removed'])

    def test_missing_metadata_is_incomplete_not_removed(self):
        row = classify_app_page('<html>Try again</html>', 200, URL, 'test.app')
        self.assertEqual(row['store_status'], 'incomplete')

    def test_explicit_not_found_is_regional_unavailable(self):
        body = "We're sorry, the requested URL was not found on this server."
        self.assertEqual(classify_app_page(body, 200, URL, 'test.app')['store_status'], 'soft_404')
        self.assertEqual(classify_app_page('', 404, URL, 'test.app')['store_status'], '404')
        self.assertEqual(classify_app_page('', 429, URL, 'test.app')['store_status'], 'error')

    def test_redirect_and_wrong_product_are_incomplete(self):
        self.assertEqual(classify_app_page(LIVE, 200, 'https://consent.google.com/', 'test.app')['store_status'], 'incomplete')
        self.assertEqual(classify_app_page(LIVE, 200, URL, 'different.app')['store_status'], 'incomplete')

    def test_live_requires_title_and_native_identity(self):
        row = classify_app_page(LIVE, 200, URL, 'test.app')
        self.assertEqual(row['store_status'], '200')
        self.assertEqual(row['current_developer'], 'Current Name')
        self.assertIn('/dev?id=123', row['current_dev_link'])

    def test_incomplete_page_retries(self):
        from types import SimpleNamespace
        responses = [SimpleNamespace(text='', status_code=200, url=URL),
                     SimpleNamespace(text=LIVE, status_code=200, url=URL)]
        with patch('gp_store_verification.requests.get', side_effect=responses), patch('gp_store_verification.time.sleep'):
            row = verify_app('test.app')
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(row['store_status'], '200')

    def test_removed_history_remains_a_recovery_seed(self):
        app = {'platform': 'GP', 'pkg_or_id': 'test.app', 'company_cn': 'A',
               'developer': 'Old', 'dev_link': 'https://play.google.com/store/apps/dev?id=123', 'removed': True}
        self.assertEqual(next(iter(extract_gp_developers([app]).values()))['seed_pkgs'], ['test.app'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.json'
            path.write_text(json.dumps({'gp_scan': {'error_details': [{'company': 'A', 'developer': 'Old', 'url': app['dev_link']}]}}))
            with patch('audit_domestic_reconciliation.verify_app', return_value={'store_status': '200'}) as check:
                rows = verify_failed_gp_store_pages(str(path), [app], set(), ())
            check.assert_called_once_with('test.app')
            self.assertTrue(rows[0]['previously_removed'])

    def test_partial_live_results_cannot_hide_errors(self):
        apps = [{'platform': 'GP', 'pkg_or_id': pkg, 'company_cn': 'A', 'developer': 'Old'} for pkg in ['a.one', 'b.two']]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'summary.json'
            path.write_text(json.dumps({'gp_scan': {'error_details': [{'company': 'A', 'developer': 'Old'}]}}))
            rows = build_failed_gp_review(str(path), apps, apps,
                {'GP:a.one': {'publisher_id': '1'}}, {}, defaultdict(list), set(), (),
                [{'company': 'A', 'id': 'a.one', 'store_status': '200', 'current_developer': 'New'},
                 {'company': 'A', 'id': 'b.two', 'store_status': 'incomplete'}])
        self.assertEqual(rows[0]['scope_status'], 'unresolved_store_verification_errors')

    def test_developer_parser_excludes_recommendations(self):
        data = [[None, [[None] * 22]]]
        data[0][1][0][21] = [[[["owned.app", 1]]], [None, None, None, [None, 'NEXT']]]
        body = "AF_initDataCallback({key: 'ds:3', data:" + json.dumps(data) + ", sideChannel: {}});"
        body += '<a href="/store/apps/details?id=unrelated.app">Recommendation</a>'
        packages, token = parse_developer_page(body, True)
        self.assertEqual(packages, {'owned.app'})
        self.assertEqual(token, 'NEXT')

    def test_pagination_failure_preserves_partial_and_incomplete(self):
        from types import SimpleNamespace
        response = SimpleNamespace(text='', raise_for_status=lambda: None)
        import requests
        with patch('gp_store_verification.requests.get', return_value=response), \
             patch('gp_store_verification.parse_developer_page', return_value=({'owned.app'}, 'NEXT')), \
             patch('gp_store_verification.requests.post', side_effect=requests.Timeout('timeout')):
            result = scan_developer('https://play.google.com/store/apps/dev?id=123')
        self.assertFalse(result['complete'])
        self.assertEqual(result['packages'], ['owned.app'])

    def test_modern_pagination_checks_native_account_and_adds_apps(self):
        from types import SimpleNamespace
        item = [None] * 13
        item[12] = ['second.app', 7]
        section = [[item], 'More by Developer', [None, None, None, None,
                   [None, None, '/store/apps/dev?id=123']], None]
        data = [[None, None, None, None, None, None, section]]
        first = SimpleNamespace(text='', raise_for_status=lambda: None)
        rpc = SimpleNamespace(text=")]}'\n" + json.dumps([['wrb.fr', 'qnKhOb', json.dumps(data)]]), raise_for_status=lambda: None)
        with patch('gp_store_verification.requests.get', return_value=first), \
             patch('gp_store_verification.parse_developer_page', return_value=({'first.app'}, 'NEXT')), \
             patch('gp_store_verification.requests.post', return_value=rpc), \
             patch('gp_store_verification.time.sleep'):
            row = scan_developer('https://play.google.com/store/apps/dev?id=123')
            self.assertTrue(row['complete'])
            self.assertEqual(row['packages'], ['first.app', 'second.app'])
            row = scan_developer('https://play.google.com/store/apps/dev?id=456')
            self.assertFalse(row['complete'])

    def test_legacy_envelope_with_modern_cards(self):
        from types import SimpleNamespace
        card = [None] * 13
        card[12] = ['second.app', 7]
        data = [[[[card]], None, None, None, None, None, None, None]]
        first = SimpleNamespace(text='', raise_for_status=lambda: None)
        rpc = SimpleNamespace(text=")]}'\n" + json.dumps([['wrb.fr', 'qnKhOb', json.dumps(data)]]), raise_for_status=lambda: None)
        with patch('gp_store_verification.requests.get', return_value=first), \
             patch('gp_store_verification.parse_developer_page', return_value=({'first.app'}, 'NEXT')), \
             patch('gp_store_verification.requests.post', return_value=rpc), \
             patch('gp_store_verification.time.sleep'):
            row = scan_developer('https://play.google.com/store/apps/developer?id=Name')
        self.assertTrue(row['complete'])
        self.assertEqual(row['packages'], ['first.app', 'second.app'])


if __name__ == '__main__':
    unittest.main()
