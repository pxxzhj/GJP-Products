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
                auto_repair=False,
            )

        self.assertFalse(result['passed'])
        self.assertEqual(result['live_missing'], 3)

    def test_live_missing_apps_are_repaired_then_reaudited(self):
        with tempfile.TemporaryDirectory() as run_dir:
            audit_calls = []

            def runner(command, cwd):
                audit_calls.append(command)
                live_missing = 1 if len(audit_calls) == 1 else 0
                with open(
                    os.path.join(run_dir, 'reconciliation_summary.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({'strong_store_live': live_missing}, f)
                with open(
                    os.path.join(
                        run_dir, 'strong_candidate_store_verification.csv'
                    ),
                    'w',
                    encoding='utf-8',
                ) as f:
                    f.write('store_status\n200\n')
                return SimpleNamespace(returncode=1 if live_missing else 0)

            def repair_runner(command, cwd):
                self.assertIn('--expand-developers', command)
                report_dir = command[command.index('--report-dir') + 1]
                with open(
                    os.path.join(report_dir, 'import_report.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({
                        'added': [{
                            'platform': 'GP',
                            'id': 'new.app',
                            'company': '公司A',
                            'name': 'New App',
                            'developer': 'Developer A',
                        }],
                        'already_present': [],
                        'failed': [],
                        'expansion_errors': [{
                            'platform': 'iOS',
                            'company': '公司A',
                            'developer_id': '100',
                            'error': 'old developer entry unavailable',
                        }],
                        'cross_company_collisions': [],
                    }, f)
                return SimpleNamespace(returncode=0)

            result = run_appmagic_completeness_gate(
                run_dir=run_dir,
                runner=runner,
                repair_runner=repair_runner,
            )

        self.assertTrue(result['passed'])
        self.assertEqual(len(audit_calls), 2)
        self.assertEqual(result['repair_rounds'], 1)
        self.assertEqual(result['auto_repair_added'][0]['id'], 'new.app')
        self.assertEqual(len(result['repair_warnings']), 1)

    def test_incomplete_audit_blocks_before_auto_repair(self):
        with tempfile.TemporaryDirectory() as run_dir:
            def runner(command, cwd):
                with open(
                    os.path.join(run_dir, 'reconciliation_summary.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({
                        'strong_store_live': 1,
                        'publisher_fetch_errors': 2,
                    }, f)
                return SimpleNamespace(returncode=1)

            repair_called = []
            result = run_appmagic_completeness_gate(
                run_dir=run_dir,
                runner=runner,
                repair_runner=lambda command, cwd: repair_called.append(command),
            )

        self.assertFalse(result['passed'])
        self.assertEqual(result['blocking_reason'], 'audit_incomplete')
        self.assertEqual(result['incomplete'], {'publisher_fetch_errors': 2})
        self.assertEqual(repair_called, [])

    def test_auto_repair_failures_remain_blocking(self):
        with tempfile.TemporaryDirectory() as run_dir:
            def runner(command, cwd):
                with open(
                    os.path.join(run_dir, 'reconciliation_summary.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({'strong_store_live': 1}, f)
                with open(
                    os.path.join(
                        run_dir, 'strong_candidate_store_verification.csv'
                    ),
                    'w',
                    encoding='utf-8',
                ) as f:
                    f.write('store_status\n200\n')
                return SimpleNamespace(returncode=1)

            def repair_runner(command, cwd):
                report_dir = command[command.index('--report-dir') + 1]
                with open(
                    os.path.join(report_dir, 'import_report.json'),
                    'w',
                    encoding='utf-8',
                ) as f:
                    json.dump({
                        'added': [],
                        'already_present': [],
                        'failed': [{
                            'platform': 'GP',
                            'id': 'new.app',
                            'company': '公司A',
                            'error': 'timeout',
                        }],
                        'expansion_errors': [],
                        'cross_company_collisions': [],
                    }, f)
                return SimpleNamespace(returncode=0)

            result = run_appmagic_completeness_gate(
                run_dir=run_dir,
                runner=runner,
                repair_runner=repair_runner,
            )

        self.assertFalse(result['passed'])
        self.assertEqual(result['blocking_reason'], 'auto_repair_incomplete')

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
