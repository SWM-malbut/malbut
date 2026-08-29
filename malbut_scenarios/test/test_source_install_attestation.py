"""Fail-closed contracts for source/install attestation."""

from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from malbut_scenarios import source_install_attestation as attestation_module
from malbut_scenarios.source_install_attestation import (
    SourceInstallAttestation,
    SourceInstallAttestationError,
    attest_source_install,
)


_GIT = '/usr/bin/git'


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [_GIT, '-C', str(root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        timeout=5,
        shell=False,
        env={
            **os.environ,
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_NOSYSTEM': '1',
            'LANG': 'C',
            'LC_ALL': 'C',
        },
    )
    assert result.stderr == b''
    return result.stdout.decode('utf-8').strip()


def _repository(tmp_path: Path, content: bytes = b'installed bytes\n'):
    source_root = tmp_path / 'source'
    source_file = source_root / 'package' / 'module.py'
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(content)
    _git(source_root.parent, 'init', '-q', str(source_root))
    _git(source_root, 'add', '--', 'package/module.py')
    _git(
        source_root,
        '-c',
        'user.name=Malbut Test',
        '-c',
        'user.email=malbut@example.invalid',
        'commit',
        '-q',
        '-m',
        'fixture',
    )

    installed_file = tmp_path / 'installed' / 'module.py'
    installed_file.parent.mkdir()
    installed_file.write_bytes(content)
    return (
        source_root,
        source_file,
        installed_file,
        _git(source_root, 'rev-parse', '--verify', 'HEAD'),
        _git(source_root, 'rev-parse', '--verify', 'HEAD^{tree}'),
    )


def _attest(fixture):
    source_root, _source_file, installed_file, commit, _tree = fixture
    return attest_source_install(
        source_root,
        commit,
        {'package/module.py': installed_file},
    )


def _assert_code(code, callable_value) -> None:
    with pytest.raises(SourceInstallAttestationError) as caught:
        callable_value()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_clean_exact_source_and_install_return_content_free_attestation(
    tmp_path,
) -> None:
    """A clean exact binding yields only commit and tree digest."""
    fixture = _repository(tmp_path)
    source_root, source_file, installed_file, commit, tree = fixture
    source_before = source_file.stat()
    installed_before = installed_file.stat()

    receipt = _attest(fixture)

    expected_digest = hashlib.sha256(
        b'malbut.source-install-attestation.v1\0'
        + commit.encode('ascii')
        + b'\0'
        + tree.encode('ascii')
    ).hexdigest()
    assert receipt == SourceInstallAttestation(
        commit=commit,
        tree_digest=expected_digest,
    )
    assert set(receipt.__dataclass_fields__) == {'commit', 'tree_digest'}
    assert not hasattr(receipt, '__dict__')
    assert source_file.stat() == source_before
    assert installed_file.stat() == installed_before
    assert _git(source_root, 'status', '--porcelain=v1', '-z') == ''
    rendered = repr(receipt)
    assert str(source_root) not in rendered
    assert str(installed_file) not in rendered
    assert 'installed bytes' not in rendered
    with pytest.raises(FrozenInstanceError):
        receipt.commit = '0' * 40


def test_digest_is_git_tree_bound_and_stable_across_install_locations(
    tmp_path,
) -> None:
    """Install paths cannot influence the content-free source identity."""
    fixture = _repository(tmp_path)
    source_root, _source_file, installed_file, commit, _tree = fixture
    second_install = tmp_path / 'other-install' / 'copied.py'
    second_install.parent.mkdir()
    shutil.copyfile(installed_file, second_install)

    first = attest_source_install(
        source_root,
        commit,
        {'package/module.py': installed_file},
    )
    second = attest_source_install(
        source_root,
        commit,
        {'package/module.py': second_install},
    )

    assert first == second


@pytest.mark.parametrize('expected', (
    'f' * 40,
    '0' * 64,
))
def test_arbitrary_full_commit_cannot_attest_another_head(
    tmp_path,
    expected,
) -> None:
    """A syntactically valid but different commit is not accepted."""
    fixture = _repository(tmp_path)
    source_root, _source_file, installed_file, _commit, _tree = fixture

    _assert_code(
        'git_head_mismatch',
        lambda: attest_source_install(
            source_root,
            expected,
            {'package/module.py': installed_file},
        ),
    )


@pytest.mark.parametrize('expected', (
    '',
    'A' * 40,
    'a' * 39,
    '--help',
    'a' * 40 + '\nprivate',
    42,
))
def test_expected_commit_must_be_full_lowercase_object_id(
    tmp_path,
    expected,
) -> None:
    """Only a complete lowercase object identifier enters comparison."""
    fixture = _repository(tmp_path)
    source_root, _source_file, installed_file, _commit, _tree = fixture

    _assert_code(
        'expected_commit_invalid',
        lambda: attest_source_install(
            source_root,
            expected,
            {'package/module.py': installed_file},
        ),
    )


@pytest.mark.parametrize('change_kind', ('tracked', 'untracked'))
def test_dirty_or_untracked_source_is_rejected(tmp_path, change_kind) -> None:
    """Tracked changes and untracked content both invalidate cleanliness."""
    fixture = _repository(tmp_path)
    source_root, source_file, _installed_file, _commit, _tree = fixture
    if change_kind == 'tracked':
        source_file.write_bytes(b'dirty\n')
    else:
        (source_root / 'untracked-private.txt').write_text(
            'private',
            encoding='utf-8',
        )

    _assert_code('git_dirty', lambda: _attest(fixture))


def test_untracked_binding_is_rejected_before_dirty_status(tmp_path) -> None:
    """An explicit untracked binding receives the stable binding code."""
    fixture = _repository(tmp_path)
    source_root, _source_file, installed_file, commit, _tree = fixture
    untracked = source_root / 'package' / 'not-tracked.py'
    untracked.write_bytes(installed_file.read_bytes())

    _assert_code(
        'binding_untracked',
        lambda: attest_source_install(
            source_root,
            commit,
            {'package/not-tracked.py': installed_file},
        ),
    )


def test_prefix_of_tracked_name_does_not_count_as_tracked(tmp_path) -> None:
    """Git tracking requires an exact NUL-delimited path match."""
    fixture = _repository(tmp_path)
    source_root, source_file, installed_file, commit, _tree = fixture
    shorter = source_root / 'package' / 'module'
    source_file.rename(source_root / 'package' / 'module.py.long')
    _git(source_root, 'add', '-A')
    _git(
        source_root,
        '-c',
        'user.name=Malbut Test',
        '-c',
        'user.email=malbut@example.invalid',
        'commit',
        '-q',
        '-m',
        'rename fixture',
    )
    shorter.write_bytes(installed_file.read_bytes())
    new_commit = _git(source_root, 'rev-parse', '--verify', 'HEAD')

    _assert_code(
        'binding_untracked',
        lambda: attest_source_install(
            source_root,
            new_commit,
            {'package/module': installed_file},
        ),
    )


def test_one_byte_installed_artifact_mismatch_is_rejected(tmp_path) -> None:
    """A mismatch beyond the first read chunk still fails closed."""
    content = b'a' * (64 * 1024 + 17)
    fixture = _repository(tmp_path, content)
    installed_file = fixture[2]
    installed_file.write_bytes(content[:-1] + b'b')

    _assert_code('artifact_mismatch', lambda: _attest(fixture))


def test_truncated_installed_artifact_is_rejected(tmp_path) -> None:
    """An installed prefix is not considered byte-identical."""
    fixture = _repository(tmp_path)
    installed_file = fixture[2]
    installed_file.write_bytes(b'installed')
    _assert_code('artifact_mismatch', lambda: _attest(fixture))


def test_tracked_source_symlink_is_rejected(tmp_path) -> None:
    """Git-tracked symlinks cannot serve as attested source artifacts."""
    source_root = tmp_path / 'source'
    source_root.mkdir()
    external = tmp_path / 'external.py'
    external.write_bytes(b'bytes\n')
    source_file = source_root / 'module.py'
    source_file.symlink_to(external)
    _git(source_root, 'init', '-q')
    _git(source_root, 'add', '--', 'module.py')
    _git(
        source_root,
        '-c',
        'user.name=Malbut Test',
        '-c',
        'user.email=malbut@example.invalid',
        'commit',
        '-q',
        '-m',
        'symlink fixture',
    )
    installed = tmp_path / 'installed.py'
    installed.write_bytes(external.read_bytes())
    commit = _git(source_root, 'rev-parse', '--verify', 'HEAD')

    _assert_code(
        'source_file_invalid',
        lambda: attest_source_install(
            source_root,
            commit,
            {'module.py': installed},
        ),
    )


def test_installed_symlink_and_nonregular_file_are_rejected(tmp_path) -> None:
    """Installed artifacts must be canonical regular files."""
    first_root = tmp_path / 'first'
    first_root.mkdir()
    fixture = _repository(first_root)
    source_root, _source_file, installed_file, commit, _tree = fixture
    linked = tmp_path / 'installed-link.py'
    linked.symlink_to(installed_file)
    _assert_code(
        'installed_file_invalid',
        lambda: attest_source_install(
            source_root,
            commit,
            {'package/module.py': linked},
        ),
    )

    directory = tmp_path / 'installed-directory'
    directory.mkdir()
    _assert_code(
        'installed_file_invalid',
        lambda: attest_source_install(
            source_root,
            commit,
            {'package/module.py': directory},
        ),
    )


def test_source_root_symlink_and_nested_repo_path_are_rejected(
    tmp_path,
) -> None:
    """The caller must select the exact canonical repository root."""
    fixture = _repository(tmp_path)
    source_root, _source_file, installed_file, commit, _tree = fixture
    linked_root = tmp_path / 'linked-source'
    linked_root.symlink_to(source_root, target_is_directory=True)
    _assert_code(
        'source_root_invalid',
        lambda: attest_source_install(
            linked_root,
            commit,
            {'package/module.py': installed_file},
        ),
    )

    _assert_code(
        'git_toplevel_mismatch',
        lambda: attest_source_install(
            source_root / 'package',
            commit,
            {'module.py': installed_file},
        ),
    )


@pytest.mark.parametrize('relative', (
    '../outside.py',
    '/absolute.py',
    'package//module.py',
    'package/./module.py',
    'package\\module.py',
    'package/new\nline.py',
    '',
))
def test_noncanonical_or_out_of_root_binding_is_rejected(
    tmp_path,
    relative,
) -> None:
    """Source keys cannot escape or ambiguously name repository files."""
    fixture = _repository(tmp_path)
    source_root, _source_file, installed_file, commit, _tree = fixture
    _assert_code(
        'binding_invalid',
        lambda: attest_source_install(
            source_root,
            commit,
            {relative: installed_file},
        ),
    )


@pytest.mark.parametrize('bindings', (
    {},
    [],
    {'package/module.py': 'relative-installed.py'},
    {'package/module.py': Path('relative-installed.py')},
))
def test_bindings_require_nonempty_mapping_and_absolute_paths(
    tmp_path,
    bindings,
) -> None:
    """At least one explicit source-to-absolute-install binding is needed."""
    fixture = _repository(tmp_path)
    source_root, _source_file, _installed_file, commit, _tree = fixture
    expected = 'bindings_invalid' if not bindings else 'binding_invalid'
    _assert_code(
        expected,
        lambda: attest_source_install(source_root, commit, bindings),
    )


def test_same_installed_file_cannot_claim_two_source_bindings(
    tmp_path,
) -> None:
    """One installed file cannot stand in for two tracked sources."""
    fixture = _repository(tmp_path)
    source_root, source_file, installed_file, _commit, _tree = fixture
    second_source = source_root / 'package' / 'second.py'
    shutil.copyfile(source_file, second_source)
    _git(source_root, 'add', '--', 'package/second.py')
    _git(
        source_root,
        '-c',
        'user.name=Malbut Test',
        '-c',
        'user.email=malbut@example.invalid',
        'commit',
        '-q',
        '-m',
        'second fixture',
    )
    commit = _git(source_root, 'rev-parse', '--verify', 'HEAD')

    _assert_code(
        'binding_invalid',
        lambda: attest_source_install(
            source_root,
            commit,
            {
                'package/module.py': installed_file,
                'package/second.py': installed_file,
            },
        ),
    )


def test_git_process_is_fixed_argv_without_shell(
    tmp_path,
    monkeypatch,
) -> None:
    """Every Git query uses the fixed executable and a list argv."""
    fixture = _repository(tmp_path)
    real_popen = subprocess.Popen
    calls = []

    def recording_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(
        attestation_module.subprocess,
        'Popen',
        recording_popen,
    )
    _attest(fixture)

    assert calls
    for argv, kwargs in calls:
        assert isinstance(argv, list)
        assert argv[0] == '/usr/bin/git'
        assert argv[1] == '-C'
        assert kwargs['shell'] is False
        assert kwargs['stdin'] is subprocess.DEVNULL
        assert kwargs['start_new_session'] is True


def test_git_timeout_is_bounded_and_content_free(
    tmp_path,
    monkeypatch,
) -> None:
    """A stalled Git process is killed and reported by stable code."""
    fixture = _repository(tmp_path)
    real_popen = subprocess.Popen

    def stalled_popen(_argv, **kwargs):
        return real_popen(
            [sys.executable, '-c', 'import time; time.sleep(10)'],
            **kwargs,
        )

    monkeypatch.setattr(attestation_module.subprocess, 'Popen', stalled_popen)
    monkeypatch.setattr(attestation_module, '_GIT_TIMEOUT_SECONDS', 0.05)
    _assert_code('git_timeout', lambda: _attest(fixture))


def test_git_output_is_bounded_before_process_exit(
    tmp_path,
    monkeypatch,
) -> None:
    """Excess output aborts before a noisy process reaches EOF."""
    fixture = _repository(tmp_path)
    real_popen = subprocess.Popen

    def noisy_popen(_argv, **kwargs):
        return real_popen(
            [
                sys.executable,
                '-c',
                'import os,time; os.write(1,b"x"*70000); time.sleep(10)',
            ],
            **kwargs,
        )

    monkeypatch.setattr(attestation_module.subprocess, 'Popen', noisy_popen)
    _assert_code('git_output_exceeded', lambda: _attest(fixture))


def test_malformed_or_non_utf8_git_output_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    """Repository identity output must have the exact UTF-8 shape."""
    fixture = _repository(tmp_path)
    real_popen = subprocess.Popen

    def invalid_popen(_argv, **kwargs):
        return real_popen(
            [sys.executable, '-c', 'import os; os.write(1,b"\\xff\\n")'],
            **kwargs,
        )

    monkeypatch.setattr(attestation_module.subprocess, 'Popen', invalid_popen)
    _assert_code('git_output_invalid', lambda: _attest(fixture))


def test_nonzero_git_result_exposes_no_stderr_or_path(
    tmp_path,
    monkeypatch,
) -> None:
    """Git stderr and filesystem identities never enter public errors."""
    fixture = _repository(tmp_path)
    private_value = str(tmp_path / 'private-secret-path')
    real_popen = subprocess.Popen

    def failed_popen(_argv, **kwargs):
        return real_popen(
            [
                sys.executable,
                '-c',
                'import sys; '
                'sys.stderr.write(sys.argv[1]); '
                'raise SystemExit(7)',
                private_value,
            ],
            **kwargs,
        )

    monkeypatch.setattr(attestation_module.subprocess, 'Popen', failed_popen)
    with pytest.raises(SourceInstallAttestationError) as caught:
        _attest(fixture)
    assert caught.value.code == 'git_failed'
    assert private_value not in str(caught.value)
    assert private_value not in repr(caught.value)


def test_source_change_during_attestation_is_detected(
    tmp_path,
    monkeypatch,
) -> None:
    """The final repository check closes the comparison race window."""
    fixture = _repository(tmp_path)
    source_file = fixture[1]
    original_compare = attestation_module._require_byte_identical

    def compare_then_change(binding):
        original_compare(binding)
        source_file.write_bytes(b'changed after comparison\n')

    monkeypatch.setattr(
        attestation_module,
        '_require_byte_identical',
        compare_then_change,
    )
    _assert_code('source_changed', lambda: _attest(fixture))


def test_direct_receipt_construction_rejects_non_digest_content() -> None:
    """Direct receipts cannot carry arbitrary text in identity fields."""
    for commit, digest in (
        ('private-commit', '0' * 64),
        ('a' * 40, 'private-tree'),
        ('A' * 40, '0' * 64),
    ):
        _assert_code(
            'attestation_invalid',
            lambda commit=commit, digest=digest: SourceInstallAttestation(
                commit=commit,
                tree_digest=digest,
            ),
        )
