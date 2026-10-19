import unittest
from unittest.mock import patch
from gp_candidate_ownership import authorize_candidates


class CandidateOwnershipTests(unittest.TestCase):
    def check(self, anchor_publisher, anchor_native, candidate_mapping):
        app = {'platform': 'GP', 'pkg_or_id': 'known.app', 'company_cn': 'A',
               'dev_link': 'https://play.google.com/store/apps/dev?id=123'}
        candidates = {'new.app': {'company': 'A', 'dev_url': app['dev_link']}}
        def summarize(raw, platform, pkg):
            return {'status': 'mapped', 'publisher_id': anchor_publisher,
                    'store_publisher_id': anchor_native} if pkg == 'known.app' else candidate_mapping
        with patch('audit_domestic_reconciliation.summarize_mapping', side_effect=summarize):
            return authorize_candidates([app], candidates, search=lambda targets: {})

    def test_transferred_anchor_does_not_authorize_different_publisher(self):
        accepted, pending = self.check('old', 'Old Name', {'status': 'mapped', 'publisher_id': 'new'})
        self.assertFalse(accepted)
        self.assertEqual(len(pending), 1)

    def test_existing_publisher_authorizes_candidate(self):
        accepted, pending = self.check('same', '123', {'status': 'mapped', 'publisher_id': 'same'})
        self.assertIn('new.app', accepted)
        self.assertFalse(pending)

    def test_not_indexed_new_app_needs_explicit_native_account_anchor(self):
        accepted, _ = self.check('same', '123', {'status': 'not_found'})
        self.assertIn('new.app', accepted)
        accepted, pending = self.check('old', 'Old Name', {'status': 'not_found'})
        self.assertFalse(accepted)
        self.assertTrue(pending)

    def test_network_failure_does_not_authorize(self):
        accepted, pending = self.check('same', '123', {'status': 'error', 'error': '429'})
        self.assertFalse(accepted)
        self.assertTrue(pending)
