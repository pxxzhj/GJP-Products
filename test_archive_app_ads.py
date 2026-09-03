import unittest
from collections import Counter

from archive_app_ads import (
    candidate_evidence,
    normalize_candidate,
    validate_candidate_evidence,
)
from audit_web_evidence import EvidenceAuditor, filter_email_domain_tainted_pages


class ArchiveAppAdsSafetyTests(unittest.TestCase):
    def test_email_domain_queue_entry_point_is_disabled(self):
        auditor = EvidenceAuditor.__new__(EvidenceAuditor)
        auditor.stats = Counter()
        self.assertEqual(auditor.queue_domain_seed('example.com', {}), 0)
        self.assertEqual(auditor.stats['email_domain_seed_attempts_blocked'], 1)

    def test_social_profiles_are_not_candidates(self):
        self.assertEqual(normalize_candidate('https://weibo.com/u/123'), '')
        self.assertEqual(normalize_candidate('https://m.weibo.cn/u/123'), '')

    def test_candidate_evidence_uses_current_store_sources_only(self):
        apps = [{
            'platform': 'iOS',
            'pkg_or_id': '1',
            'company_cn': 'A',
            'developer': 'Developer',
            'dev_link': 'https://apps.apple.com/developer/id1',
        }]
        evidence = candidate_evidence(
            apps,
            {'iOS:1': {'seller_url': 'https://example.com/support'}},
            {},
        )
        ref = evidence['https://example.com/app-ads.txt'][0]
        self.assertEqual(ref['source'], 'current_ios_lookup')
        self.assertEqual(ref['source_kind'], 'app_store')
        self.assertTrue(validate_candidate_evidence(evidence)['passed'])

    def test_guard_rejects_historical_or_email_domain_sources(self):
        for source, source_kind in [
            ('historical_store_evidence', 'app_store'),
            ('email_domain', 'website'),
        ]:
            evidence = {'https://example.com/app-ads.txt': [{
                'app_key': 'iOS:1',
                'source': source,
                'source_kind': source_kind,
            }]}
            with self.assertRaises(RuntimeError):
                validate_candidate_evidence(evidence)

    def test_email_domain_lineage_is_excluded_from_old_web_runs(self):
        base = {
            'company': 'A',
            'app_name': 'Seed',
            'source_url': 'https://example.com',
        }
        pages = [
            {**base, 'source_kind': 'email_domain'},
            {**base, 'source_kind': 'same_domain_link', 'source_url': 'https://example.com/apps'},
            {
                'company': 'A',
                'app_name': 'Seed',
                'source_kind': 'website',
                'source_url': 'https://official.example.org',
            },
        ]
        clean = filter_email_domain_tainted_pages(pages)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]['source_url'], 'https://official.example.org')

    def test_store_seeded_domain_survives_legacy_email_seed(self):
        base = {'company': 'A', 'app_name': 'Seed'}
        pages = [
            {**base, 'source_kind': 'email_domain', 'source_url': 'https://example.com'},
            {**base, 'source_kind': 'website', 'source_url': 'https://example.com'},
            {**base, 'source_kind': 'same_domain_link', 'source_url': 'https://example.com/apps'},
        ]
        clean = filter_email_domain_tainted_pages(pages)
        self.assertEqual(
            [row['source_kind'] for row in clean],
            ['website', 'same_domain_link'],
        )


if __name__ == '__main__':
    unittest.main()
