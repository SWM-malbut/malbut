#!/usr/bin/env python3
"""Check runner mirror ordering without root, network access or apt."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('prefer-apt-fallbacks.py')
SPEC = importlib.util.spec_from_file_location('apt_fallbacks', SCRIPT)
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)
AZURE = 'http://azure.archive.ubuntu.com/ubuntu/'
ARCHIVE = 'https://archive.ubuntu.com/ubuntu/'
SECURITY = 'https://security.ubuntu.com/ubuntu/'
# actions/runner-images ubuntu22/20260927.309 configure-apt-sources.sh
RUNNER_LIST = f'{AZURE}\tpriority:1\n{ARCHIVE}\tpriority:2\n{SECURITY}\tpriority:3\n'


class AptFallbackTests(unittest.TestCase):
    def test_runner_endpoints_are_retained_with_azure_last(self):
        self.assertEqual(HELPER.prefer_fallbacks(RUNNER_LIST),
                         f'{AZURE}\tpriority:3\n{ARCHIVE}\tpriority:1\n'
                         f'{SECURITY}\tpriority:2\n')

    def test_implicit_priorities_follow_explicit_alternatives_but_precede_azure(self):
        before = (f'{AZURE}\tpriority:1\n{ARCHIVE}\tpriority:10\n'
                  f'{SECURITY}\nhttps://other.example/ubuntu/\tarch:amd64\n')
        self.assertEqual(HELPER.prefer_fallbacks(before),
                         f'{AZURE}\tpriority:3\n{ARCHIVE}\tpriority:1\n'
                         f'{SECURITY}\tpriority:2\n'
                         'https://other.example/ubuntu/\tarch:amd64\tpriority:2\n')

    def test_comments_metadata_ties_and_newlines_are_preserved(self):
        before = (f'# original mirrors\r\n\r\n{AZURE}\tpriority:1 type:deb\r\n'
                  f'{ARCHIVE}\tarch:amd64 priority:7\r\n'
                  f'{SECURITY}\tpriority:7  type:index')
        self.assertEqual(HELPER.prefer_fallbacks(before),
                         f'# original mirrors\r\n\r\n{AZURE}\tpriority:2 type:deb\r\n'
                         f'{ARCHIVE}\tarch:amd64 priority:1\r\n'
                         f'{SECURITY}\tpriority:1  type:index')

    def test_only_azure_no_azure_and_empty_lists_stay_unchanged(self):
        for before in ('', '# retained\n', f'{AZURE}\tpriority:1\n',
                       f'{ARCHIVE}\tpriority:2\n{SECURITY}\n'):
            with self.subTest(before=before):
                self.assertEqual(HELPER.prefer_fallbacks(before), before)

    def test_repeated_application_is_idempotent(self):
        for before in (RUNNER_LIST, f'{AZURE}\n{ARCHIVE}\tpriority:20\n{SECURITY}\n'):
            after = HELPER.prefer_fallbacks(before)
            self.assertEqual(HELPER.prefer_fallbacks(after), after)

    def test_missing_file_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'absent.txt'
            result = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                    capture_output=True, text=True, check=True)
            self.assertIn('unchanged', result.stdout)
            self.assertFalse(path.exists())

    def test_cli_updates_the_existing_file_and_logs_both_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'apt-mirrors.txt'
            for before in (RUNNER_LIST, RUNNER_LIST.replace('\n', '\r\n')):
                with self.subTest(before=before):
                    path.write_bytes(before.encode())
                    result = subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                            capture_output=True, check=True)
                    after = HELPER.prefer_fallbacks(before)
                    self.assertEqual(path.read_bytes(), after.encode())
                    self.assertIn(('APT mirrors before:\n' + before).encode(), result.stdout)
                    self.assertIn(('APT mirrors after:\n' + after).encode(), result.stdout)


if __name__ == '__main__':
    unittest.main()
