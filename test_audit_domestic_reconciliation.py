import json
import os
import tempfile
import unittest
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from unittest.mock import patch

from audit_domestic_reconciliation import (
    build_candidates,
    cache_entry_is_fresh,
    fetch_all_publishers,
    fetch_known_mappings,
    validate_candidate_classification,
)


def app(app_id, company='公司A'):
    return {'platform': 'GP', 'pkg_or_id': app_id, 'company_cn': company}


class AppMagicPublisherClassificationTests(unittest.TestCase):
    def test_successful_cache_entries_expire(self):
        recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec='seconds')
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec='seconds')
        self.assertTrue(cache_entry_is_fresh({'fetched_at': recent}, 20))
        self.assertFalse(cache_entry_is_fresh({'fetched_at': old}, 20))
        self.assertFalse(cache_entry_is_fresh({}, 20))

    def test_failed_cache_entries_are_never_fresh(self):
        recent = datetime.now().isoformat(timespec='seconds')
        self.assertFalse(cache_entry_is_fresh({
            'fetched_at': recent,
            'error': '429',
        }, 20))
        self.assertFalse(cache_entry_is_fresh({
            'fetched_at': recent,
            'refresh_error': 'timeout',
        }, 20))

    def test_failed_publisher_refresh_preserves_last_successful_apps(self):
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec='seconds')
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, 'publishers.json')
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump({'publisher': {
                    'apps': [{'platform': 'GP', 'id': 'known.app'}],
                    'error': '',
                    'fetched_at': old,
                }}, f)
            with patch(
                'audit_domestic_reconciliation.fetch_publisher_apps',
                return_value={'publisher': {'error': '429', 'apps': []}},
            ):
                result = fetch_all_publishers(
                    {'publisher'}, cache_path, refresh_after_hours=20
                )

        self.assertEqual(result['publisher']['apps'][0]['id'], 'known.app')
        self.assertEqual(result['publisher']['refresh_error'], '429')
        self.assertFalse(cache_entry_is_fresh(result['publisher'], 20))

    def test_failed_mapping_refresh_preserves_last_successful_mapping(self):
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec='seconds')
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, 'mappings.json')
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump({'GP:known.app': {
                    'status': 'mapped',
                    'publisher_id': 'publisher',
                    'fetched_at': old,
                }}, f)
            with patch(
                'audit_domestic_reconciliation.search_appmagic_by_ids',
                return_value={'GP:known.app': {'error': 'timeout'}},
            ):
                result = fetch_known_mappings(
                    [app('known.app')], cache_path, refresh_after_hours=20
                )

        self.assertEqual(result['GP:known.app']['publisher_id'], 'publisher')
        self.assertEqual(result['GP:known.app']['refresh_error'], 'timeout')
        self.assertFalse(cache_entry_is_fresh(result['GP:known.app'], 20))

    def test_unique_publisher_with_multiple_known_apps_is_strong(self):
        rows = build_candidates(
            [app('known.one'), app('known.two')],
            {'publisher': {'apps': [{
                'platform': 'GP',
                'id': 'missing.app',
                'name': 'Missing',
                'store_publisher_name': 'Renamed Developer',
            }]}},
            {'publisher': Counter({'公司A': 2})},
            {'公司A': Counter({'publisher': 2})},
            defaultdict(list),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]['review_status'],
            'strong_appmagic_unique_publisher',
        )

    def test_unique_publisher_with_one_known_app_is_strong(self):
        rows = build_candidates(
            [app('known.one')],
            {'publisher': {'apps': [{
                'platform': 'GP',
                'id': 'missing.app',
                'name': 'Missing',
                'store_publisher_name': 'Renamed Developer',
            }]}},
            {'publisher': Counter({'公司A': 1})},
            {'公司A': Counter({'publisher': 1})},
            defaultdict(list),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]['review_status'],
            'strong_appmagic_unique_publisher',
        )

    def test_shared_publisher_still_requires_manual_review(self):
        rows = build_candidates(
            [app('known.a', '公司A'), app('known.b', '公司B')],
            {'publisher': {'apps': [{
                'platform': 'GP', 'id': 'missing.app', 'name': 'Missing'
            }]}},
            {'publisher': Counter({'公司A': 1, '公司B': 1})},
            {
                '公司A': Counter({'publisher': 1}),
                '公司B': Counter({'publisher': 1}),
            },
            defaultdict(list),
        )
        self.assertTrue(rows)
        self.assertTrue(all(
            row['review_status'] == 'appmagic_shared_publisher_manual'
            for row in rows
        ))

    def test_invariant_rejects_future_downgrade(self):
        with self.assertRaises(RuntimeError):
            validate_candidate_classification([{
                'company': '公司A',
                'platform': 'GP',
                'id': 'missing.app',
                'publisher_linked_companies': '公司A',
                'known_company_overlap': 1,
                'existing_library_companies': '',
                'review_status': 'appmagic_multi_overlap_manual',
            }])


if __name__ == '__main__':
    unittest.main()
