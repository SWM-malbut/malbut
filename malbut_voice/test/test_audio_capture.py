"""Tests for exact bounded subprocess capture without opening a microphone."""

import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from malbut_voice.audio_capture import BoundedArecordCapture
from malbut_voice.config import AudioBinding, CapturePolicy
from malbut_voice.errors import CaptureError, ConfigSecurityError
from malbut_voice.provenance import (
    DeviceAttestation,
    _ProvenanceAuthority,
    zeroize,
)


ATTESTATION = DeviceAttestation(
    binding_digest='a' * 64,
    binary_device=1,
    binary_inode=2,
    rdev_major=116,
    rdev_minor=9,
)


class _FakeInspector:
    def __init__(self, *, child_attested=True):
        self.child_attested = child_attested
        self.child_checks = 0

    def attest(self):
        return ATTESTATION

    def child_has_attested_device(self, _pid, _attestation):
        self.child_checks += 1
        return self.child_attested


def _binding():
    return AudioBinding(
        alsa_device='plughw:CARD=PCH,DEV=0',
        device_node=Path('/dev/snd/pcmC1D0c'),
        rdev_major=116,
        rdev_minor=9,
        device_uid=0,
        device_gid=29,
        device_mode=0o660,
        card_id='PCH',
        driver='snd_hda_intel',
        arecord_path=Path('/usr/bin/arecord'),
        arecord_resolved_path=Path('/usr/bin/aplay'),
        arecord_sha256='a' * 64,
        alsa_config_path=Path('/usr/share/alsa/alsa.conf'),
        alsa_config_sha256='b' * 64,
    )


def _policy():
    return CapturePolicy(
        sample_rate_hz=16000,
        channels=1,
        sample_format='S16_LE',
        default_duration_seconds=1,
        maximum_duration_seconds=1,
        maximum_stderr_bytes=256,
        attestation_timeout_ms=100,
        completion_grace_ms=1000,
        term_grace_ms=50,
        kill_grace_ms=50,
    )


def _subprocess_factory(program, calls):
    def factory(argv, **kwargs):
        calls.append((argv, kwargs))
        child_argv = [sys.executable, '-c', program]
        return subprocess.Popen(child_argv, **kwargs)

    return factory


def _capture(program, calls, *, policy=None, inspector=None):
    authority = _ProvenanceAuthority()
    capture = BoundedArecordCapture(
        _binding(),
        _policy() if policy is None else policy,
        authority,
        inspector=_FakeInspector() if inspector is None else inspector,
        popen_factory=_subprocess_factory(program, calls),
        boot_id_reader=lambda: (
            '00000000-0000-0000-0000-000000000001'
        ),
    )
    return authority, capture


def test_capture_uses_exact_shell_free_sanitized_arecord_contract():
    """Capture exact PCM while asserting argv, pipes, and sanitized env."""
    calls = []
    program = 'import sys;sys.stdout.buffer.write(bytes(32000))'
    authority, capture = _capture(program, calls)

    capability = capture.capture(1, expected_attestation=ATTESTATION)
    pcm, receipt = authority.consume_capture(capability)

    assert len(pcm) == 32000
    assert receipt.frame_count == 16000
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        '/usr/bin/arecord',
        '--quiet',
        '--device=plughw:CARD=PCH,DEV=0',
        '--file-type=raw',
        '--format=S16_LE',
        '--rate=16000',
        '--channels=1',
        '--duration=1',
        '-',
    ]
    assert kwargs['shell'] is False
    assert kwargs['stdin'] is subprocess.DEVNULL
    assert kwargs['stdout'] is subprocess.PIPE
    assert kwargs['stderr'] is subprocess.PIPE
    assert kwargs['close_fds'] is True
    assert kwargs['start_new_session'] is True
    assert kwargs['env'] == {
        'ALSA_CONFIG_PATH': '/usr/share/alsa/alsa.conf',
        'ALSA_CONFIG_DIR': '/usr/share/alsa',
        'HOME': '/nonexistent',
        'LANG': 'C',
        'LC_ALL': 'C',
    }
    zeroize(pcm)


