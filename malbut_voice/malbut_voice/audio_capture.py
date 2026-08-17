"""Bounded in-memory one-shot capture from one attested ALSA device."""

import os
import re
import selectors
import signal
import subprocess
import time

from malbut_voice.config import (
    AudioBinding,
    CapturePolicy,
    _audio_binding_fingerprint,
    _capture_policy_fingerprint,
)
from malbut_voice.device_attestation import AlsaDeviceInspector
from malbut_voice.errors import (
    CaptureError,
    DeviceAttestationError,
    chain_free_boundary,
)
from malbut_voice.provenance import _ProvenanceAuthority, zeroize


BOOT_ID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}$'
)


def _system_boottime_ns():
    if not hasattr(time, 'CLOCK_BOOTTIME'):
        raise CaptureError('clock_boottime_unavailable')
    try:
        value = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    except (OSError, OverflowError):
        raise CaptureError('clock_boottime_failed')
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CaptureError('clock_boottime_invalid')
    return value


def _read_boot_id():
    path = '/proc/sys/kernel/random/boot_id'
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            payload = os.read(descriptor, 65)
        finally:
            os.close(descriptor)
        value = payload.decode('ascii').strip()
    except (OSError, UnicodeDecodeError):
        raise CaptureError('boot_id_unavailable')
    if BOOT_ID_PATTERN.fullmatch(value) is None:
        raise CaptureError('boot_id_invalid')
    return value


