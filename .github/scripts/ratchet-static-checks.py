#!/usr/bin/env python3
"""Diff-aware ratchet gate for ruff and gradual mypy on changed Python files.

New Python files must pass with zero violations. Modified legacy files fail
only when the PR introduces net-new (code, message) violations compared to
the base revision, preventing unrelated pre-existing debt from blocking work.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
import inspect
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple


EXCLUDED_PREFIXES = (
    'malbut_test/',
    'malbut_yolo/vendor/',
)
MYPY_INCLUDED_PREFIXES = (
    'malbut_agent_server/malbut_agent_server/',
)
MYPY_ARCHIVE_PATHS = (
    'malbut_agent_server/malbut_agent_server',
    'homecam_agent/homecam_detector',
    'malbut_autoslam',
)
_MYPY_LINE_RE = re.compile(
    r'^(?P<file>.+?):(?P<line>\d+):(?:\d+:)?\s*error:\s*(?P<message>.+?)'
    r'(?:\s+\[(?P<code>[^\]]+)\])?$'
)


@dataclass(frozen=True)
class Diagnostic:
    """One normalized static-analysis finding."""

    tool: str
    path: str
    line: int
    code: str
    message: str

    @property
    def signature(self) -> Tuple[str, str]:
        return (self.code, self.message)

    def format_line(self) -> str:
        return f'  {self.path}:{self.line}: [{self.tool}:{self.code}] {self.message}'


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git', *args],
        cwd=str(repo_root),
        capture_output=True,
        check=check,
    )


def resolve_base_commit(repo_root: Path, raw_base: str) -> Optional[str]:
    """Resolve the comparison base commit via merge-base when available."""
    candidate = raw_base.strip()
    if not candidate or set(candidate) == {'0'}:
        prev = _git(repo_root, 'rev-parse', '--verify', 'HEAD^', check=False)
        if prev.returncode != 0:
            return None
        candidate = prev.stdout.decode('utf-8').strip()
    exists = _git(repo_root, 'cat-file', '-e', f'{candidate}^{{commit}}', check=False)
    if exists.returncode != 0:
        prev = _git(repo_root, 'rev-parse', '--verify', 'HEAD^', check=False)
        if prev.returncode != 0:
            return None
        candidate = prev.stdout.decode('utf-8').strip()
    merged = _git(repo_root, 'merge-base', candidate, 'HEAD', check=False)
    if merged.returncode == 0:
        resolved = merged.stdout.decode('utf-8').strip()
        if resolved:
            return resolved
    return candidate


def changed_python_files(repo_root: Path, base: str) -> Dict[str, str]:
    """Return {posix_path: status} for added/modified Python files vs base."""
    proc = _git(
        repo_root,
        'diff', '--no-renames', '--name-status', '--diff-filter=ACMR',
        '-z', base, 'HEAD', '--', '*.py',
    )
    raw = proc.stdout.decode('utf-8')
    if not raw:
        return {}
    tokens = raw.rstrip('\0').split('\0')
    result: Dict[str, str] = {}
    idx = 0
    while idx + 1 < len(tokens):
        status = tokens[idx][:1]
        rel_path = tokens[idx + 1]
        idx += 2
        if any(rel_path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        if (repo_root / rel_path).is_file():
            result[rel_path] = status
    return result


def _parse_ruff_json(output: str, repo_root: Path, fallback_path: Optional[str] = None) -> List[Diagnostic]:
    if not output.strip():
        return []
    entries = json.loads(output)
    diagnostics: List[Diagnostic] = []
    for item in entries:
        filename = item.get('filename') or fallback_path or ''
        rel_path = _normalize_rel_path(filename, repo_root, fallback_path)
        location = item.get('location') or {}
        line = int(location.get('row', 1))
        code = str(item.get('code') or 'RUFF')
        message = str(item.get('message') or '').strip()
        diagnostics.append(Diagnostic('ruff', rel_path, line, code, message))
    return diagnostics


def _normalize_rel_path(raw_path: str, root: Path, fallback: Optional[str] = None) -> str:
    if not raw_path or raw_path == '-':
        return fallback or ''
    path = Path(raw_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return fallback or path.as_posix()
    return path.as_posix().removeprefix('./')


def run_ruff_head(
    repo_root: Path,
    files: Sequence[str],
    config_path: Path,
    ruff_bin: str,
) -> Dict[str, List[Diagnostic]]:
    """Run ruff on HEAD files and group findings by repository-relative path."""
    by_file: Dict[str, List[Diagnostic]] = {path: [] for path in files}
    if not files:
        return by_file
    proc = subprocess.run(
        [
            ruff_bin, 'check',
            '--config', str(config_path),
            '--output-format', 'json',
            '--no-cache',
            *files,
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f'ruff failed with exit code {proc.returncode}: {proc.stderr.strip()}'
        )
    for diag in _parse_ruff_json(proc.stdout, repo_root):
        if diag.path in by_file:
            by_file[diag.path].append(diag)
    return by_file


def run_ruff_base(
    repo_root: Path,
    base: str,
    files: Sequence[str],
    config_path: Path,
    ruff_bin: str,
) -> Dict[str, List[Diagnostic]]:
    """Run ruff against base-commit file contents via stdin."""
    by_file: Dict[str, List[Diagnostic]] = {path: [] for path in files}
    for rel_path in files:
        shown = _git(repo_root, 'show', f'{base}:{rel_path}', check=False)
        if shown.returncode != 0:
            continue
        proc = subprocess.run(
            [
                ruff_bin, 'check',
                '--config', str(config_path),
                '--output-format', 'json',
                '--no-cache',
                '--stdin-filename', rel_path,
                '-',
            ],
            cwd=str(repo_root),
            input=shown.stdout,
            capture_output=True,
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                f'ruff (base {rel_path}) failed with exit code {proc.returncode}: '
                f'{proc.stderr.decode("utf-8", errors="replace").strip()}'
            )
        decoded = proc.stdout.decode('utf-8', errors='replace')
        by_file[rel_path] = _parse_ruff_json(decoded, repo_root, fallback_path=rel_path)
    return by_file


def _parse_mypy_output(output: str, target_root: Path, allowed: Sequence[str]) -> Dict[str, List[Diagnostic]]:
    by_file: Dict[str, List[Diagnostic]] = {path: [] for path in allowed}
    for raw_line in output.splitlines():
        match = _MYPY_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        rel_path = _normalize_rel_path(match.group('file'), target_root)
        if rel_path not in by_file:
            continue
        by_file[rel_path].append(
            Diagnostic(
                tool='mypy',
                path=rel_path,
                line=int(match.group('line')),
                code=match.group('code') or 'mypy',
                message=match.group('message').strip(),
            )
        )
    return by_file


def _run_mypy(
    target_root: Path,
    files: Sequence[str],
    config_path: Path,
    mypy_bin: str,
    cache_dir: Path,
) -> Dict[str, List[Diagnostic]]:
    if not files:
        return {}
    mypy_paths = [
        str(target_root / rel)
        for rel in ('malbut_agent_server', 'homecam_agent/homecam_detector', 'malbut_autoslam')
        if (target_root / rel).is_dir()
    ]
    env = dict(os.environ)
    if mypy_paths:
        env['MYPYPATH'] = os.pathsep.join(mypy_paths)
    proc = subprocess.run(
        [
            mypy_bin,
            '--config-file', str(config_path),
            '--follow-imports=silent',
            '--no-error-summary',
            '--hide-error-context',
            '--show-column-numbers',
            '--show-error-codes',
            '--cache-dir', str(cache_dir),
            *files,
        ],
        cwd=str(target_root),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f'mypy failed with exit code {proc.returncode}: '
            f'{proc.stderr.strip() or proc.stdout.strip()}'
        )
    return _parse_mypy_output(proc.stdout, target_root, files)


def _extract_base_tree(repo_root: Path, base: str, destination: Path, files: Sequence[str]) -> List[str]:
    """Extract base-commit Python subtrees needed for mypy into destination."""
    existing_roots: List[str] = []
    for candidate in MYPY_ARCHIVE_PATHS:
        probe = _git(repo_root, 'cat-file', '-e', f'{base}:{candidate}', check=False)
        if probe.returncode == 0:
            existing_roots.append(candidate)
    if not existing_roots:
        return []
    archive = _git(repo_root, 'archive', '--format=tar', base, *existing_roots)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:') as tar:
        if 'filter' in inspect.signature(tar.extractall).parameters:
            tar.extractall(path=str(destination), filter='data')
        else:
            tar.extractall(path=str(destination))
    return [rel for rel in files if (destination / rel).is_file()]


def compare_ratchet(
    file_statuses: Dict[str, str],
    head_by_file: Dict[str, List[Diagnostic]],
    base_by_file: Dict[str, List[Diagnostic]],
) -> Tuple[List[Diagnostic], int]:
    """Return (net_new_regressions, total_fixed_count) using multiset ratchet."""
    regressions: List[Diagnostic] = []
    improved = 0
    for rel_path in sorted(file_statuses):
        head_diags = head_by_file.get(rel_path, [])
        if file_statuses[rel_path] == 'A':
            regressions.extend(head_diags)
            continue
        base_diags = base_by_file.get(rel_path, [])
        base_counts: Counter[Tuple[str, str]] = Counter(d.signature for d in base_diags)
        seen_in_head: Counter[Tuple[str, str]] = Counter()
        file_regressions: List[Diagnostic] = []
        for diag in sorted(head_diags, key=lambda d: (d.line, d.code, d.message)):
            seen_in_head[diag.signature] += 1
            if seen_in_head[diag.signature] > base_counts[diag.signature]:
                file_regressions.append(diag)
        regressions.extend(file_regressions)
        if not file_regressions and len(head_diags) < len(base_diags):
            improved += len(base_diags) - len(head_diags)
    return regressions, improved


def run_ratchet_checks(
    repo_root: Path,
    *,
    raw_base: str = '',
    ruff_bin: str = 'ruff',
    mypy_bin: str = 'mypy',
    skip_mypy: bool = False,
) -> Tuple[int, str]:
    """Run ruff + gradual mypy ratchet checks and return (exit_code, report)."""
    config_path = repo_root / 'pyproject.toml'
    if not config_path.is_file():
        return 2, f'Missing pyproject.toml at {config_path}'

    base = resolve_base_commit(repo_root, raw_base)
    if base is None:
        return 0, 'No base commit available for diff ratchet; skipping ratchet check.'

    changed = changed_python_files(repo_root, base)
    if not changed:
        return 0, 'Ratchet static checks: no eligible Python files changed.'

    lines: List[str] = [
        f'Ratchet static checks against {base[:12]} ({len(changed)} Python file(s))'
    ]
    all_regressions: List[Diagnostic] = []

    ruff_files = sorted(changed)
    modified_ruff = [path for path in ruff_files if changed[path] != 'A']
    ruff_head = run_ruff_head(repo_root, ruff_files, config_path, ruff_bin)
    ruff_base = run_ruff_base(repo_root, base, modified_ruff, config_path, ruff_bin)
    ruff_regressions, ruff_improved = compare_ratchet(changed, ruff_head, ruff_base)
    all_regressions.extend(ruff_regressions)
    lines.append(
        f'  ruff: checked {len(ruff_files)} file(s), '
        f'new={len(ruff_regressions)}, fixed={ruff_improved}'
    )

    if not skip_mypy:
        mypy_statuses = {
            path: status
            for path, status in changed.items()
            if any(path.startswith(prefix) for prefix in MYPY_INCLUDED_PREFIXES)
        }
        mypy_files = sorted(mypy_statuses)
        if mypy_files:
            with tempfile.TemporaryDirectory(prefix='malbut-ratchet-mypy-') as temp_dir:
                temp_root = Path(temp_dir)
                head_cache = temp_root / 'cache-head'
                base_cache = temp_root / 'cache-base'
                base_tree = temp_root / 'base-tree'
                base_tree.mkdir()
                mypy_head = _run_mypy(
                    repo_root, mypy_files, config_path, mypy_bin, head_cache,
                )
                modified_mypy = [
                    path for path in mypy_files if mypy_statuses[path] != 'A'
                ]
                mypy_base: Dict[str, List[Diagnostic]] = {}
                if modified_mypy:
                    available_base = _extract_base_tree(
                        repo_root, base, base_tree, modified_mypy,
                    )
                    if available_base:
                        mypy_base = _run_mypy(
                            base_tree, available_base, config_path, mypy_bin, base_cache,
                        )
                mypy_regressions, mypy_improved = compare_ratchet(
                    mypy_statuses, mypy_head, mypy_base,
                )
                all_regressions.extend(mypy_regressions)
                lines.append(
                    f'  mypy: checked {len(mypy_files)} runtime file(s), '
                    f'new={len(mypy_regressions)}, fixed={mypy_improved}'
                )
        else:
            lines.append('  mypy: no agent runtime Python files changed.')

    if all_regressions:
        lines.append('New static-analysis violations introduced in this change:')
        for diag in all_regressions:
            lines.append(diag.format_line())
        return 1, '\n'.join(lines)

    lines.append('Ratchet static checks passed with zero new violations.')
    return 0, '\n'.join(lines)


def _default_tool(name: str) -> str:
    sibling = Path(sys.executable).parent / name
    if sibling.is_file():
        return str(sibling)
    return name


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for CI and local pre-push verification."""
    parser = argparse.ArgumentParser(
        description='Run diff-aware ruff and gradual mypy ratchet checks.',
    )
    parser.add_argument(
        '--base',
        default=os.environ.get('BASE_SHA', ''),
        help='Base git commit SHA (defaults to BASE_SHA env var or HEAD^)',
    )
    parser.add_argument(
        '--repo-root',
        default='',
        help='Repository root (defaults to repository containing this script)',
    )
    parser.add_argument('--ruff-bin', default=_default_tool('ruff'), help='Path to ruff binary')
    parser.add_argument('--mypy-bin', default=_default_tool('mypy'), help='Path to mypy binary')
    parser.add_argument('--skip-mypy', action='store_true', help='Skip gradual mypy check')
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parents[2]
    )
    code, report = run_ratchet_checks(
        repo_root,
        raw_base=args.base,
        ruff_bin=args.ruff_bin,
        mypy_bin=args.mypy_bin,
        skip_mypy=args.skip_mypy,
    )
    stream = sys.stderr if code != 0 else sys.stdout
    print(report, file=stream)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
