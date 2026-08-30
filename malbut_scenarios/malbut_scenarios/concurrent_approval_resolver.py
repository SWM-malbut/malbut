"""Bounded target-resolution gate for concurrent-approval evidence."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any

from malbut_agent_server.named_target import BoundNamedTarget


_CONTENDER_COUNT = 2
_MAX_TIMEOUT_SECONDS = 30.0
CONCURRENT_APPROVAL_OBSERVATION_FILENAME = (
    'swm25-136-concurrent-approval.json'
)
_OBSERVATION_BYTES_LIMIT = 512
_OBSERVATION_KEYS = frozenset({
    'contender_count',
    'fault_profile',
    'release_count',
})


class ConcurrentApprovalGateError(RuntimeError):
    """Report that the bounded approval rendezvous failed closed."""


@dataclass(frozen=True, slots=True)
class ConcurrentApprovalGateSnapshot:
    """Expose only aggregate pressure counts for acceptance evidence."""

    contender_count: int
    release_count: int


@dataclass(frozen=True, slots=True)
class ConcurrentApprovalGateObservation:
    """Content-free proof that both server-side contenders rendezvoused."""

    fault_profile: str = 'concurrent_approval'
    contender_count: int = _CONTENDER_COUNT
    release_count: int = _CONTENDER_COUNT

    def __post_init__(self) -> None:
        """Accept only the exact bounded concurrent-approval result."""
        if (
            self.fault_profile != 'concurrent_approval'
            or type(self.contender_count) is not int
            or type(self.release_count) is not int
            or self.contender_count != _CONTENDER_COUNT
            or self.release_count != _CONTENDER_COUNT
        ):
            raise ValueError('concurrent approval observation is invalid')

    def as_dict(self) -> dict[str, object]:
        """Return the fixed public-safe JSON projection."""
        return {
            'contender_count': self.contender_count,
            'fault_profile': self.fault_profile,
            'release_count': self.release_count,
        }


def concurrent_approval_observation_path(database_path: str) -> Path:
    """Return the fixed private observation beside the fresh runtime DB."""
    if type(database_path) is not str or not database_path.strip():
        raise ValueError('database_path is invalid')
    if database_path == ':memory:':
        raise ValueError(
            'concurrent approval evidence requires a durable database'
        )
    try:
        database = Path(database_path).expanduser().resolve(strict=False)
        parent = database.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ConcurrentApprovalGateError(
            'concurrent approval observation is invalid'
        ) from None
    return parent / CONCURRENT_APPROVAL_OBSERVATION_FILENAME


def read_concurrent_approval_observation(
    path: Path,
) -> ConcurrentApprovalGateObservation:
    """Read one owner-private, regular, strict observation file."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise ConcurrentApprovalGateError(
            'concurrent approval observation is invalid'
        )
    descriptor = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, 'O_CLOEXEC'):
            flags |= os.O_CLOEXEC
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_size <= 0
            or metadata.st_size > _OBSERVATION_BYTES_LIMIT
        ):
            raise ValueError
        payload = b''
        while len(payload) <= _OBSERVATION_BYTES_LIMIT:
            chunk = os.read(
                descriptor,
                _OBSERVATION_BYTES_LIMIT + 1 - len(payload),
            )
            if not chunk:
                break
            payload += chunk
        if len(payload) != metadata.st_size:
            raise ValueError
        value = json.loads(
            payload.decode('utf-8', errors='strict'),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if type(value) is not dict or set(value) != _OBSERVATION_KEYS:
            raise ValueError
        observation = ConcurrentApprovalGateObservation(
            fault_profile=value['fault_profile'],
            contender_count=value['contender_count'],
            release_count=value['release_count'],
        )
        canonical = json.dumps(
            observation.as_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('ascii')
        if payload != canonical:
            raise ValueError
        return observation
    except (
        KeyError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise ConcurrentApprovalGateError(
            'concurrent approval observation is invalid'
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate or non-string JSON object keys."""
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    """Reject non-standard NaN and infinity tokens."""
    raise ValueError


class ConcurrentApprovalResolverGate:
    """Synchronize only the two resolves after proposal-time resolution."""

    def __init__(
        self,
        delegate: Any,
        *,
        timeout_seconds: float = 10.0,
        observation_path: Path | None = None,
    ) -> None:
        """Validate the delegate and create one bounded two-party barrier."""
        if not callable(getattr(delegate, 'resolve', None)):
            raise TypeError('delegate must implement resolve')
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.0 < float(timeout_seconds) <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError('timeout_seconds is invalid')
        self._delegate = delegate
        self._timeout_seconds = float(timeout_seconds)
        self._barrier = threading.Barrier(_CONTENDER_COUNT)
        if observation_path is not None:
            if (
                not isinstance(observation_path, Path)
                or not observation_path.is_absolute()
            ):
                raise ConcurrentApprovalGateError(
                    'concurrent approval observation is invalid'
                )
            try:
                parent = observation_path.parent.resolve(strict=True)
                if observation_path.parent != parent:
                    raise ValueError
                os.lstat(observation_path)
            except FileNotFoundError:
                pass
            except (OSError, RuntimeError, ValueError):
                raise ConcurrentApprovalGateError(
                    'concurrent approval observation is invalid'
                ) from None
            else:
                raise ConcurrentApprovalGateError(
                    'concurrent approval observation is invalid'
                )
        self._observation_path = observation_path
        self._condition = threading.Condition()
        self._resolve_count = 0
        self._contender_count = 0
        self._release_count = 0
        self._observation_published = observation_path is None
        self._failed = False
        self._closed = False

    def __repr__(self) -> str:
        """Render only aggregate counts, never the delegate or target."""
        snapshot = self.snapshot()
        return (
            'ConcurrentApprovalResolverGate('
            f'contender_count={snapshot.contender_count!r}, '
            f'release_count={snapshot.release_count!r})'
        )

    def snapshot(self) -> ConcurrentApprovalGateSnapshot:
        """Return stable, content-free counts for the bounded round."""
        with self._condition:
            return ConcurrentApprovalGateSnapshot(
                contender_count=self._contender_count,
                release_count=self._release_count,
            )

    def close(self) -> None:
        """Abort a partial rendezvous during owner-directed shutdown."""
        with self._condition:
            self._closed = True
            self._failed = True
            self._barrier.abort()
            self._condition.notify_all()

    def resolve(self, location: str) -> BoundNamedTarget:
        """Delegate normally except for the second and third total calls."""
        with self._condition:
            if self._failed or self._closed:
                raise ConcurrentApprovalGateError(
                    'concurrent approval gate is unavailable'
                )
            self._resolve_count += 1
            resolve_number = self._resolve_count
            is_contender = 2 <= resolve_number <= 3
            if is_contender:
                self._contender_count += 1

        if is_contender:
            self._await_contender()
        return self._delegate.resolve(location)

    def _await_contender(self) -> None:
        """Release one approval pair or permanently fail the gate closed."""
        try:
            self._barrier.wait(timeout=self._timeout_seconds)
        except threading.BrokenBarrierError as error:
            with self._condition:
                self._failed = True
                self._condition.notify_all()
            raise ConcurrentApprovalGateError(
                'concurrent approval rendezvous failed'
            ) from error
        deadline = time.monotonic() + self._timeout_seconds
        with self._condition:
            if self._failed or self._closed:
                raise ConcurrentApprovalGateError(
                    'concurrent approval gate is unavailable'
                )
            self._release_count += 1
            if self._release_count == _CONTENDER_COUNT:
                try:
                    if self._observation_path is not None:
                        _write_observation(
                            self._observation_path,
                            ConcurrentApprovalGateObservation(),
                        )
                except Exception:
                    self._failed = True
                    self._condition.notify_all()
                    raise ConcurrentApprovalGateError(
                        'concurrent approval observation is invalid'
                    ) from None
                self._observation_published = True
                self._condition.notify_all()
            while not self._observation_published:
                if self._failed or self._closed:
                    raise ConcurrentApprovalGateError(
                        'concurrent approval gate is unavailable'
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._failed = True
                    self._condition.notify_all()
                    raise ConcurrentApprovalGateError(
                        'concurrent approval rendezvous failed'
                    )
                self._condition.wait(timeout=remaining)


def _write_observation(
    path: Path,
    observation: ConcurrentApprovalGateObservation,
) -> None:
    """Publish one complete file without following or overwriting names."""
    payload = json.dumps(
        observation.as_dict(),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('ascii')
    if not payload or len(payload) > _OBSERVATION_BYTES_LIMIT:
        raise ConcurrentApprovalGateError(
            'concurrent approval observation is invalid'
        )
    parent = path.parent
    temporary = parent / (
        '.swm25-136-concurrent-approval-'
        + secrets.token_hex(16)
        + '.tmp'
    )
    descriptor = None
    temporary_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, TypeError, ValueError):
        raise ConcurrentApprovalGateError(
            'concurrent approval observation is invalid'
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_created:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass


__all__ = [
    'CONCURRENT_APPROVAL_OBSERVATION_FILENAME',
    'ConcurrentApprovalGateError',
    'ConcurrentApprovalGateObservation',
    'ConcurrentApprovalGateSnapshot',
    'ConcurrentApprovalResolverGate',
    'concurrent_approval_observation_path',
    'read_concurrent_approval_observation',
]
