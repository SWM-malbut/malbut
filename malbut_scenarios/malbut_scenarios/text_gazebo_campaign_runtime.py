"""Run the installed SWM25-133 acceptance boundary for one campaign case."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping, Optional, Protocol

from malbut_scenarios.owned_process import (
    OwnedProcess,
    OwnedProcessError,
    ProcessCleanupEvidence,
    ProcessOutputEvidence,
    _pids_in_session,
    _process_uid,
    _read_process_stat,
)
from malbut_scenarios.text_gazebo_campaign_evidence import (
    CampaignEvidenceError,
    ChildManifestSummary,
    parse_child_manifest,
)
from malbut_scenarios.text_gazebo_runtime import sanitized_ros_environment


_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_RUN_ID = re.compile(r'run-[0-9a-f]{32}\Z')
_EXECUTABLE_RELATIVE = Path(
    'lib/malbut_scenarios/run_text_gazebo_acceptance'
)
_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_CHECK_OUTPUT_BYTES = 16 * 1024
_MAX_RUN_SECONDS = 900.0
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024


class TextGazeboCampaignRuntimeError(RuntimeError):
    """Expose one stable adapter failure without child or host details."""

    _CODES = frozenset({
        'campaign_runner_config_invalid',
        'campaign_runner_install_invalid',
        'campaign_runner_source_invalid',
        'campaign_runner_evidence_invalid',
        'campaign_runner_start_failed',
        'campaign_runner_timeout',
        'campaign_runner_child_failed',
        'campaign_runner_output_overflow',
        'campaign_runner_cleanup_incomplete',
        'campaign_runner_unexpected_failure',
    })

    def __init__(self, code: str) -> None:
        """Normalize every failure to a bounded public code."""
        normalized = (
            code if code in self._CODES
            else 'campaign_runner_unexpected_failure'
        )
        super().__init__(normalized)
        self.code = normalized


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboCampaignCheckConfig:
    """Source identity and limits needed to discover installed provenance."""

    installed_prefix: Path
    source_tree: Path
    source_commit: str
    timeout_seconds: float = 120.0
    poll_interval_seconds: float = 0.1

    def __post_init__(self) -> None:
        """Validate check configuration without reading external state."""
        if (
            not _absolute_path(self.installed_prefix)
            or not _absolute_path(self.source_tree)
            or type(self.source_commit) is not str
            or _COMMIT.fullmatch(self.source_commit) is None
            or not _bounded_seconds(
                self.timeout_seconds,
                maximum=_MAX_RUN_SECONDS,
            )
            or not _bounded_seconds(
                self.poll_interval_seconds,
                maximum=1.0,
            )
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_config_invalid'
            )

    def __repr__(self) -> str:
        """Render only the public commit, never local paths."""
        return (
            'TextGazeboCampaignCheckConfig('
            f'commit={self.source_commit!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboCampaignRunnerConfig:
    """Immutable provenance and process limits for an installed runner."""

    installed_prefix: Path
    source_tree: Path
    source_commit: str
    source_tree_digest: str
    installed_digest: str
    timeout_seconds: float = 420.0
    maximum_output_bytes: int = 8 * 1024 * 1024
    poll_interval_seconds: float = 0.1

    def __post_init__(self) -> None:
        """Validate values lexically without reading files or environment."""
        if (
            not _absolute_path(self.installed_prefix)
            or not _absolute_path(self.source_tree)
            or type(self.source_commit) is not str
            or _COMMIT.fullmatch(self.source_commit) is None
            or type(self.source_tree_digest) is not str
            or _DIGEST.fullmatch(self.source_tree_digest) is None
            or type(self.installed_digest) is not str
            or _DIGEST.fullmatch(self.installed_digest) is None
            or not _bounded_seconds(
                self.timeout_seconds,
                maximum=_MAX_RUN_SECONDS,
            )
            or isinstance(self.maximum_output_bytes, bool)
            or not isinstance(self.maximum_output_bytes, int)
            or not 4096 <= self.maximum_output_bytes <= _MAX_OUTPUT_BYTES
            or not _bounded_seconds(
                self.poll_interval_seconds,
                maximum=1.0,
            )
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_config_invalid'
            )

    def __repr__(self) -> str:
        """Render provenance digests but never local filesystem paths."""
        return (
            'TextGazeboCampaignRunnerConfig('
            f'commit={self.source_commit!r}, '
            f'source_tree_digest={self.source_tree_digest!r}, '
            f'installed_digest={self.installed_digest!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboCampaignRunRequest:
    """One explicit simulation request and its new private evidence path."""

    ros_domain_id: int
    evidence_path: Path
    gui: bool = False

    def __post_init__(self) -> None:
        """Validate explicit authority fields without filesystem access."""
        if (
            type(self.ros_domain_id) is not int
            or not 1 <= self.ros_domain_id <= 100
            or not _absolute_path(self.evidence_path)
            or type(self.gui) is not bool
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_config_invalid'
            )

    def __repr__(self) -> str:
        """Avoid exposing the case evidence path in diagnostics."""
        return (
            'TextGazeboCampaignRunRequest('
            f'ros_domain_id={self.ros_domain_id!r}, gui={self.gui!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboCampaignRunResult:
    """Content-free proof that one exact-success receipt was validated."""

    manifest_digest: str
    receipt_digest: str
    run_id: str
    commit: str
    source_tree_digest: str
    installed_digest: str
    goal_set_digest: str
    runtime_binding_digest: str
    elapsed_seconds: float
    child_output_digest: str
    child_output_bytes: int
    exact_success: bool
    cleanup_complete: bool
    forced_termination_count: int
    simulation: bool
    physical_authorized: bool
    child_manifest: ChildManifestSummary

    def __post_init__(self) -> None:
        """Keep the public result bounded and digest-only."""
        for name in (
            'manifest_digest',
            'receipt_digest',
            'source_tree_digest',
            'installed_digest',
            'goal_set_digest',
            'runtime_binding_digest',
            'child_output_digest',
        ):
            value = getattr(self, name)
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise TextGazeboCampaignRuntimeError(
                    'campaign_runner_evidence_invalid'
                )
        if (
            type(self.run_id) is not str
            or _RUN_ID.fullmatch(self.run_id) is None
            or type(self.commit) is not str
            or _COMMIT.fullmatch(self.commit) is None
            or not _bounded_seconds(
                self.elapsed_seconds,
                maximum=_MAX_RUN_SECONDS,
                allow_zero=True,
            )
            or isinstance(self.child_output_bytes, bool)
            or not isinstance(self.child_output_bytes, int)
            or not 0 <= self.child_output_bytes <= _MAX_OUTPUT_BYTES
            or type(self.exact_success) is not bool
            or type(self.cleanup_complete) is not bool
            or isinstance(self.forced_termination_count, bool)
            or not isinstance(self.forced_termination_count, int)
            or self.forced_termination_count < 0
            or type(self.simulation) is not bool
            or type(self.physical_authorized) is not bool
            or not isinstance(
                self.child_manifest,
                ChildManifestSummary,
            )
            or self.exact_success is not True
            or self.cleanup_complete is not True
            or self.forced_termination_count != 0
            or self.simulation is not True
            or self.physical_authorized is not False
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )
        child = self.child_manifest
        if (
            child.manifest_digest != self.manifest_digest
            or child.receipt_digest != self.receipt_digest
            or child.run_id != self.run_id
            or child.commit != self.commit
            or child.source_tree_digest != self.source_tree_digest
            or child.installed_digest != self.installed_digest
            or child.goal_set_digest != self.goal_set_digest
            or child.runtime_binding_digest
            != self.runtime_binding_digest
            or child.exact_success is not self.exact_success
            or child.cleanup_complete is not self.cleanup_complete
            or child.forced_termination_count
            != self.forced_termination_count
            or child.simulation is not self.simulation
            or child.physical_authorized is not self.physical_authorized
            or Decimal(str(self.elapsed_seconds))
            < Decimal(str(child.total_duration_seconds))
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )

    def __repr__(self) -> str:
        """Return only public digests and the exact-success verdict."""
        return (
            'TextGazeboCampaignRunResult('
            f'manifest_digest={self.manifest_digest!r}, '
            f'exact_success={self.exact_success!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class TextGazeboCampaignCheckResult:
    """Content-free provenance from the non-actuating installed check."""

    commit: str
    source_tree_digest: str
    installed_digest: str
    child_output_digest: str
    child_output_bytes: int
    elapsed_seconds: float
    nav2_start_count: int
    simulation: bool
    physical_authorized: bool

    def __post_init__(self) -> None:
        """Require the exact non-actuating, provenance-bound result."""
        if (
            type(self.commit) is not str
            or _COMMIT.fullmatch(self.commit) is None
            or any(
                type(getattr(self, name)) is not str
                or _DIGEST.fullmatch(getattr(self, name)) is None
                for name in (
                    'source_tree_digest',
                    'installed_digest',
                    'child_output_digest',
                )
            )
            or isinstance(self.child_output_bytes, bool)
            or not isinstance(self.child_output_bytes, int)
            or not 1 <= self.child_output_bytes <= _MAX_CHECK_OUTPUT_BYTES
            or not _bounded_seconds(
                self.elapsed_seconds,
                maximum=_MAX_RUN_SECONDS,
                allow_zero=True,
            )
            or type(self.nav2_start_count) is not int
            or self.nav2_start_count != 0
            or type(self.simulation) is not bool
            or self.simulation is not True
            or type(self.physical_authorized) is not bool
            or self.physical_authorized is not False
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )

    def __repr__(self) -> str:
        """Render only public provenance without local paths or output."""
        return (
            'TextGazeboCampaignCheckResult('
            f'commit={self.commit!r}, '
            f'source_tree_digest={self.source_tree_digest!r}, '
            f'installed_digest={self.installed_digest!r})'
        )


@dataclass(frozen=True, repr=False, slots=True)
class _CapturedOutputEvidence:
    """One bounded child payload plus its content-free accounting."""

    payload: bytes
    bytes_observed: int
    digest: str
    overflowed: bool

    def __repr__(self) -> str:
        """Never render retained child bytes in diagnostics."""
        return (
            '_CapturedOutputEvidence('
            f'bytes_observed={self.bytes_observed!r}, '
            f'digest={self.digest!r}, '
            f'overflowed={self.overflowed!r})'
        )


class _BoundedCaptureReader:
    """Drain a pipe while retaining no more than one explicit byte limit."""

    def __init__(self, stream, maximum_bytes: int) -> None:
        self._stream = stream
        self._maximum_bytes = maximum_bytes
        self._payload = bytearray()
        self._digest = hashlib.sha256()
        self._bytes_observed = 0
        self._overflowed = False
        self._error: Optional[Exception] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name='malbut-campaign-check-output',
            daemon=False,
        )

    def start(self) -> None:
        """Start exactly one bounded output collector."""
        self._thread.start()

    def join(self, timeout: float) -> bool:
        """Wait a bounded period for the output collector."""
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def evidence(self) -> _CapturedOutputEvidence:
        """Return captured bytes only after synchronization by the caller."""
        with self._lock:
            if self._error is not None:
                raise OwnedProcessError('process_identity_unavailable')
            return _CapturedOutputEvidence(
                payload=bytes(self._payload),
                bytes_observed=self._bytes_observed,
                digest=self._digest.hexdigest(),
                overflowed=self._overflowed,
            )

    def _run(self) -> None:
        try:
            while True:
                read = getattr(self._stream, 'read1', self._stream.read)
                chunk = read(4096)
                if not chunk:
                    break
                with self._lock:
                    self._bytes_observed += len(chunk)
                    remaining = self._maximum_bytes - len(self._payload)
                    if remaining > 0:
                        selected = chunk[:remaining]
                        self._payload.extend(selected)
                        self._digest.update(selected)
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


class _CapturedProcessOwner:
    """Own one Popen session while retaining only bounded check output."""

    def __init__(
        self,
        label: str,
        argv,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        maximum_output_bytes: int,
    ) -> None:
        del label
        self._argv = tuple(argv)
        self._cwd = cwd
        self._environment = dict(environment)
        self._maximum_output_bytes = maximum_output_bytes
        self._process: Optional[subprocess.Popen] = None
        self._session_id: Optional[int] = None
        self._leader_start_ticks: Optional[int] = None
        self._reader: Optional[_BoundedCaptureReader] = None
        self._cleanup: Optional[ProcessCleanupEvidence] = None

    @property
    def returncode(self) -> Optional[int]:
        """Return the child status without exposing process identity."""
        return None if self._process is None else self._process.poll()

    def start(self) -> None:
        """Start one list-argv, shell-free child in a new session."""
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
            self._record_unverified_start_cleanup(process)
            raise OwnedProcessError('process_start_failed')
        try:
            observed = _read_process_stat(process.pid)
            if (
                observed.process_group_id != process.pid
                or observed.session_id != process.pid
            ):
                raise OwnedProcessError('process_identity_unavailable')
        except BaseException:
            self._record_unverified_start_cleanup(process)
            raise
        reader = _BoundedCaptureReader(
            process.stdout,
            self._maximum_output_bytes,
        )
        self._process = process
        self._session_id = process.pid
        self._leader_start_ticks = observed.start_ticks
        self._reader = reader
        reader.start()

    def _record_unverified_start_cleanup(
        self,
        process: subprocess.Popen,
    ) -> None:
        """Fail closed when a spawned session cannot be proven or owned."""
        self._process = process
        forced = 0
        remaining = 1
        collector_stopped = process.stdout is None
        try:
            if process.poll() is None:
                process.kill()
                forced = 1
            process.wait(timeout=5)
            remaining = 0
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            if process.stdout is not None:
                process.stdout.close()
                collector_stopped = True
        except OSError:
            collector_stopped = False
        self._cleanup = ProcessCleanupEvidence(
            process_started=True,
            remaining_process_count=remaining,
            forced_termination_count=forced,
            output_collector_stopped=collector_stopped,
            output_overflowed=False,
            # The leader may have created descendants before identity was
            # available.  Never claim complete cleanup without that proof.
            cleanup_complete=False,
        )

    def require_running(self) -> None:
        """Require one live exact session and non-overflowing output."""
        if not self._owned_pids():
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
        """Stop and reap only this exact captured child session."""
        if self._cleanup is not None:
            return self._cleanup
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
        remaining = self._owned_pids()
        if remaining:
            self._signal_owned(signal.SIGINT)
            remaining = self._wait_absent(interrupt_seconds)
        if remaining:
            forced += 1
            self._signal_owned(signal.SIGTERM)
            remaining = self._wait_absent(terminate_seconds)
        if remaining:
            forced += 1
            self._signal_owned(signal.SIGKILL)
            remaining = self._wait_absent(kill_seconds)
        try:
            self._process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        reader = self._reader
        stopped = reader is None or reader.join(timeout=5.0)
        overflowed = False
        if stopped and reader is not None:
            overflowed = reader.evidence().overflowed
        remaining = self._owned_pids()
        self._cleanup = ProcessCleanupEvidence(
            process_started=True,
            remaining_process_count=len(remaining),
            forced_termination_count=forced,
            output_collector_stopped=stopped,
            output_overflowed=overflowed,
            cleanup_complete=(
                not remaining and stopped and not overflowed
            ),
        )
        return self._cleanup

    def output_evidence(self) -> _CapturedOutputEvidence:
        """Return no more than the configured number of child bytes."""
        reader = self._reader
        if reader is None:
            return _CapturedOutputEvidence(
                payload=b'',
                bytes_observed=0,
                digest=hashlib.sha256(b'').hexdigest(),
                overflowed=False,
            )
        return reader.evidence()

    def _owned_pids(self) -> tuple[int, ...]:
        session_id = self._session_id
        start_ticks = self._leader_start_ticks
        if session_id is None:
            return ()
        if start_ticks is None:
            raise OwnedProcessError('process_identity_unavailable')
        leader_uid = _process_uid(session_id)
        if leader_uid is not None:
            try:
                observed = _read_process_stat(session_id)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                observed = None
            if (
                observed is not None
                and (
                    leader_uid != os.getuid()
                    or observed.start_ticks != start_ticks
                )
            ):
                raise OwnedProcessError('process_identity_unavailable')
        return _pids_in_session(session_id, os.getuid())

    def _signal_owned(self, selected_signal: signal.Signals) -> None:
        session_id = self._session_id
        if session_id is None:
            return
        owned = []
        for pid in self._owned_pids():
            try:
                observed = _read_process_stat(pid)
            except (FileNotFoundError, ProcessLookupError):
                continue
            if observed.session_id == session_id:
                owned.append((pid, observed))
        if not owned:
            return
        try:
            os.killpg(session_id, selected_signal)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise OwnedProcessError(
                'process_cleanup_incomplete'
            ) from error
        for pid, before in owned:
            if before.process_group_id == session_id:
                continue
            try:
                if _process_uid(pid) != os.getuid():
                    continue
                current = _read_process_stat(pid)
                if (
                    current.session_id != session_id
                    or current.process_group_id != before.process_group_id
                    or current.start_ticks != before.start_ticks
                ):
                    continue
                os.kill(pid, selected_signal)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as error:
                raise OwnedProcessError(
                    'process_cleanup_incomplete'
                ) from error

    def _wait_absent(self, timeout: float) -> tuple[int, ...]:
        deadline = time.monotonic() + timeout
        while True:
            if self._process is not None:
                self._process.poll()
            remaining = self._owned_pids()
            if not remaining or time.monotonic() >= deadline:
                return remaining
            time.sleep(0.05)


class _ProcessOwner(Protocol):
    """Narrow process-owner surface used by the adapter."""

    @property
    def returncode(self) -> Optional[int]:
        """Return the child exit status or ``None`` while active."""

    def start(self) -> None:
        """Start one exactly owned child session."""

    def require_running(self) -> None:
        """Verify that the child session and output collector are healthy."""

    def stop(self, **kwargs: float) -> ProcessCleanupEvidence:
        """Stop and reap the exact child session."""

    def output_evidence(self) -> ProcessOutputEvidence:
        """Return digest-only bounded child output evidence."""


_OwnerFactory = Callable[..., _ProcessOwner]


class InstalledTextGazeboAcceptanceRunner:
    """Invoke exactly one installed SWM25-133 runner as a child session."""

    def __init__(
        self,
        config: (
            TextGazeboCampaignCheckConfig
            | TextGazeboCampaignRunnerConfig
        ),
        *,
        owner_factory: _OwnerFactory = OwnedProcess,
        capture_owner_factory: _OwnerFactory = _CapturedProcessOwner,
        temporary_directory_factory: Callable = tempfile.TemporaryDirectory,
        environment_source: Callable[[], Mapping[str, str]] = (
            lambda: os.environ
        ),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Store dependencies without filesystem, environment, or child I/O."""
        if (
            not isinstance(
                config,
                (
                    TextGazeboCampaignCheckConfig,
                    TextGazeboCampaignRunnerConfig,
                ),
            )
            or not callable(owner_factory)
            or not callable(capture_owner_factory)
            or not callable(temporary_directory_factory)
            or not callable(environment_source)
            or not callable(monotonic)
            or not callable(sleep)
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_config_invalid'
            )
        self._config = config
        self._owner_factory = owner_factory
        self._capture_owner_factory = capture_owner_factory
        self._temporary_directory_factory = temporary_directory_factory
        self._environment_source = environment_source
        self._monotonic = monotonic
        self._sleep = sleep

    def __repr__(self) -> str:
        """Report configuration state without paths or launch material."""
        return 'InstalledTextGazeboAcceptanceRunner(configured=True)'

    def run(
        self,
        request: TextGazeboCampaignRunRequest,
    ) -> TextGazeboCampaignRunResult:
        """Run, reap, and strictly verify one installed acceptance child."""
        if not isinstance(request, TextGazeboCampaignRunRequest):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_config_invalid'
            )
        if not isinstance(self._config, TextGazeboCampaignRunnerConfig):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_config_invalid'
            )
        executable, source_tree, evidence_parent = self._runtime_paths(
            request
        )
        environment = self._environment(
            request=request,
            private_home=evidence_parent,
        )
        argv = self._argv(executable, source_tree, request)
        try:
            owner = self._owner_factory(
                'campaign-case-runner',
                argv,
                cwd=source_tree,
                environment=environment,
                maximum_output_bytes=self._config.maximum_output_bytes,
            )
        except Exception:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_start_failed'
            ) from None

        started = self._monotonic()
        primary_error: Optional[TextGazeboCampaignRuntimeError] = None
        primary_interrupt: Optional[KeyboardInterrupt] = None
        returncode: Optional[int] = None
        child_started = False
        try:
            try:
                owner.start()
                child_started = True
            except Exception:
                raise TextGazeboCampaignRuntimeError(
                    'campaign_runner_start_failed'
                ) from None
            deadline = started + self._config.timeout_seconds
            while True:
                returncode = owner.returncode
                if returncode is not None:
                    break
                if self._monotonic() >= deadline:
                    raise TextGazeboCampaignRuntimeError(
                        'campaign_runner_timeout'
                    )
                try:
                    owner.require_running()
                except OwnedProcessError as error:
                    if error.code == 'process_output_overflow':
                        raise TextGazeboCampaignRuntimeError(
                            'campaign_runner_output_overflow'
                        ) from None
                    observed_returncode = owner.returncode
                    if observed_returncode is not None:
                        returncode = observed_returncode
                        break
                    raise TextGazeboCampaignRuntimeError(
                        'campaign_runner_child_failed'
                    ) from None
                self._sleep(self._config.poll_interval_seconds)
        except TextGazeboCampaignRuntimeError as error:
            primary_error = error
        except KeyboardInterrupt as error:
            primary_interrupt = error
        except BaseException:
            primary_error = TextGazeboCampaignRuntimeError(
                'campaign_runner_unexpected_failure'
            )

        cleanup = self._stop(owner)
        allow_not_started = bool(
            not child_started
            and primary_error is not None
            and primary_error.code == 'campaign_runner_start_failed'
        )
        if not (
            (cleanup.process_started or allow_not_started)
            and cleanup.cleanup_complete
            and cleanup.remaining_process_count == 0
            and cleanup.output_collector_stopped
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_cleanup_incomplete'
            )
        output = self._output(owner)
        if cleanup.output_overflowed or output.overflowed:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_output_overflow'
            )
        if primary_interrupt is not None:
            if cleanup.forced_termination_count != 0:
                raise TextGazeboCampaignRuntimeError(
                    'campaign_runner_cleanup_incomplete'
                )
            raise primary_interrupt
        if primary_error is not None:
            raise primary_error
        if cleanup.forced_termination_count != 0:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_cleanup_incomplete'
            )
        if returncode != 0:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_child_failed'
            )

        child = _read_manifest(request.evidence_path)
        if (
            child.commit != self._config.source_commit
            or child.source_tree_digest
            != self._config.source_tree_digest
            or child.installed_digest != self._config.installed_digest
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )
        elapsed = self._monotonic() - started
        return TextGazeboCampaignRunResult(
            manifest_digest=child.manifest_digest,
            receipt_digest=child.receipt_digest,
            run_id=child.run_id,
            commit=child.commit,
            source_tree_digest=child.source_tree_digest,
            installed_digest=child.installed_digest,
            goal_set_digest=child.goal_set_digest,
            runtime_binding_digest=child.runtime_binding_digest,
            elapsed_seconds=elapsed,
            child_output_digest=output.digest,
            child_output_bytes=output.bytes_observed,
            exact_success=True,
            cleanup_complete=True,
            forced_termination_count=cleanup.forced_termination_count,
            simulation=child.simulation,
            physical_authorized=child.physical_authorized,
            child_manifest=child,
        )

    def check(self) -> TextGazeboCampaignCheckResult:
        """Run the installed non-actuating provenance check in one child."""
        executable, source_tree = self._installed_source_paths()
        started = self._monotonic()
        try:
            with self._temporary_directory_factory(
                prefix='malbut-swm25-134-check-',
            ) as temporary:
                private_home = Path(temporary).resolve(strict=True)
                private_home.chmod(0o700)
                environment = self._environment_for(
                    domain_id=1,
                    gui=False,
                    private_home=private_home,
                )
                argv = (
                    str(executable),
                    '--check',
                    '--source-commit',
                    self._config.source_commit,
                    '--source-tree',
                    str(source_tree),
                )
                try:
                    owner = self._capture_owner_factory(
                        'campaign-check-runner',
                        argv,
                        cwd=source_tree,
                        environment=environment,
                        maximum_output_bytes=_MAX_CHECK_OUTPUT_BYTES,
                    )
                except Exception:
                    raise TextGazeboCampaignRuntimeError(
                        'campaign_runner_start_failed'
                    ) from None
                returncode, cleanup, output = self._execute_captured(owner)
        except TextGazeboCampaignRuntimeError:
            raise
        except Exception:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_unexpected_failure'
            ) from None
        if returncode != 0:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_child_failed'
            )
        if not isinstance(output, _CapturedOutputEvidence):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )
        value = _strict_check_output(output.payload)
        if isinstance(self._config, TextGazeboCampaignRunnerConfig) and (
            value['source_tree_digest']
            != self._config.source_tree_digest
            or value['installed_digest'] != self._config.installed_digest
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )
        return TextGazeboCampaignCheckResult(
            commit=self._config.source_commit,
            source_tree_digest=value['source_tree_digest'],
            installed_digest=value['installed_digest'],
            child_output_digest=output.digest,
            child_output_bytes=output.bytes_observed,
            elapsed_seconds=self._monotonic() - started,
            nav2_start_count=value['nav2_start_count'],
            simulation=value['simulation'],
            physical_authorized=value['physical_authorized'],
        )

    def _execute_captured(
        self,
        owner: _ProcessOwner,
    ) -> tuple[int, ProcessCleanupEvidence, object]:
        primary_error: Optional[TextGazeboCampaignRuntimeError] = None
        primary_interrupt: Optional[KeyboardInterrupt] = None
        returncode: Optional[int] = None
        child_started = False
        try:
            try:
                owner.start()
                child_started = True
            except Exception:
                raise TextGazeboCampaignRuntimeError(
                    'campaign_runner_start_failed'
                ) from None
            deadline = self._monotonic() + self._config.timeout_seconds
            while True:
                returncode = owner.returncode
                if returncode is not None:
                    break
                if self._monotonic() >= deadline:
                    raise TextGazeboCampaignRuntimeError(
                        'campaign_runner_timeout'
                    )
                try:
                    owner.require_running()
                except OwnedProcessError as error:
                    if error.code == 'process_output_overflow':
                        raise TextGazeboCampaignRuntimeError(
                            'campaign_runner_output_overflow'
                        ) from None
                    observed_returncode = owner.returncode
                    if observed_returncode is not None:
                        returncode = observed_returncode
                        break
                    raise TextGazeboCampaignRuntimeError(
                        'campaign_runner_child_failed'
                    ) from None
                self._sleep(self._config.poll_interval_seconds)
        except TextGazeboCampaignRuntimeError as error:
            primary_error = error
        except KeyboardInterrupt as error:
            primary_interrupt = error
        except BaseException:
            primary_error = TextGazeboCampaignRuntimeError(
                'campaign_runner_unexpected_failure'
            )
        cleanup = self._stop(owner)
        allow_not_started = bool(
            not child_started
            and primary_error is not None
            and primary_error.code == 'campaign_runner_start_failed'
        )
        if not (
            (cleanup.process_started or allow_not_started)
            and cleanup.cleanup_complete
            and cleanup.remaining_process_count == 0
            and cleanup.output_collector_stopped
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_cleanup_incomplete'
            )
        try:
            output = owner.output_evidence()
        except Exception:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_output_overflow'
            ) from None
        if cleanup.output_overflowed or output.overflowed:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_output_overflow'
            )
        if primary_interrupt is not None:
            if cleanup.forced_termination_count != 0:
                raise TextGazeboCampaignRuntimeError(
                    'campaign_runner_cleanup_incomplete'
                )
            raise primary_interrupt
        if primary_error is not None:
            raise primary_error
        if cleanup.forced_termination_count != 0:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_cleanup_incomplete'
            )
        if returncode is None:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_child_failed'
            )
        return returncode, cleanup, output

    def _runtime_paths(
        self,
        request: TextGazeboCampaignRunRequest,
    ) -> tuple[Path, Path, Path]:
        executable, source_tree = self._installed_source_paths()
        parent = _private_existing_directory(request.evidence_path.parent)
        try:
            os.lstat(request.evidence_path)
        except FileNotFoundError:
            pass
        except OSError:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            ) from None
        else:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )
        return executable, source_tree, parent

    def _installed_source_paths(self) -> tuple[Path, Path]:
        prefix = _canonical_directory(
            self._config.installed_prefix,
            'campaign_runner_install_invalid',
        )
        source_tree = _canonical_directory(
            self._config.source_tree,
            'campaign_runner_source_invalid',
        )
        executable = prefix / _EXECUTABLE_RELATIVE
        try:
            metadata = os.lstat(executable)
            canonical = executable.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_install_invalid'
            ) from None
        if (
            canonical != executable
            or not stat.S_ISREG(metadata.st_mode)
            or not os.access(executable, os.X_OK)
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_install_invalid'
            )
        return executable, source_tree

    def _environment(
        self,
        *,
        request: TextGazeboCampaignRunRequest,
        private_home: Path,
    ) -> dict[str, str]:
        return self._environment_for(
            domain_id=request.ros_domain_id,
            gui=request.gui,
            private_home=private_home,
        )

    def _environment_for(
        self,
        *,
        domain_id: int,
        gui: bool,
        private_home: Path,
    ) -> dict[str, str]:
        try:
            source = self._environment_source()
            if not isinstance(source, Mapping):
                raise TypeError
            return sanitized_ros_environment(
                source,
                private_home=private_home,
                domain_id=domain_id,
                gui=gui,
            )
        except Exception:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_config_invalid'
            ) from None

    def _argv(
        self,
        executable: Path,
        source_tree: Path,
        request: TextGazeboCampaignRunRequest,
    ) -> tuple[str, ...]:
        values = [
            str(executable),
            '--run',
            '--execute-approved-simulation',
            '--source-commit',
            self._config.source_commit,
            '--source-tree',
            str(source_tree),
            '--ros-domain-id',
            str(request.ros_domain_id),
            '--evidence',
            str(request.evidence_path),
        ]
        if request.gui:
            values.append('--gui')
        return tuple(values)

    @staticmethod
    def _stop(owner: _ProcessOwner) -> ProcessCleanupEvidence:
        try:
            return owner.stop(
                interrupt_seconds=20.0,
                terminate_seconds=10.0,
                kill_seconds=5.0,
            )
        except Exception:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_cleanup_incomplete'
            ) from None

    @staticmethod
    def _output(owner: _ProcessOwner) -> ProcessOutputEvidence:
        try:
            return owner.output_evidence()
        except Exception:
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_output_overflow'
            ) from None