def _system_process_group_exists(process_group_id):
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BoundedArecordCapture:
    """Issue private PCM capabilities from one explicit bounded subprocess."""

    def __init__(
        self,
        binding,
        policy,
        authority,
        *,
        inspector=None,
        popen_factory=None,
        clock_ns=None,
        selector_factory=None,
        killpg=None,
        sleeper=None,
        cleanup_clock_ns=None,
        boot_id_reader=None,
        group_exists=None,
    ):
        """Configure dependencies; construction never opens a microphone."""
        if not isinstance(binding, AudioBinding):
            raise TypeError('binding must be AudioBinding')
        if not isinstance(policy, CapturePolicy):
            raise TypeError('policy must be CapturePolicy')
        if not isinstance(authority, _ProvenanceAuthority):
            raise TypeError('authority must be private provenance authority')
        self._binding = binding
        self._policy = policy
        self._binding_fingerprint = _audio_binding_fingerprint(binding)
        self._policy_fingerprint = _capture_policy_fingerprint(policy)
        self._authority = authority
        self._inspector = (
            AlsaDeviceInspector(binding) if inspector is None else inspector
        )
        self._popen = (
            subprocess.Popen if popen_factory is None else popen_factory
        )
        self._clock_ns = _system_boottime_ns if clock_ns is None else clock_ns
        self._selector_factory = (
            selectors.DefaultSelector
            if selector_factory is None
            else selector_factory
        )
        self._killpg = os.killpg if killpg is None else killpg
        self._sleep = time.sleep if sleeper is None else sleeper
        self._cleanup_clock_ns = (
            time.monotonic_ns
            if cleanup_clock_ns is None
            else cleanup_clock_ns
        )
        self._boot_id_reader = (
            _read_boot_id if boot_id_reader is None else boot_id_reader
        )
        self._group_exists = (
            _system_process_group_exists
            if group_exists is None
            else group_exists
        )

    @chain_free_boundary
    def prepare(self):
        """Statically attest configured resources without spawning arecord."""
        self._assert_configuration_intact()
        return self._inspector.attest()

    def _assert_configuration_intact(self):
        if (
            _audio_binding_fingerprint(self._binding)
            != self._binding_fingerprint
            or _capture_policy_fingerprint(self._policy)
            != self._policy_fingerprint
        ):
            raise CaptureError('capture_configuration_changed')

    def _argv(self, duration_seconds):
        return [
            str(self._binding.arecord_path),
            '--quiet',
            f'--device={self._binding.alsa_device}',
            '--file-type=raw',
            f'--format={self._policy.sample_format}',
            f'--rate={self._policy.sample_rate_hz}',
            f'--channels={self._policy.channels}',
            f'--duration={duration_seconds}',
            '-',
        ]

    def _environment(self):
        return {
            'ALSA_CONFIG_PATH': str(self._binding.alsa_config_path),
            'ALSA_CONFIG_DIR': '/usr/share/alsa',
            'HOME': '/nonexistent',
            'LANG': 'C',
            'LC_ALL': 'C',
        }

    def _spawn(self, duration_seconds):
        try:
            return self._popen(
                self._argv(duration_seconds),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
                env=self._environment(),
            )
        except (OSError, ValueError):
            raise CaptureError('capture_spawn_failed')

    def _terminate_and_reap(self, process):
        def group_is_alive():
            try:
                process.poll()
                return bool(self._group_exists(process.pid))
            except BaseException:
                return True

        def signal_group(sent_signal):
            try:
                self._killpg(process.pid, sent_signal)
                return True
            except BaseException:
                return False

        def wait_for_group_exit(grace_ms):
            if not group_is_alive():
                return True
            try:
                started = self._cleanup_clock_ns()
                if (
                    isinstance(started, bool)
                    or not isinstance(started, int)
                    or started < 0
                ):
                    return False
                deadline = started + grace_ms * 1000000
                while group_is_alive():
                    now = self._cleanup_clock_ns()
                    if (
                        isinstance(now, bool)
                        or not isinstance(now, int)
                        or now < 0
                        or now >= deadline
                    ):
                        return False
                    self._sleep(0.005)
                return True
            except BaseException:
                return False

        # Signal first. A failing deadline helper must never prevent recorder
        # teardown after capture has raised or an interrupt has arrived.
        term_sent = signal_group(signal.SIGTERM)
        term_completed = term_sent and wait_for_group_exit(
            self._policy.term_grace_ms,
        )
        if not term_completed:
            signal_group(signal.SIGKILL)
            wait_for_group_exit(self._policy.kill_grace_ms)
        group_timed_out = group_is_alive()
        reap_failed = False
        try:
            process.wait(
                timeout=max(self._policy.kill_grace_ms / 1000.0, 0.001),
            )
        except BaseException:
            reap_failed = True
        if group_timed_out:
            raise CaptureError('capture_teardown_timeout')
        if reap_failed:
            raise CaptureError('capture_reap_failed')

    @staticmethod
    def _close_streams(process):
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    @staticmethod
    def _read_ready(selector, pcm, stderr, pcm_limit, stderr_limit):
        for key, _events in selector.select(timeout=0.02):
            stream = key.fileobj
            is_pcm = key.data == 'pcm'
            target = pcm if is_pcm else stderr
            limit = pcm_limit if is_pcm else stderr_limit
            try:
                read_size = min(65536, limit - len(target) + 1)
                chunk = os.read(stream.fileno(), read_size)
            except BlockingIOError:
                continue
            except OSError:
                raise CaptureError('capture_pipe_read_failed')
            if not chunk:
                try:
                    selector.unregister(stream)
                except (KeyError, ValueError):
                    pass
                continue
            target.extend(chunk)
            if len(target) > limit:
                code = (
                    'capture_pcm_overflow'
                    if is_pcm
                    else 'capture_stderr_overflow'
                )
                raise CaptureError(code)

    @chain_free_boundary
    def capture(self, duration_seconds, expected_attestation=None):
        """Capture one exact raw PCM payload after explicit caller action."""
        self._assert_configuration_intact()
        expected_bytes = self._policy.expected_bytes(duration_seconds)
        attestation = self.prepare()
        if (
            expected_attestation is not None
            and attestation != expected_attestation
        ):
            raise CaptureError('capture_device_attestation_changed')
        boot_id = self._boot_id_reader()
        pcm = bytearray()
        stderr = bytearray()
        process = None
        selector = None
        try:
            started = self._clock_ns()
            process = self._spawn(duration_seconds)
            if process.stdout is None or process.stderr is None:
                raise CaptureError('capture_pipes_unavailable')
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            selector = self._selector_factory()
            selector.register(process.stdout, selectors.EVENT_READ, 'pcm')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            attestation_deadline = started + (
                self._policy.attestation_timeout_ms * 1000000
            )
            completion_deadline = started + (
                duration_seconds * 1000000000
                + self._policy.completion_grace_ms * 1000000
            )
            child_attested = False
            while True:
                now = self._clock_ns()
                if not child_attested:
                    try:
                        child_attested = (
                            self._inspector.child_has_attested_device(
                                process.pid,
                                attestation,
                            )
                        )
                    except DeviceAttestationError:
                        raise CaptureError('capture_child_rejected')
                    if not child_attested and now >= attestation_deadline:
                        raise CaptureError('capture_child_attestation_timeout')
                if now >= completion_deadline:
                    raise CaptureError('capture_deadline_exceeded')
                self._read_ready(
                    selector,
                    pcm,
                    stderr,
                    expected_bytes,
                    self._policy.maximum_stderr_bytes,
                )
                return_code = process.poll()
                if return_code is not None and not selector.get_map():
                    break
            process.wait(timeout=0)
            if self._group_exists(process.pid):
                raise CaptureError('capture_descendant_remained')
            ended = self._clock_ns()
            if not child_attested:
                raise CaptureError('capture_child_unattested')
            if return_code != 0:
                raise CaptureError('capture_process_failed')
            if len(pcm) != expected_bytes or len(pcm) % 2:
                raise CaptureError('capture_pcm_truncated')
            return self._authority.issue_capture(
                pcm,
                boot_id=boot_id,
                device_binding_digest=attestation.binding_digest,
                started_boottime_ns=started,
                ended_boottime_ns=ended,
                sample_rate_hz=self._policy.sample_rate_hz,
                channels=self._policy.channels,
            )
        except BaseException:
            zeroize(pcm)
            if process is not None:
                # Teardown failure intentionally takes priority over an
                # interrupt because an uncontained recorder is the larger
                # safety failure. The public decorator removes its chain.
                self._terminate_and_reap(process)
            raise
        finally:
            zeroize(stderr)
            if selector is not None:
                selector.close()
            if process is not None:
                self._close_streams(process)
