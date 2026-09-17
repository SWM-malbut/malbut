"""Exercise native-style crashes, retry limits and cancellation with real children."""

import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).parents[2]
OOM = 'cudaMalloc failed: out of memory'


@pytest.fixture
def child(tmp_path):
    """Use a separate interpreter so supervisor signals cannot affect pytest."""
    attempts = tmp_path / 'attempts'
    script = tmp_path / 'child.py'

    def start(body, *, ready=False, timeout=2.0, delays=(0.02, 0.03)):
        script.write_text(
            'import os, pathlib, resource, signal, subprocess, sys, time\n'
            'resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n'
            f'attempts = pathlib.Path({str(attempts)!r})\n'
            'with attempts.open("a") as stream:\n'
            '    stream.write(str(os.getpid()) + "\\n")\n'
            'attempt = len(attempts.read_text().splitlines())\n' + body)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'malbut_bringup') + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.Popen([
            sys.executable, '-c',
            'from malbut_bringup.speech_process import run; import sys; '
            f'sys.exit(run(sys.argv[1:], {timeout!r}, wait_for_ready={ready!r}, '
            f'retry_delays={delays!r}))', sys.executable, str(script),
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)

    yield start, attempts


def finish(process):
    """Keep a failed test from leaving a supervisor or its child behind."""
    try:
        output, _ = process.communicate(timeout=8)
        return process.returncode, output.decode()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=3)


def wait_line(process, expected):
    """Wait for an actual forwarded event rather than sleeping for startup."""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 5
        output = b''
        while time.monotonic() < deadline:
            if selector.select(timeout=0.1):
                chunk = os.read(process.stdout.fileno(), 32768)
                output = (output + chunk)[-32768:]
                if expected.encode() in output:
                    return
                assert chunk, 'supervisor exited before the expected event'
    pytest.fail(f'event did not arrive: {expected}')