def _absolute_path(value: object) -> bool:
    return bool(
        isinstance(value, Path)
        and value.is_absolute()
        and value.name not in {'', '.', '..'}
    )


def _bounded_seconds(
    value: object,
    *,
    maximum: float,
    allow_zero: bool = False,
) -> bool:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return False
    normalized = float(value)
    return (
        0.0 <= normalized <= maximum
        if allow_zero else 0.0 < normalized <= maximum
    )


def _canonical_directory(path: Path, code: str) -> Path:
    try:
        metadata = os.lstat(path)
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise TextGazeboCampaignRuntimeError(code) from None
    if (
        canonical != path
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise TextGazeboCampaignRuntimeError(code)
    return canonical


def _private_existing_directory(path: Path) -> Path:
    parent = _canonical_directory(
        path,
        'campaign_runner_evidence_invalid',
    )
    try:
        metadata = os.lstat(parent)
    except OSError:
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        ) from None
    if (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        )
    return parent


def _strict_object(raw: bytes) -> dict[str, object]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError

    try:
        decoded = raw.decode('utf-8')
        value = json.loads(
            decoded,
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        ) from None
    if type(value) is not dict:
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        )
    return value


def _strict_check_output(raw: bytes) -> dict[str, object]:
    value = _exact(_strict_object(raw), {
        'installed_digest',
        'mode',
        'nav2_start_count',
        'physical_authorized',
        'simulation',
        'source_tree_digest',
        'status',
    })
    if (
        value['mode'] != 'check'
        or value['status'] != 'ok'
        or type(value['nav2_start_count']) is not int
        or value['nav2_start_count'] != 0
        or type(value['simulation']) is not bool
        or value['simulation'] is not True
        or type(value['physical_authorized']) is not bool
        or value['physical_authorized'] is not False
        or type(value['source_tree_digest']) is not str
        or _DIGEST.fullmatch(value['source_tree_digest']) is None
        or type(value['installed_digest']) is not str
        or _DIGEST.fullmatch(value['installed_digest']) is None
    ):
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        )
    expected = (
        json.dumps(value, ensure_ascii=True, sort_keys=True) + '\n'
    ).encode('utf-8')
    if raw != expected:
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        )
    return value


