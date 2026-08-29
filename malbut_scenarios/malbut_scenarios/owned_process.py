"""Own one bounded child-process session without retaining raw output."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time
from typing import Mapping, Optional, Sequence


_READ_CHUNK_BYTES = 64 * 1024
_MAX_OUTPUT_LIMIT = 64 * 1024 * 1024
_MAX_STOP_SECONDS = 60.0


class OwnedProcessError(RuntimeError):
    """Report one stable process-owner failure without private details."""

    _CODES = frozenset({
        'process_config_invalid',
        'process_start_failed',
        'process_identity_unavailable',
        'process_output_overflow',
        'process_cleanup_incomplete',
        'process_already_started',
    })

    def __init__(self, code: str) -> None:
        """Normalize failures to one public-safe process code."""
        normalized = (
            code if code in self._CODES else 'process_config_invalid'
        )
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True, slots=True)
class ProcessOutputEvidence:
    """Digest-only child output accounting."""

    bytes_observed: int
    bytes_hashed: int
    digest: str
    overflowed: bool


@dataclass(frozen=True, slots=True)
class ProcessCleanupEvidence:
    """Aggregate cleanup result without PID, argv, environment, or path."""

    process_started: bool
    remaining_process_count: int
    forced_termination_count: int
    output_collector_stopped: bool
    output_overflowed: bool
    cleanup_complete: bool


class _DigestingPipeReader:
    """Drain one pipe so a verbose child cannot block on its output."""

    def __init__(self, stream, maximum_bytes: int) -> None:
        self._stream = stream
        self._maximum_bytes = maximum_bytes
        self._digest = hashlib.sha256()
        self._bytes_observed = 0
        self._bytes_hashed = 0
        self._overflowed = False
        self._error: Optional[Exception] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name='malbut-owned-process-output',
            daemon=False,
        )

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def evidence(self) -> ProcessOutputEvidence:
        with self._lock:
            if self._error is not None:
                raise OwnedProcessError('process_identity_unavailable')
            return ProcessOutputEvidence(
                bytes_observed=self._bytes_observed,
                bytes_hashed=self._bytes_hashed,
                digest=self._digest.hexdigest(),
                overflowed=self._overflowed,
            )

    def _run(self) -> None:
        try:
            while True:
                read = getattr(self._stream, 'read1', self._stream.read)
                chunk = read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                with self._lock:
                    self._bytes_observed += len(chunk)
                    remaining = (
                        self._maximum_bytes - self._bytes_hashed
                    )
                    if remaining > 0:
                        selected = chunk[:remaining]
                        self._digest.update(selected)
                        self._bytes_hashed += len(selected)
                    if self._bytes_observed > self._maximum_bytes:
                        self._overflowed = True
        except Exception as error:  # noqa: B902 - thread boundary
            with self._lock:
                self._error = error
        finally:
            try:
                self._stream.close()
            except Exception as error:  # noqa: B902
                with self._lock:
                    if self._error is None:
                        self._error = error


class OwnedProcess:
    """Start and stop only the exact Linux session created by this owner."""

    def __init__(
        self,
        label: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        maximum_output_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        """Validate configuration without starting a process or thread."""
        if (
            type(label) is not str
            or not label
            or len(label) > 64
            or not label.replace('-', '').replace('_', '').isalnum()
        ):
            raise OwnedProcessError('process_config_invalid')
        if (
            not isinstance(argv, (tuple, list))
            or not argv
            or any(type(item) is not str or not item for item in argv)
        ):
            raise OwnedProcessError('process_config_invalid')
        if not isinstance(cwd, Path):
            raise OwnedProcessError('process_config_invalid')
        executable = Path(argv[0])
        if not executable.is_absolute() or executable.is_symlink():
            raise OwnedProcessError('process_config_invalid')
        try:
            executable_metadata = executable.stat()
            resolved_cwd = cwd.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as error:
            raise OwnedProcessError('process_config_invalid') from error
        if (
            not stat.S_ISREG(executable_metadata.st_mode)
            or not os.access(executable, os.X_OK)
            or not resolved_cwd.is_dir()
        ):
            raise OwnedProcessError('process_config_invalid')
        if not isinstance(environment, Mapping):
            raise OwnedProcessError('process_config_invalid')
        try:
            environment = dict(environment)
        except (TypeError, ValueError) as error:
            raise OwnedProcessError('process_config_invalid') from error
        if any(
            type(key) is not str
            or not key
            or '=' in key
            or '\x00' in key
            or type(value) is not str
            or '\x00' in value
            for key, value in environment.items()
        ):
            raise OwnedProcessError('process_config_invalid')
        if (
            isinstance(maximum_output_bytes, bool)
            or not isinstance(maximum_output_bytes, int)
            or not 4096 <= maximum_output_bytes <= _MAX_OUTPUT_LIMIT
        ):
            raise OwnedProcessError('process_config_invalid')
        self._label = label
        self._argv = tuple(argv)
        self._cwd = resolved_cwd
        self._environment = dict(environment)
        self._maximum_output_bytes = maximum_output_bytes
        self._process: Optional[subprocess.Popen] = None
        self._session_id: Optional[int] = None
        self._leader_start_ticks: Optional[int] = None
        self._reader: Optional[_DigestingPipeReader] = None
        self._cleanup: Optional[ProcessCleanupEvidence] = None

    def __repr__(self) -> str:
        """Return state without argv, environment, PID, or filesystem path."""
        return (
            f'OwnedProcess(label={self._label!r}, '
            f'started={self._process is not None!r})'
        )

    @property
    def started(self) -> bool:
        """Return whether this owner started its child session."""
        return self._process is not None

    @property
    def running(self) -> bool:
        """Return whether any process remains in the owned session."""
        process = self._process
        return process is not None and bool(self._owned_pids())

    @property
    def returncode(self) -> Optional[int]:
        """Return the owned leader's status when it has exited."""
        process = self._process
        return None if process is None else process.poll()

    def start(self) -> None:
        """Create one session leader using list argv and ``shell=False``."""
        if self._process is not None:
            raise OwnedProcessError('process_already_started')
        try:
            process = subprocess.Popen(
                list(self._argv),
                cwd=self._cwd,
                env=self._environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise OwnedProcessError('process_start_failed') from error
        if process.stdout is None:
            process.kill()
            process.wait(timeout=5)
            raise OwnedProcessError('process_start_failed')
        try:
            process_stat = _read_process_stat(process.pid)
            if (
                process_stat.process_group_id != process.pid
                or process_stat.session_id != process.pid
            ):
                raise OwnedProcessError('process_identity_unavailable')
        except Exception:
            try:
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            raise
        reader = _DigestingPipeReader(
            process.stdout,
            self._maximum_output_bytes,
        )
        self._process = process
        self._session_id = process.pid
        self._leader_start_ticks = process_stat.start_ticks
        self._reader = reader
        reader.start()

    def require_running(self) -> None:
        """Fail when the exact owned session exited unexpectedly."""
        if not self.running:
            raise OwnedProcessError('process_identity_unavailable')
        reader = self._reader
        if reader is not None and reader.evidence().overflowed:
            raise OwnedProcessError('process_output_overflow')

    def stop(
        self,
        *,
        interrupt_seconds: float = 20.0,
        terminate_seconds: float = 10.0,
        kill_seconds: float = 5.0,
    ) -> ProcessCleanupEvidence:
        """Stop the session once and return aggregate cleanup evidence."""
        if self._cleanup is not None:
            return self._cleanup
        waits = tuple(
            _bounded_seconds(value)
            for value in (
                interrupt_seconds,
                terminate_seconds,
                kill_seconds,
            )
        )
        if self._process is None:
            self._cleanup = ProcessCleanupEvidence(
                process_started=False,
                remaining_process_count=0,
                forced_termination_count=0,
                output_collector_stopped=True,
                output_overflowed=False,
                cleanup_complete=True,
            )
            return self._cleanup

        forced = 0
        pids = self._owned_pids()
        if pids:
            self._signal_owned(signal.SIGINT)
            pids = self._wait_absent(waits[0])
        if pids:
            forced += 1
            self._signal_owned(signal.SIGTERM)
            pids = self._wait_absent(waits[1])
        if pids:
            forced += 1
            self._signal_owned(signal.SIGKILL)
            pids = self._wait_absent(waits[2])
        try:
            self._process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        reader = self._reader
        reader_stopped = True
        output = ProcessOutputEvidence(
            0, 0, hashlib.sha256().hexdigest(), False
        )
        if reader is not None:
            reader_stopped = reader.join(timeout=5.0)
            if reader_stopped:
                output = reader.evidence()
        remaining = self._owned_pids()
        complete = not remaining and reader_stopped
        self._cleanup = ProcessCleanupEvidence(
            process_started=True,
            remaining_process_count=len(remaining),
            forced_termination_count=forced,
            output_collector_stopped=reader_stopped,
            output_overflowed=output.overflowed,
            cleanup_complete=complete and not output.overflowed,
        )
        return self._cleanup

    def output_evidence(self) -> ProcessOutputEvidence:
        """Return bounded digest-only output after the collector stops."""
        reader = self._reader
        if reader is None:
            return ProcessOutputEvidence(
                0, 0, hashlib.sha256().hexdigest(), False
            )
        return reader.evidence()

    def _owned_pids(self) -> tuple[int, ...]:
        session_id = self._session_id
        if session_id is None:
            return ()
        leader_start_ticks = self._leader_start_ticks
        if leader_start_ticks is None:
            raise OwnedProcessError('process_identity_unavailable')
        leader_uid = _process_uid(session_id)
        if leader_uid is not None:
            try:
                observed_leader = _read_process_stat(session_id)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                observed_leader = None
            if (
                observed_leader is not None
                and (
                    leader_uid != os.getuid()
                    or observed_leader.start_ticks != leader_start_ticks
                )
            ):
                # The numeric session leader PID has been recycled.  Never
                # signal a new process merely because it reused our old ID.
                raise OwnedProcessError('process_identity_unavailable')
        return _pids_in_session(session_id, os.getuid())

    def _signal_owned(self, selected_signal: signal.Signals) -> None:
        session_id = self._session_id
        if session_id is None:
            return
        if not self._owned_pids():
            return
        try:
            os.killpg(session_id, selected_signal)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise OwnedProcessError('process_cleanup_incomplete') from error
        for pid in self._owned_pids():
            try:
                os.kill(pid, selected_signal)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                raise OwnedProcessError(
                    'process_cleanup_incomplete'
                ) from error

    def _wait_absent(self, timeout: float) -> tuple[int, ...]:
        deadline = time.monotonic() + timeout
        while True:
            # Reap the owned leader promptly. A terminated-but-unreaped
            # leader remains visible in /proc and would otherwise look like
            # a live session member until every escalation deadline elapsed.
            if self._process is not None:
                self._process.poll()
            remaining = self._owned_pids()
            if not remaining:
                return ()
            if time.monotonic() >= deadline:
                return remaining
            time.sleep(0.05)


@dataclass(frozen=True, slots=True)
class _ProcessStat:
    process_group_id: int
    session_id: int
    start_ticks: int


def _read_process_stat(pid: int) -> _ProcessStat:
    path = Path('/proc') / str(pid) / 'stat'
    payload = path.read_bytes()
    if not 1 <= len(payload) <= 4096:
        raise OwnedProcessError('process_identity_unavailable')
    closing = payload.rfind(b')')
    if closing < 2:
        raise OwnedProcessError('process_identity_unavailable')
    fields = payload[closing + 2:].split()
    try:
        return _ProcessStat(
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            start_ticks=int(fields[19]),
        )
    except (IndexError, TypeError, ValueError) as error:
        raise OwnedProcessError('process_identity_unavailable') from error


def _process_uid(pid: int) -> Optional[int]:
    try:
        return (Path('/proc') / str(pid)).stat().st_uid
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as error:
        raise OwnedProcessError('process_identity_unavailable') from error


def _pids_in_session(session_id: int, uid: int) -> tuple[int, ...]:
    result = []
    try:
        entries = tuple(Path('/proc').iterdir())
    except OSError as error:
        raise OwnedProcessError('process_identity_unavailable') from error
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if _process_uid(pid) != uid:
            continue
        try:
            observed = _read_process_stat(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except OwnedProcessError:
            continue
        if observed.session_id == session_id:
            result.append(pid)
    return tuple(sorted(result))


def _bounded_seconds(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= _MAX_STOP_SECONDS
    ):
        raise ValueError('process timeout is invalid')
    return float(value)


__all__ = [
    'OwnedProcess',
    'OwnedProcessError',
    'ProcessCleanupEvidence',
    'ProcessOutputEvidence',
]
