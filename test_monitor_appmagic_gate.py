import json
import os
import tempfile
import unittest
from types import SimpleNamespace

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