def _exact(value: object, names: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != names:
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        )
    return value


def _read_manifest(path: Path) -> ChildManifestSummary:
    _private_existing_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= _MAX_EVIDENCE_BYTES
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )
        raw = os.read(descriptor, _MAX_EVIDENCE_BYTES + 1)
        after = os.fstat(descriptor)
        path_after = os.lstat(path)
        if (
            len(raw) != before.st_size
            or len(raw) > _MAX_EVIDENCE_BYTES
            or _status_identity(before) != _status_identity(after)
            or _status_identity(after) != _status_identity(path_after)
            or not stat.S_ISREG(path_after.st_mode)
            or path_after.st_uid != os.getuid()
            or stat.S_IMODE(path_after.st_mode) != 0o600
            or path_after.st_nlink != 1
        ):
            raise TextGazeboCampaignRuntimeError(
                'campaign_runner_evidence_invalid'
            )
    except TextGazeboCampaignRuntimeError:
        raise
    except OSError:
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        return parse_child_manifest(raw)
    except CampaignEvidenceError:
        raise TextGazeboCampaignRuntimeError(
            'campaign_runner_evidence_invalid'
        ) from None


def _status_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return the mutable and identity fields used for race detection."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


__all__ = [
    'InstalledTextGazeboAcceptanceRunner',
    'TextGazeboCampaignCheckResult',
    'TextGazeboCampaignCheckConfig',
    'TextGazeboCampaignRunRequest',
    'TextGazeboCampaignRunResult',
    'TextGazeboCampaignRunnerConfig',
    'TextGazeboCampaignRuntimeError',
]