@pytest.mark.parametrize(
    ('program', 'error_code'),
    [
        (
            'import sys;sys.stdout.buffer.write(bytes(32001))',
            'capture_pcm_overflow',
        ),
        (
            'import sys;sys.stderr.buffer.write(bytes(257))',
            'capture_stderr_overflow',
        ),
        (
            'import sys;sys.stdout.buffer.write(bytes(10))',
            'capture_pcm_truncated',
        ),
        ('import sys;sys.exit(7)', 'capture_process_failed'),
    ],
)
def test_capture_rejects_overflow_truncation_and_process_failure(
    program,
    error_code,
):
    """Fail closed for every non-exact subprocess result."""
    authority, capture = _capture(program, [])

    with pytest.raises(CaptureError, match=error_code):
        capture.capture(1, expected_attestation=ATTESTATION)

    assert authority.instance_id


def test_capture_rejects_child_that_never_opens_attested_rdev():
    """Require live child fd attestation before accepting any PCM."""
    calls = []
    program = (
        'import sys,time;'
        'sys.stdout.buffer.write(bytes(32000));'
        'sys.stdout.buffer.flush();time.sleep(0.3)'
    )
    inspector = _FakeInspector(child_attested=False)
    policy = replace(_policy(), attestation_timeout_ms=50)
    _authority, capture = _capture(
        program,
        calls,
        policy=policy,
        inspector=inspector,
    )

    with pytest.raises(CaptureError, match='attestation_timeout'):
        capture.capture(1, expected_attestation=ATTESTATION)

    assert inspector.child_checks >= 1


class _DeadlineProcess:
    def __init__(self):
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        os.close(stdout_write)
        os.close(stderr_write)
        self.stdout = os.fdopen(stdout_read, 'rb', buffering=0)
        self.stderr = os.fdopen(stderr_read, 'rb', buffering=0)
        self.pid = 424242
        self.returncode = None
        self.waited = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired('fake', timeout)
        return self.returncode


def test_deadline_orders_term_then_kill_and_reaps_child():
    """Escalate the process group through TERM and KILL, then wait once."""
    process = _DeadlineProcess()
    signals = []
    clock_value = [0]

    def clock_ns():
        clock_value[0] += 2000000000
        return clock_value[0]

    def killpg(pid, sent_signal):
        assert pid == process.pid
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            process.returncode = -signal.SIGKILL

    authority = _ProvenanceAuthority()
    capture = BoundedArecordCapture(
        _binding(),
        _policy(),
        authority,
        inspector=_FakeInspector(),
        popen_factory=lambda _argv, **_kwargs: process,
        clock_ns=clock_ns,
        killpg=killpg,
        cleanup_clock_ns=clock_ns,
        group_exists=lambda _pid: process.returncode is None,
        sleeper=lambda _seconds: None,
        boot_id_reader=lambda: (
            '00000000-0000-0000-0000-000000000001'
        ),
    )

    with pytest.raises(CaptureError, match='deadline_exceeded'):
        capture.capture(1, expected_attestation=ATTESTATION)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.waited is True


def test_cleanup_kills_retained_pipe_group_after_leader_already_exited():
    """Do not leave descendants alive when the session leader exits first."""
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()

    class ExitedLeader:
        pid = 434343
        returncode = 0
        stdout = os.fdopen(stdout_read, 'rb', buffering=0)
        stderr = os.fdopen(stderr_read, 'rb', buffering=0)
        waited = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.waited = True
            return self.returncode

    process = ExitedLeader()
    group_alive = [True]
    signals = []
    clock_value = [0]

    def clock_ns():
        clock_value[0] += 2000000000
        return clock_value[0]

    def killpg(_pid, sent_signal):
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            group_alive[0] = False
            os.close(stdout_write)
            os.close(stderr_write)

    capture = BoundedArecordCapture(
        _binding(),
        _policy(),
        _ProvenanceAuthority(),
        inspector=_FakeInspector(),
        popen_factory=lambda _argv, **_kwargs: process,
        clock_ns=clock_ns,
        killpg=killpg,
        cleanup_clock_ns=clock_ns,
        group_exists=lambda _pid: group_alive[0],
        sleeper=lambda _seconds: None,
        boot_id_reader=lambda: (
            '00000000-0000-0000-0000-000000000001'
        ),
    )

    with pytest.raises(CaptureError, match='deadline_exceeded'):
        capture.capture(1, expected_attestation=ATTESTATION)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.waited is True


