"""Strict, content-free attestation of clean source and installed files."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import stat
import subprocess
import time
from typing import Dict, Sequence, Tuple


_GIT_EXECUTABLE = '/usr/bin/git'
_GIT_TIMEOUT_SECONDS = 5.0
_GIT_MAXIMUM_OUTPUT_BYTES = 64 * 1024
_READ_SIZE = 64 * 1024
_OBJECT_ID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_ATTESTATION_DOMAIN = b'malbut.source-install-attestation.v1\0'


class SourceInstallAttestationError(RuntimeError):
    """Fail-closed error carrying only one stable, content-free code."""

    __slots__ = ('code',)

    def __init__(self, code: str) -> None:
        """Store only the stable public error code."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SourceInstallAttestation:
    """Content-free identity of the source commit and Git tree."""

    commit: str
    tree_digest: str

    def __post_init__(self) -> None:
        """Keep even directly-created receipts content-free and bounded."""
        if not isinstance(self.commit, str) or not _OBJECT_ID.fullmatch(
            self.commit
        ):
            raise SourceInstallAttestationError('attestation_invalid')
        if not isinstance(self.tree_digest, str) or not _SHA256.fullmatch(
            self.tree_digest
        ):
            raise SourceInstallAttestationError('attestation_invalid')


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class _RepositoryIdentity:
    commit: str
    tree: str


@dataclass(frozen=True, slots=True)
class _Binding:
    relative: str
    source: Path
    installed: Path


