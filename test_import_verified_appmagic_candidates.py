import csv
import os
import tempfile
import unittest
from unittest.mock import patch

from import_verified_appmagic_candidates import (
    expand_native_developer_accounts,
    load_verified_candidates,
)


class VerifiedCandidateImportTests(unittest.TestCase):
    def write_candidates(self, path, rows):
        fields = [
            'review_status', 'company', 'platform', 'id', 'store_status',
            'publisher_linked_companies', 'existing_library_companies',
        ]
        with open(path, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_only_unique_strong_live_candidates_are_importable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'candidates.csv')
            self.write_candidates(path, [{
                'review_status': 'strong_appmagic_unique_publisher',
                'company': '公司A',
                'platform': 'GP',
                'id': 'new.app',
                'store_status': '200',
                'publisher_linked_companies': '公司A',
                'existing_library_companies': '',
            }])
            rows = load_verified_candidates(path)

        self.assertEqual([row['id'] for row in rows], ['new.app'])

    def test_shared_publisher_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'candidates.csv')
            self.write_candidates(path, [{
                'review_status': 'strong_appmagic_unique_publisher',
                'company': '公司A',
                'platform': 'GP',
                'id': 'new.app',
                'store_status': '200',
                'publisher_linked_companies': '公司A;公司B',
                'existing_library_companies': '',
            }])
            with self.assertRaises(RuntimeError):
                load_verified_candidates(path)

    def test_ios_collision_does_not_expand_the_other_company_account(self):
        apps = [
            {
                'company_cn': '公司A',
                'platform': 'iOS',
                'pkg_or_id': '1',
                'dev_link': 'https://apps.apple.com/developer/id100',
                'developer': 'Developer A',
            },
            {
                'company_cn': '公司B',
                'platform': 'iOS',
                'pkg_or_id': '2',
                'dev_link': 'https://apps.apple.com/developer/id200',
                'developer': 'Developer B',
            },
        ]
        positions = {
            (app['platform'], app['pkg_or_id']): index
            for index, app in enumerate(apps)
        }
        calls = []

        def fetch_developer(artist_id):
            calls.append(artist_id)
            if artist_id == '200':
                self.fail('cross-company developer account must not be expanded')
            return [{
                'wrapperType': 'software',
                'trackId': 2,
                'trackName': 'Other Company App',
                'artistId': 200,
                'artistName': 'Developer B',
                'artistViewUrl': 'https://apps.apple.com/developer/id200',
                'releaseDate': '2025-01-01T00:00:00Z',
                'currentVersionReleaseDate': '2025-01-02T00:00:00Z',
                'price': 0,
            }]

        collisions = []
        with patch(
            'import_verified_appmagic_candidates.fetch_ios_developer',
            side_effect=fetch_developer,
        ):
            result = expand_native_developer_accounts(
                apps,
                positions,
                [apps[0]],
                [],
                [],
                collisions,
            )

        self.assertEqual(calls, ['100'])
        self.assertEqual(len(collisions), 1)
        self.assertEqual(result['scanned_ios_accounts'], 1)


if __name__ == '__main__':
    unittest.main()