def assert_stopped(pid):
    """Accept a terminated orphan briefly remaining as a zombie until init reaps it."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        status = Path(f'/proc/{pid}/stat')
        if status.is_file() and status.read_text().split()[2] == 'Z':
            return
        time.sleep(0.01)
    pytest.fail(f'child remains alive: {pid}')


def test_native_cuda_oom_restarts_once_then_preflight_succeeds(child):
    start, attempts = child
    code, output = finish(start(
        f'if attempt == 1:\n    print({OOM!r}, file=sys.stderr, flush=True)\n'
        '    os.abort()\nprint("preflight_ok", flush=True)\n'))
    assert code == 0
    assert len(attempts.read_text().splitlines()) == 2
    assert OOM in output and 'preflight_ok' in output
    assert 'speech_cuda_oom_retry attempt=2' in output


def test_cuda_oom_exhaustion_is_three_attempts_and_nonzero(child):
    start, attempts = child
    code, output = finish(start(f'print({OOM!r}, flush=True)\nos.abort()\n'))
    assert code == 128 + signal.SIGABRT
    assert len(attempts.read_text().splitlines()) == 3
    assert output.count('speech_cuda_oom_retry') == 2
    assert 'speech_cuda_oom_exhausted attempts=3' in output


def test_cuda_oom_before_large_crash_output_is_not_discarded(child):
    start, attempts = child
    code, output = finish(start(
        f'if attempt == 1:\n    os.write(1, {OOM.encode()!r} + b"\\n" + b"x" * 32768)\n'
        '    os.abort()\n'))
    assert code == 0
    assert len(attempts.read_text().splitlines()) == 2
    assert OOM in output


@pytest.mark.parametrize('body', [
    'print("microphone unavailable", flush=True)\nsys.exit(2)\n',
    'print("malloc failed: out of memory", flush=True)\nos.abort()\n',
    'print("NvMapMemAllocInternalTagged: error 12", flush=True)\nos.abort()\n',
])
def test_non_cuda_failure_or_generic_abort_is_not_retried(child, body):
    start, attempts = child
    code, output = finish(start(body))
    assert code != 0
    assert len(attempts.read_text().splitlines()) == 1
    assert 'speech_cuda_oom_retry' not in output


def test_runtime_can_retry_before_ready_but_never_after_ready(child):
    start, attempts = child
    code, output = finish(start(
        'if attempt == 2:\n    print("malbut_speech_capture_ready", flush=True)\n'
        f'print({OOM!r}, flush=True)\nos.abort()\n', ready=True))
    assert code == 128 + signal.SIGABRT
    assert len(attempts.read_text().splitlines()) == 2
    assert output.count('speech_cuda_oom_retry') == 1


def test_ready_runtime_outlives_startup_deadline(child):
    start, attempts = child
    code, output = finish(start(
        'print("malbut_speech_capture_ready", flush=True)\ntime.sleep(0.4)\n',
        ready=True, timeout=0.2))
    assert code == 0
    assert len(attempts.read_text().splitlines()) == 1
    assert 'speech_startup_timeout' not in output


@pytest.mark.parametrize('line', ['', 'prefix malbut_speech_capture_ready',
                                  'malbut_speech_capture_ready suffix'])
def test_missing_or_inexact_ready_marker_fails_startup(child, line):
    start, attempts = child
    code, _ = finish(start(f'print({line!r}, flush=True)\n', ready=True))
    assert code != 0
    assert len(attempts.read_text().splitlines()) == 1


def test_startup_timeout_terminates_child_and_descendant(child, tmp_path):
    start, attempts = child
    descendant = tmp_path / 'descendant'
    code, output = finish(start(
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
        f'pathlib.Path({str(descendant)!r}).write_text(str(p.pid))\n'
        'time.sleep(30)\n', timeout=0.3))
    assert code == 124
    assert 'speech_startup_timeout' in output
    assert_stopped(int(attempts.read_text().strip()))
    assert_stopped(int(descendant.read_text()))


def test_startup_deadline_includes_retry_delay(child):
    start, attempts = child
    code, output = finish(start(
        f'print({OOM!r}, flush=True)\nos.abort()\n', timeout=0.2, delays=(1.0, 1.0)))
    assert code == 124
    assert 'speech_startup_timeout' in output
    assert len(attempts.read_text().splitlines()) == 1


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('phase', ['child', 'retry_delay'])
def test_cancel_forwards_signal_reaps_child_and_never_restarts(child, signum, phase):
    start, attempts = child
    body = ('print("child_running", flush=True)\ntime.sleep(30)\n' if phase == 'child'
            else f'print({OOM!r}, flush=True)\nos.abort()\n')
    process = start(body, delays=(5.0, 10.0))
    try:
        wait_line(process, 'child_running' if phase == 'child' else 'speech_cuda_oom_retry')
        process.send_signal(signum)
        code, _ = finish(process)
        assert code == 128 + signum
        pids = attempts.read_text().splitlines()
        assert len(pids) == 1
        assert_stopped(int(pids[0]))
    finally:
        if process.poll() is None:
            process.terminate()
            finish(process)


def test_large_and_split_output_stays_live_and_preserves_exact_marker(child):
    start, attempts = child
    process = start(
        'sys.stdout.write("x" * 200000 + "\\nmalbut_speech_")\nsys.stdout.flush()\n'
        'time.sleep(0.03)\nprint("capture_ready", flush=True)\ntime.sleep(0.3)\n',
        ready=True, timeout=0.2)
    code, output = finish(process)
    assert code == 0
    assert output == 'x' * 200000 + '\nmalbut_speech_capture_ready\n'
    assert len(attempts.read_text().splitlines()) == 1


def test_module_entry_point_keeps_command_arguments_and_reports_launch_failure(tmp_path):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'malbut_bringup') + os.pathsep + env.get('PYTHONPATH', '')
    prefix = [sys.executable, '-m', 'malbut_bringup.speech_process',
              '--startup-timeout-s', '2', '--']
    success = subprocess.run(
        prefix + [sys.executable, '-c', 'import sys; print(sys.argv[1])', 'argument with spaces'],
        env=env, capture_output=True, text=True, timeout=5)
    assert success.returncode == 0
    assert success.stdout == 'argument with spaces\n'
    missing = subprocess.run(prefix + [str(tmp_path / 'missing')], env=env, capture_output=True,
                             text=True, timeout=5)
    assert missing.returncode == 2
    assert 'FileNotFoundError' in missing.stderr
