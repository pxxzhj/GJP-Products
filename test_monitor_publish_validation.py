import subprocess
import unittest
from unittest.mock import patch

import monitor


class PublishValidationTests(unittest.TestCase):
    def test_all_release_checks_run_before_publish(self):
        commands = []
        monitor.validate_before_publish(runner=lambda command, **kwargs: commands.append((command, kwargs)))
        self.assertEqual(len(commands), 4)
        self.assertIn('py_compile', commands[0][0])
        self.assertIn('test_*.py', commands[1][0])
        self.assertEqual(commands[-1][0], ['node', 'validate_library.js'])
        self.assertTrue(all(options['check'] for _, options in commands))

    def test_failed_validation_prevents_staging_or_commit(self):
        with patch.object(monitor, 'validate_before_publish', side_effect=subprocess.CalledProcessError(1, 'node')), \
             patch.object(monitor.subprocess, 'run') as run:
            with self.assertRaises(subprocess.CalledProcessError):
                monitor.git_commit_push(1, 2)
        run.assert_not_called()