def test_keyboard_interrupt_zeroizes_and_reaps_process_group(monkeypatch):
    """Guarantee cleanup for BaseException after a recorder has spawned."""
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    os.write(stdout_write, b'\x01\x00')

    class InterruptedProcess:
        pid = 444444
        returncode = None
        stdout = os.fdopen(stdout_read, 'rb', buffering=0)
        stderr = os.fdopen(stderr_read, 'rb', buffering=0)
        waited = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.waited = True
            if self.returncode is None:
                raise subprocess.TimeoutExpired('fake', timeout)
            return self.returncode

    process = InterruptedProcess()
    calls = [0]
    signals = []
    zeroized = []

    def clock_ns():
        calls[0] += 1
        if calls[0] == 3:
            raise KeyboardInterrupt()
        if calls[0] < 3:
            return calls[0]
        return calls[0] * 2000000000

    def killpg(_pid, sent_signal):
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            process.returncode = -signal.SIGKILL
            os.close(stdout_write)
            os.close(stderr_write)

    def recording_zeroize(buffer):
        before = bytes(buffer)
        zeroize(buffer)
        zeroized.append((before, bytes(buffer)))

    monkeypatch.setattr(
        'malbut_voice.audio_capture.zeroize',
        recording_zeroize,
    )
    capture = BoundedArecordCapture(
        _binding(),
        _policy(),
        _ProvenanceAuthority(),
        inspector=_FakeInspector(),
        popen_factory=lambda _argv, **_kwargs: process,
        clock_ns=clock_ns,
        killpg=killpg,
        cleanup_clock_ns=clock_ns,
        group_exists=lambda _pid: process.returncode is None,
        sleeper=lambda _seconds: None,
        boot_id_reader=lambda: (
            '00000000-0000-0000-0000-000000000001'
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        capture.capture(1, expected_attestation=ATTESTATION)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.waited is True
    assert any(before == b'\x01\x00' for before, _after in zeroized)
    assert all(set(after) <= {0} for _before, after in zeroized)


def test_clock_failure_after_spawn_uses_independent_fail_safe_teardown():
    """Signal and reap even when capture and cleanup clocks both fail."""
    process = _DeadlineProcess()
    signals = []
    capture_clock_calls = [0]

    def capture_clock():
        capture_clock_calls[0] += 1
        if capture_clock_calls[0] == 1:
            return 0
        raise CaptureError('clock_boottime_failed')

    def cleanup_clock():
        raise RuntimeError('/private/cleanup-clock')

    def killpg(_pid, sent_signal):
        signals.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            process.returncode = -signal.SIGKILL

    capture = BoundedArecordCapture(
        _binding(),
        _policy(),
        _ProvenanceAuthority(),
        inspector=_FakeInspector(),
        popen_factory=lambda _argv, **_kwargs: process,
        clock_ns=capture_clock,
        cleanup_clock_ns=cleanup_clock,
        killpg=killpg,
        group_exists=lambda _pid: process.returncode is None,
        sleeper=lambda _seconds: None,
        boot_id_reader=lambda: (
            '00000000-0000-0000-0000-000000000001'
        ),
    )

    with pytest.raises(CaptureError, match='clock_boottime_failed') as raised:
        capture.capture(1, expected_attestation=ATTESTATION)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.waited is True
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__traceback__ is None


def test_capture_rejects_changed_static_attestation_before_spawn():
    """Do not spawn when the second device check differs from preflight."""
    changed = replace(ATTESTATION, binary_inode=99)

    class ChangedInspector(_FakeInspector):
        def attest(self):
            return changed

    spawned = []
    authority = _ProvenanceAuthority()
    capture = BoundedArecordCapture(
        _binding(),
        _policy(),
        authority,
        inspector=ChangedInspector(),
        popen_factory=lambda *_args, **_kwargs: spawned.append(True),
        boot_id_reader=lambda: (
            '00000000-0000-0000-0000-000000000001'
        ),
    )

    with pytest.raises(CaptureError, match='attestation_changed'):
        capture.capture(1, expected_attestation=ATTESTATION)

    assert spawned == []


def test_capture_rechecks_policy_mutation_before_spawn():
    """Reject object-level command mutation before constructing a child."""
    policy = _policy()
    spawned = []
    capture = BoundedArecordCapture(
        _binding(),
        policy,
        _ProvenanceAuthority(),
        inspector=_FakeInspector(),
        popen_factory=lambda *_args, **_kwargs: spawned.append(True),
        boot_id_reader=lambda: (
            '00000000-0000-0000-0000-000000000001'
        ),
    )
    object.__setattr__(policy, 'sample_format', 'FLOAT_BE')

    with pytest.raises(ConfigSecurityError, match='sample_format'):
        capture.capture(1, expected_attestation=ATTESTATION)

    assert spawned == []
