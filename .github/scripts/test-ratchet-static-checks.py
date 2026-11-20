#!/usr/bin/env python3
"""Verify the diff-aware ruff + gradual mypy ratchet gate."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / '.github/scripts/ratchet-static-checks.py'


def _load_ratchet_module():
    spec = importlib.util.spec_from_file_location('ratchet_static_checks', SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RATCHET = _load_ratchet_module()


def _resolve_tool(name: str) -> str | None:
    sibling = Path(sys.executable).parent / name
    if sibling.is_file():
        return str(sibling)
    return shutil.which(name)


RUFF_BIN = _resolve_tool('ruff')
MYPY_BIN = _resolve_tool('mypy')
LIVE_TOOLS_AVAILABLE = RUFF_BIN is not None and MYPY_BIN is not None
REQUIRE_LIVE_TOOLS = os.environ.get('RATCHET_REQUIRE_TOOLS') == '1'


class RatchetUnitTests(unittest.TestCase):
    """Stdlib-only contract tests that run without ruff or mypy installed."""

    def test_compare_ratchet_enforces_zero_errors_on_new_files(self) -> None:
        diag = RATCHET.Diagnostic('ruff', 'pkg/new.py', 1, 'F401', 'unused')
        regressions, fixed = RATCHET.compare_ratchet(
            {'pkg/new.py': 'A'},
            {'pkg/new.py': [diag]},
            {},
        )
        self.assertEqual(regressions, [diag])
        self.assertEqual(fixed, 0)

    def test_compare_ratchet_allows_unchanged_legacy_signatures_and_blocks_new(self) -> None:
        old = RATCHET.Diagnostic('mypy', 'pkg/old.py', 10, 'attr-defined', 'old error')
        moved_old = RATCHET.Diagnostic('mypy', 'pkg/old.py', 14, 'attr-defined', 'old error')
        brand_new = RATCHET.Diagnostic('mypy', 'pkg/old.py', 22, 'arg-type', 'bad arg')
        regressions, fixed = RATCHET.compare_ratchet(
            {'pkg/old.py': 'M'},
            {'pkg/old.py': [moved_old, brand_new]},
            {'pkg/old.py': [old]},
        )
        self.assertEqual(regressions, [brand_new])
        self.assertEqual(fixed, 0)

    def test_compare_ratchet_counts_duplicate_net_new_signature_and_fixes(self) -> None:
        base_diag = RATCHET.Diagnostic('ruff', 'pkg/a.py', 1, 'F401', 'unused `os`')
        dup1 = RATCHET.Diagnostic('ruff', 'pkg/a.py', 1, 'F401', 'unused `os`')
        dup2 = RATCHET.Diagnostic('ruff', 'pkg/a.py', 5, 'F401', 'unused `os`')
        regressions, _ = RATCHET.compare_ratchet(
            {'pkg/a.py': 'M'},
            {'pkg/a.py': [dup1, dup2]},
            {'pkg/a.py': [base_diag]},
        )
        self.assertEqual(regressions, [dup2])

        regressions, fixed = RATCHET.compare_ratchet(
            {'pkg/a.py': 'M'},
            {'pkg/a.py': []},
            {'pkg/a.py': [base_diag]},
        )
        self.assertEqual(regressions, [])
        self.assertEqual(fixed, 1)

    def test_mypy_parser_extracts_file_line_code_and_message(self) -> None:
        output = (
            'malbut_agent_server/malbut_agent_server/foo.py:12:5: '
            'error: Incompatible return value type [return-value]\n'
            'malbut_agent_server/malbut_agent_server/foo.py:12: note: detail\n'
            'malbut_agent_server/malbut_agent_server/other.py:3: '
            'error: Ignored unrelated file [misc]\n'
        )
        parsed = RATCHET._parse_mypy_output(
            output,
            Path('/repo'),
            ['malbut_agent_server/malbut_agent_server/foo.py'],
        )
        diags = parsed['malbut_agent_server/malbut_agent_server/foo.py']
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].line, 12)
        self.assertEqual(diags[0].code, 'return-value')
        self.assertIn('Incompatible return value type', diags[0].message)

    def test_excluded_mirror_and_vendor_paths_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'repo'
            repo.mkdir()
            shutil.copy2(ROOT / 'pyproject.toml', repo / 'pyproject.toml')
            (repo / 'README.md').write_text('hi\n', encoding='utf-8')
            self._git(repo, 'init')
            self._git(repo, 'config', 'user.email', 'ci-test@example.com')
            self._git(repo, 'config', 'user.name', 'CI Test')
            self._git(repo, 'add', '.')
            self._git(repo, 'commit', '-m', 'base')
            base = self._git(repo, 'rev-parse', 'HEAD').stdout.strip()
            for rel in (
                'malbut_test/malbut_agent_server/malbut_agent_server/mirror.py',
                'malbut_yolo/vendor/third_party.py',
            ):
                target = repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('import os\n', encoding='utf-8')
            self._git(repo, 'add', '.')
            self._git(repo, 'commit', '-m', 'change')
            code, report = RATCHET.run_ratchet_checks(repo, raw_base=base)
            self.assertEqual(code, 0, report)
            self.assertIn('no eligible Python files changed', report)

    @staticmethod
    def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', *args],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )


class RatchetLiveToolTests(unittest.TestCase):
    """End-to-end tests exercising real ruff and mypy binaries on temp git repos."""

    def setUp(self) -> None:
        if not LIVE_TOOLS_AVAILABLE:
            if REQUIRE_LIVE_TOOLS:
                self.fail('RATCHET_REQUIRE_TOOLS=1 but ruff/mypy are not installed')
            self.skipTest('ruff/mypy not installed in stdlib-only environment')
        assert RUFF_BIN is not None and MYPY_BIN is not None
        self.ruff_bin = RUFF_BIN
        self.mypy_bin = MYPY_BIN

    def _init_repo(self, temp: str, initial_files: dict[str, str]) -> tuple[Path, str]:
        repo = Path(temp) / 'repo'
        repo.mkdir()
        shutil.copy2(ROOT / 'pyproject.toml', repo / 'pyproject.toml')
        for rel, content in initial_files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        self._git(repo, 'init')
        self._git(repo, 'config', 'user.email', 'ci-test@example.com')
        self._git(repo, 'config', 'user.name', 'CI Test')
        self._git(repo, 'add', '.')
        self._git(repo, 'commit', '-m', 'base')
        base_sha = self._git(repo, 'rev-parse', 'HEAD').stdout.strip()
        return repo, base_sha

    @staticmethod
    def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', *args],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )

    def _commit_changes(self, repo: Path, updates: dict[str, str]) -> None:
        for rel, content in updates.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        self._git(repo, 'add', '.')
        self._git(repo, 'commit', '-m', 'change')

    def test_new_clean_file_passes_both_ruff_and_mypy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo, base = self._init_repo(temp, {
                'malbut_agent_server/malbut_agent_server/__init__.py': '',
            })
            self._commit_changes(repo, {
                'malbut_agent_server/malbut_agent_server/clean_mod.py': (
                    '"""Clean module."""\n\n'
                    'def double(value: int) -> int:\n'
                    '    return value * 2\n'
                ),
            })
            code, report = RATCHET.run_ratchet_checks(
                repo, raw_base=base, ruff_bin=self.ruff_bin, mypy_bin=self.mypy_bin,
            )
            self.assertEqual(code, 0, report)
            self.assertIn('new=0', report)

    def test_new_file_with_ruff_or_mypy_error_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo, base = self._init_repo(temp, {
                'malbut_agent_server/malbut_agent_server/__init__.py': '',
            })
            self._commit_changes(repo, {
                'malbut_agent_server/malbut_agent_server/bad_mod.py': (
                    'import os\n\n'
                    'def greet(name: str) -> int:\n'
                    '    return name\n'
                ),
            })
            code, report = RATCHET.run_ratchet_checks(
                repo, raw_base=base, ruff_bin=self.ruff_bin, mypy_bin=self.mypy_bin,
            )
            self.assertEqual(code, 1, report)
            self.assertIn('[ruff:F401]', report)
            self.assertIn('[mypy:return-value]', report)

    def test_legacy_errors_in_modified_file_do_not_block_clean_edits(self) -> None:
        legacy = (
            'import sys\n\n'
            'def old_bug(x: int) -> str:\n'
            '    return x\n\n'
            'def helper(val: int) -> int:\n'
            '    return val + 1\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            repo, base = self._init_repo(temp, {
                'malbut_agent_server/malbut_agent_server/__init__.py': '',
                'malbut_agent_server/malbut_agent_server/legacy.py': legacy,
            })
            updated = legacy.replace(
                '    return val + 1\n',
                '    result = val + 2\n    return result\n',
            )
            self._commit_changes(repo, {
                'malbut_agent_server/malbut_agent_server/legacy.py': updated,
            })
            code, report = RATCHET.run_ratchet_checks(
                repo, raw_base=base, ruff_bin=self.ruff_bin, mypy_bin=self.mypy_bin,
            )
            self.assertEqual(code, 0, report)

    def test_modified_legacy_file_blocks_newly_introduced_ruff_and_ripple_mypy_errors(self) -> None:
        legacy = (
            'import sys\n\n'
            'def old_bug(x: int) -> str:\n'
            '    return x\n\n'
            'def producer() -> int:\n'
            '    return 1\n\n'
            'def consumer() -> int:\n'
            '    return producer() + 1\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            repo, base = self._init_repo(temp, {
                'malbut_agent_server/malbut_agent_server/__init__.py': '',
                'malbut_agent_server/malbut_agent_server/legacy.py': legacy,
            })
            updated = (
                'import json\n'
                + legacy.replace(
                    'def producer() -> int:\n    return 1\n',
                    'def producer() -> str:\n    return "one"\n',
                )
            )
            self._commit_changes(repo, {
                'malbut_agent_server/malbut_agent_server/legacy.py': updated,
            })
            code, report = RATCHET.run_ratchet_checks(
                repo, raw_base=base, ruff_bin=self.ruff_bin, mypy_bin=self.mypy_bin,
            )
            self.assertEqual(code, 1, report)
            self.assertIn('[ruff:F401]', report)
            self.assertIn('`json` imported but unused', report)
            self.assertNotIn('`sys` imported but unused', report)
            self.assertIn('[mypy:operator]', report)

    def test_fixing_legacy_errors_passes_and_reports_ratchet_improvement(self) -> None:
        legacy = (
            'import sys\n\n'
            'def old_bug(x: int) -> str:\n'
            '    return x\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            repo, base = self._init_repo(temp, {
                'malbut_agent_server/malbut_agent_server/__init__.py': '',
                'malbut_agent_server/malbut_agent_server/legacy.py': legacy,
            })
            fixed = 'def old_bug(x: int) -> str:\n    return str(x)\n'
            self._commit_changes(repo, {
                'malbut_agent_server/malbut_agent_server/legacy.py': fixed,
            })
            code, report = RATCHET.run_ratchet_checks(
                repo, raw_base=base, ruff_bin=self.ruff_bin, mypy_bin=self.mypy_bin,
            )
            self.assertEqual(code, 0, report)
            self.assertIn('fixed=1', report)


if __name__ == '__main__':
    unittest.main()
