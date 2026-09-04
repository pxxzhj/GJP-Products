import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import monitor
from monitor import load_current_library, run_appmagic_completeness_gate


class AppMagicCompletenessGateTests(unittest.TestCase):
    def test_company_files_are_the_canonical_monitor_input(self):
        with tempfile.TemporaryDirectory() as data_dir:
            with open(
                os.path.join(data_dir, '公司A.js'),
                'w',
                encoding='utf-8',
            ) as f:
                f.write(
                    'window._loadCompany("公司A", '
                    '[{"company_cn":"公司A","platform":"GP",'
                    '"pkg_or_id":"known.app"}]);'
                )
            apps = load_current_library(data_dir)

        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]['pkg_or_id'], 'known.app')

    def test_duplicate_keys_across_company_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as data_dir:
            for company in ('公司A', '公司B'):
                with open(
                    os.path.join(data_dir, f'{company}.js'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    f.write(
                        f'window._loadCompany("{company}", '
                        f'[{{"company_cn":"{company}","platform":"GP",'
                        '"pkg_or_id":"same.app"}]);'
                    )
            with self.assertRaises(RuntimeError):
                load_current_library(data_dir)

    def test_live_missing_apps_block_daily_commit(self):
        with tempfile.TemporaryDirectory() as run_dir:
            def runner(command, cwd):
                self.assertIn('--fail-on-live-candidates', command)
                with open(
                    os.path.join(run_dir, 'reconciliation_summary.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({'strong_store_live': 3}, f)
                return SimpleNamespace(returncode=1)

            result = run_appmagic_completeness_gate(
                run_dir=run_dir,
                runner=runner,
            )

        self.assertFalse(result['passed'])
        self.assertEqual(result['live_missing'], 3)

    def test_clean_audit_allows_daily_commit(self):
        with tempfile.TemporaryDirectory() as run_dir:
            def runner(command, cwd):
                with open(
                    os.path.join(run_dir, 'reconciliation_summary.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({'strong_store_live': 0}, f)
                return SimpleNamespace(returncode=0)

            result = run_appmagic_completeness_gate(
                run_dir=run_dir,
                runner=runner,
            )

        self.assertTrue(result['passed'])
        self.assertEqual(result['live_missing'], 0)

    def test_current_gp_error_summary_is_forwarded_to_audit(self):
        with tempfile.TemporaryDirectory() as run_dir:
            def runner(command, cwd):
                self.assertIn('--verify-failed-gp-store', command)
                summary_index = command.index('--gp-error-summary')
                self.assertEqual(command[summary_index + 1], '/tmp/current.json')
                with open(
                    os.path.join(run_dir, 'reconciliation_summary.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({'strong_store_live': 0}, f)
                return SimpleNamespace(returncode=0)

            result = run_appmagic_completeness_gate(
                run_dir=run_dir,
                gp_error_summary='/tmp/current.json',
                runner=runner,
            )

        self.assertTrue(result['passed'])

    def test_ios_existing_app_is_relinked_instead_of_reported_new(self):
        apps = [
            {
                'company_cn': '公司A', 'platform': 'iOS', 'pkg_or_id': '1',
                'dev_link': 'https://apps.apple.com/developer/id100',
                'developer': 'Old A',
            },
            {
                'company_cn': '公司A', 'platform': 'iOS', 'pkg_or_id': '2',
                'dev_link': 'https://apps.apple.com/developer/id200',
                'developer': 'Old B',
            },
        ]

        def lookup(url):
            if 'id=100' in url:
                return {'results': [
                    {'wrapperType': 'software', 'trackId': 1},
                    {
                        'wrapperType': 'software', 'trackId': 2,
                        'artistId': 300, 'artistName': 'Current Developer',
                    },
                ]}
            return {'results': [
                {'wrapperType': 'software', 'trackId': 2},
            ]}

        with patch('monitor.itunes_lookup', side_effect=lookup), patch('monitor.time.sleep'):
            new_apps = monitor.check_ios_developers(apps)

        self.assertEqual(new_apps, [])
        self.assertEqual(apps[1]['developer'], 'Current Developer')
        self.assertEqual(
            apps[1]['dev_link'],
            'https://apps.apple.com/developer/id300',
        )
        self.assertEqual(len(monitor.IOS_DEVELOPER_SCAN_STATS['identity_updates']), 1)

    def test_ios_cross_company_developer_hit_is_not_reassigned(self):
        apps = [
            {
                'company_cn': '公司A', 'platform': 'iOS', 'pkg_or_id': '1',
                'dev_link': 'https://apps.apple.com/developer/id100',
                'developer': 'Developer A',
            },
            {
                'company_cn': '公司B', 'platform': 'iOS', 'pkg_or_id': '2',
                'dev_link': 'https://apps.apple.com/developer/id200',
                'developer': 'Developer B',
            },
        ]

        def lookup(url):
            if 'id=100' in url:
                return {'results': [
                    {'wrapperType': 'software', 'trackId': 1},
                    {
                        'wrapperType': 'software', 'trackId': 2,
                        'artistId': 300, 'artistName': 'Shared Result',
                    },
                ]}
            return {'results': [
                {'wrapperType': 'software', 'trackId': 2},
            ]}

        with patch('monitor.itunes_lookup', side_effect=lookup), patch('monitor.time.sleep'):
            new_apps = monitor.check_ios_developers(apps)

        self.assertEqual(new_apps, [])
        self.assertEqual(apps[1]['company_cn'], '公司B')
        self.assertEqual(apps[1]['developer'], 'Developer B')
        self.assertEqual(
            len(monitor.IOS_DEVELOPER_SCAN_STATS['cross_company_collisions']),
            1,
        )

    def test_audit_error_without_current_summary_is_not_clean(self):
        with tempfile.TemporaryDirectory() as run_dir:
            with self.assertRaises(RuntimeError):
                run_appmagic_completeness_gate(
                    run_dir=run_dir,
                    runner=lambda command, cwd: SimpleNamespace(returncode=2),
                )

    def test_success_without_current_summary_is_not_clean(self):
        with tempfile.TemporaryDirectory() as run_dir:
            with self.assertRaises(RuntimeError):
                run_appmagic_completeness_gate(
                    run_dir=run_dir,
                    runner=lambda command, cwd: SimpleNamespace(returncode=0),
                )


if __name__ == '__main__':
    unittest.main()