def _stop_process(process: subprocess.Popen) -> None:
    try:
        process_id = int(process.pid)
        if process_id > 0:
            os.killpg(process_id, signal.SIGKILL)
    except (AttributeError, OSError, TypeError, ValueError):
        try:
            process.kill()
        except (AttributeError, OSError):
            pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _read_process_output(
    process: subprocess.Popen,
) -> Tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        _stop_process(process)
        raise SourceInstallAttestationError('git_unavailable')

    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    observed = 0
    try:
        outputs: Dict[int, bytearray] = {
            process.stdout.fileno(): bytearray(),
            process.stderr.fileno(): bytearray(),
        }
        for descriptor in outputs:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                _stop_process(process)
                raise SourceInstallAttestationError('git_timeout')
            events = selector.select(timeout=min(0.05, remaining))
            for key, _mask in events:
                try:
                    chunk = os.read(key.fd, _READ_SIZE)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                observed += len(chunk)
                if observed > _GIT_MAXIMUM_OUTPUT_BYTES:
                    _stop_process(process)
                    raise SourceInstallAttestationError(
                        'git_output_exceeded'
                    )
                outputs[key.fd].extend(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            _stop_process(process)
            raise SourceInstallAttestationError('git_timeout')
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _stop_process(process)
            raise SourceInstallAttestationError('git_timeout') from None
        return (
            bytes(outputs[process.stdout.fileno()]),
            bytes(outputs[process.stderr.fileno()]),
        )
    except SourceInstallAttestationError:
        raise
    except (OSError, TypeError, ValueError):
        _stop_process(process)
        raise SourceInstallAttestationError('git_unavailable') from None
    finally:
        selector.close()


def _run_git(source_root: Path, arguments: Sequence[str]) -> _GitResult:
    environment = {
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_OPTIONAL_LOCKS': '0',
        'GIT_TERMINAL_PROMPT': '0',
        'LANG': 'C',
        'LC_ALL': 'C',
    }
    original_home = os.environ.get('HOME')
    if original_home is not None:
        environment['HOME'] = original_home
    argv = [
        _GIT_EXECUTABLE,
        '-C',
        os.fspath(source_root),
        '-c',
        'core.fsmonitor=false',
        '-c',
        'core.untrackedCache=false',
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
            env=environment,
        )
    except (OSError, ValueError):
        raise SourceInstallAttestationError('git_unavailable') from None

    try:
        stdout, stderr = _read_process_output(process)
        return _GitResult(
            returncode=int(process.returncode),
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _git_stdout(
    source_root: Path,
    arguments: Sequence[str],
    *,
    failure_code: str = 'git_failed',
) -> bytes:
    result = _run_git(source_root, arguments)
    if result.returncode != 0:
        raise SourceInstallAttestationError(failure_code)
    if result.stderr:
        raise SourceInstallAttestationError('git_output_invalid')
    return result.stdout


def _single_line(value: bytes) -> str:
    if not value.endswith(b'\n') or value.count(b'\n') != 1:
        raise SourceInstallAttestationError('git_output_invalid')
    try:
        decoded = value[:-1].decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        raise SourceInstallAttestationError('git_output_invalid') from None
    if not decoded or '\x00' in decoded or '\r' in decoded:
        raise SourceInstallAttestationError('git_output_invalid')
    return decoded


def _require_source_root(source_root: object) -> Path:
    if not isinstance(source_root, Path) or not source_root.is_absolute():
        raise SourceInstallAttestationError('source_root_invalid')
    try:
        if source_root.resolve(strict=True) != source_root:
            raise SourceInstallAttestationError('source_root_invalid')
        source_status = os.lstat(source_root)
    except (OSError, RuntimeError):
        raise SourceInstallAttestationError('source_root_invalid') from None
    if not stat.S_ISDIR(source_status.st_mode):
        raise SourceInstallAttestationError('source_root_invalid')
    return source_root


def _repository_identity(source_root: Path) -> _RepositoryIdentity:
    top_level = _single_line(_git_stdout(
        source_root,
        ('rev-parse', '--show-toplevel'),
    ))
    if top_level != os.fspath(source_root):
        raise SourceInstallAttestationError('git_toplevel_mismatch')

    commit = _single_line(_git_stdout(
        source_root,
        ('rev-parse', '--verify', 'HEAD'),
    ))
    tree = _single_line(_git_stdout(
        source_root,
        ('rev-parse', '--verify', 'HEAD^{tree}'),
    ))
    if not _OBJECT_ID.fullmatch(commit) or not _OBJECT_ID.fullmatch(tree):
        raise SourceInstallAttestationError('git_output_invalid')
    return _RepositoryIdentity(commit=commit, tree=tree)


def _require_clean(source_root: Path, *, changed: bool = False) -> None:
    status_output = _git_stdout(
        source_root,
        (
            'status',
            '--porcelain=v1',
            '--untracked-files=all',
            '--ignore-submodules=none',
            '-z',
        ),
    )
    if status_output:
        code = 'source_changed' if changed else 'git_dirty'
        raise SourceInstallAttestationError(code)


def _canonical_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SourceInstallAttestationError('binding_invalid')
    if (
        len(value.encode('utf-8', errors='surrogatepass')) > 4096
        or '\x00' in value
        or '\\' in value
        or any(ord(character) < 32 for character in value)
    ):
        raise SourceInstallAttestationError('binding_invalid')
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or not relative.parts
        or any(part in ('.', '..') for part in relative.parts)
    ):
        raise SourceInstallAttestationError('binding_invalid')
    return value


def _canonical_installed_path(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise SourceInstallAttestationError('binding_invalid')
    try:
        if value.resolve(strict=True) != value:
            raise SourceInstallAttestationError('installed_file_invalid')
    except (OSError, RuntimeError):
        raise SourceInstallAttestationError(
            'installed_file_invalid'
        ) from None
    return value


def _bindings(
    source_root: Path,
    bindings: object,
) -> Tuple[_Binding, ...]:
    if not isinstance(bindings, Mapping):
        raise SourceInstallAttestationError('bindings_invalid')
    try:
        items = list(bindings.items())
    except Exception:
        raise SourceInstallAttestationError('bindings_invalid') from None
    if not items:
        raise SourceInstallAttestationError('bindings_invalid')

    normalized = []
    installed_paths = set()
    for relative_value, installed_value in items:
        relative = _canonical_relative_path(relative_value)
        installed = _canonical_installed_path(installed_value)
        source = source_root.joinpath(*PurePosixPath(relative).parts)
        try:
            source.relative_to(source_root)
        except ValueError:
            raise SourceInstallAttestationError('binding_invalid') from None
        if installed in installed_paths:
            raise SourceInstallAttestationError('binding_invalid')
        installed_paths.add(installed)
        normalized.append(_Binding(
            relative=relative,
            source=source,
            installed=installed,
        ))
    return tuple(sorted(normalized, key=lambda binding: binding.relative))


def _require_tracked(source_root: Path, binding: _Binding) -> None:
    output = _git_stdout(
        source_root,
        ('ls-files', '-z', '--error-unmatch', '--', binding.relative),
        failure_code='binding_untracked',
    )
    try:
        expected = os.fsencode(binding.relative) + b'\x00'
    except UnicodeEncodeError:
        raise SourceInstallAttestationError('binding_invalid') from None
    if output != expected:
        raise SourceInstallAttestationError('git_output_invalid')


def _open_regular(path: Path, failure_code: str) -> Tuple[int, os.stat_result]:
    try:
        if path.resolve(strict=True) != path:
            raise SourceInstallAttestationError(failure_code)
        path_status = os.lstat(path)
        if not stat.S_ISREG(path_status.st_mode):
            raise SourceInstallAttestationError(failure_code)
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        descriptor_status = os.fstat(descriptor)
    except SourceInstallAttestationError:
        raise
    except OSError:
        raise SourceInstallAttestationError(failure_code) from None
    if (
        not stat.S_ISREG(descriptor_status.st_mode)
        or descriptor_status.st_dev != path_status.st_dev
        or descriptor_status.st_ino != path_status.st_ino
    ):
        os.close(descriptor)
        raise SourceInstallAttestationError(failure_code)
    return descriptor, descriptor_status


def _status_identity(status_value: os.stat_result) -> Tuple[int, ...]:
    return (
        status_value.st_dev,
        status_value.st_ino,
        status_value.st_mode,
        status_value.st_size,
        status_value.st_mtime_ns,
        status_value.st_ctime_ns,
    )


def _require_unchanged(
    descriptor: int,
    path: Path,
    initial: os.stat_result,
    failure_code: str,
) -> None:
    try:
        descriptor_status = os.fstat(descriptor)
        path_status = os.lstat(path)
    except OSError:
        raise SourceInstallAttestationError(failure_code) from None
    expected = _status_identity(initial)
    if (
        _status_identity(descriptor_status) != expected
        or _status_identity(path_status) != expected
    ):
        raise SourceInstallAttestationError(failure_code)


def _require_byte_identical(binding: _Binding) -> None:
    source_descriptor = None
    installed_descriptor = None
    try:
        source_descriptor, source_status = _open_regular(
            binding.source,
            'source_file_invalid',
        )
        installed_descriptor, installed_status = _open_regular(
            binding.installed,
            'installed_file_invalid',
        )
        try:
            while True:
                source_chunk = os.read(source_descriptor, _READ_SIZE)
                installed_chunk = os.read(installed_descriptor, _READ_SIZE)
                if source_chunk != installed_chunk:
                    raise SourceInstallAttestationError(
                        'artifact_mismatch'
                    )
                if not source_chunk:
                    break
        except SourceInstallAttestationError:
            raise
        except OSError:
            raise SourceInstallAttestationError(
                'artifact_read_failed'
            ) from None
        _require_unchanged(
            source_descriptor,
            binding.source,
            source_status,
            'source_changed',
        )
        _require_unchanged(
            installed_descriptor,
            binding.installed,
            installed_status,
            'installed_changed',
        )
    finally:
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        if installed_descriptor is not None:
            try:
                os.close(installed_descriptor)
            except OSError:
                pass


def _tree_digest(commit: str, tree: str) -> str:
    digest = hashlib.sha256()
    digest.update(_ATTESTATION_DOMAIN)
    digest.update(commit.encode('ascii'))
    digest.update(b'\0')
    digest.update(tree.encode('ascii'))
    return digest.hexdigest()


def attest_source_install(
    source_root: Path,
    expected_commit: str,
    bindings: Mapping[str, Path],
) -> SourceInstallAttestation:
    """
    Attest a clean exact commit and byte-identical installed artifacts.

    No path, file content, Git output, or installed identifier is retained in
    the returned receipt or in raised exceptions.
    """
    root = _require_source_root(source_root)
    if not isinstance(expected_commit, str) or not _OBJECT_ID.fullmatch(
        expected_commit
    ):
        raise SourceInstallAttestationError('expected_commit_invalid')
    selected_bindings = _bindings(root, bindings)

    before = _repository_identity(root)
    if before.commit != expected_commit:
        raise SourceInstallAttestationError('git_head_mismatch')
    for binding in selected_bindings:
        _require_tracked(root, binding)
    _require_clean(root)
    for binding in selected_bindings:
        _require_byte_identical(binding)

    after = _repository_identity(root)
    if after != before or after.commit != expected_commit:
        raise SourceInstallAttestationError('source_changed')
    _require_clean(root, changed=True)
    return SourceInstallAttestation(
        commit=before.commit,
        tree_digest=_tree_digest(before.commit, before.tree),
    )
