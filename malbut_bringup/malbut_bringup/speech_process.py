"""Bound CUDA allocation retries before speech startup, never during capture."""

import argparse
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time


READY = b'malbut_speech_capture_ready'
STOPPING = b'malbut_speech_stopping'
CUDA_OOM = re.compile(
    rb'cudaMalloc[^\n]{0,160}(?:out of memory|cudaErrorMemoryAllocation)'
    rb'|CUDA error:\s*out of memory|CUDA_ERROR_OUT_OF_MEMORY', re.IGNORECASE)


def _stop(process, signum=signal.SIGTERM):
    """Reap the owned child and kill any remaining members of its process group."""
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _wait_for_microphone(python, device_index, interrupted):
    """Wait without loading ASR; discard hung probes and refresh the XFM route."""
    from malbut_bringup.speech_audio import shared_xfm_source

    waiting = False
    while not interrupted():
        environment = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        process = None
        try:
            if device_index == 0 and environment.get('MALBUT_SHARED_MICROPHONE'):
                source = shared_xfm_source(environment)
                environment.update(MALBUT_SHARED_MICROPHONE=source, PULSE_SOURCE=source)
            process = subprocess.Popen(
                [python, '-m', 'malbut_stt.microphone_probe', str(device_index)],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            deadline = time.monotonic() + 7.0
            while (process.poll() is None and not interrupted()
                   and time.monotonic() < deadline):
                time.sleep(0.05)
            if process.poll() == 0 and not interrupted():
                print('speech_microphone_ready', flush=True)
                return environment
        except (RuntimeError, OSError):
            pass
        finally:
            if process is not None:
                _stop(process)
        if not waiting:
            print('speech_waiting_for_microphone', flush=True)
            waiting = True
        resume = time.monotonic() + 5.0
        while time.monotonic() < resume and not interrupted():
            time.sleep(0.05)
    return None


def run(command, startup_timeout_s, *, wait_for_ready=False, retry_delays=(5.0, 10.0),
        microphone_device=None):
    """Forward output and retry only diagnosed CUDA OOM before the startup deadline."""
    if (not command or not math.isfinite(startup_timeout_s) or startup_timeout_s <= 0
            or len(retry_delays) > 2
            or (microphone_device is not None and (
                not wait_for_ready or type(microphone_device) is not int
                or microphone_device < -1))
            or any(not math.isfinite(delay) or delay < 0 for delay in retry_delays)):
        raise ValueError('invalid speech process configuration')
    interrupted = 0
    process = None

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = signum

    old_handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        environment = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        if microphone_device is not None:
            environment = _wait_for_microphone(command[0], microphone_device, lambda: interrupted)
            if environment is None:
                return 128 + interrupted
        # Missing USB hardware is not a model startup attempt or CUDA retry.
        deadline = time.monotonic() + startup_timeout_s
        for attempt, delay in enumerate((0.0, *retry_delays), start=1):
            if delay:
                print(f'speech_cuda_oom_retry attempt={attempt} delay_s={delay:g}', flush=True)
            resume = time.monotonic() + delay
            while time.monotonic() < resume and not interrupted:
                if time.monotonic() >= deadline:
                    print('speech_startup_timeout', flush=True)
                    return 124
                time.sleep(min(0.05, max(0, resume - time.monotonic())))
            if interrupted:
                return 128 + interrupted
            if time.monotonic() >= deadline:
                print('speech_startup_timeout', flush=True)
                return 124
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, bufsize=0,
                env=environment)
            ready = oom = finished = False
            shutdown_deadline = None
            source_check_at = time.monotonic() + 1.0
            pending = tail = b''
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    if interrupted:
                        _stop(process, interrupted)
                        return 128 + interrupted
                    if shutdown_deadline is not None and time.monotonic() >= shutdown_deadline:
                        print('speech_shutdown_timeout', flush=True)
                        _stop(process)
                        return 124
                    if (microphone_device == 0 and environment.get('MALBUT_SHARED_MICROPHONE')
                            and shutdown_deadline is None and time.monotonic() >= source_check_at):
                        from malbut_bringup.speech_audio import shared_xfm_source
                        try:
                            source = shared_xfm_source(environment)
                        except RuntimeError:
                            source = None
                        if source != environment['MALBUT_SHARED_MICROPHONE']:
                            print('speech_microphone_lost', flush=True)
                            _stop(process)
                            return 75
                        source_check_at = time.monotonic() + 1.0
                    if not ready and time.monotonic() >= deadline:
                        print('speech_startup_timeout', flush=True)
                        _stop(process)
                        return 124
                    for key, _ in selector.select(timeout=0.05):
                        chunk = os.read(key.fd, 32768)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                        combined = tail + chunk
                        oom = oom or bool(CUDA_OOM.search(combined))
                        tail = combined[-8192:]
                        lines = (pending + chunk).split(b'\n')
                        pending = lines.pop()
                        if (wait_for_ready and shutdown_deadline is None
                                and any(line.rstrip(b'\r') == STOPPING for line in lines)):
                            shutdown_deadline = time.monotonic() + 5.0
                        if (wait_for_ready and time.monotonic() < deadline
                                and any(line.rstrip(b'\r') == READY for line in lines)):
                            ready = True
                        # Bound a child's partial line too; never mistake a truncated
                        # suffix of a long log line for the exact readiness marker.
                        if len(pending) > 8192:
                            pending = b'\0' + pending[-8192:]
                    if process.poll() is not None and not finished:
                        _stop(process)
                        finished = True
            process.stdout.close()
            code = process.wait()
            code = code if code >= 0 else 128 - code
            if ready or (code == 0 and not wait_for_ready):
                return code
            if oom and attempt == len(retry_delays) + 1:
                print(f'speech_cuda_oom_exhausted attempts={attempt}', flush=True)
                return code or 1
            if not oom:
                return code or 1
        return 1
    finally:
        if process is not None:
            if process.poll() is None:
                _stop(process)
            if process.stdout is not None:
                process.stdout.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    """Supervise one preflight command or a readiness-marked speech runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--startup-timeout-s', type=float, default=120.0)
    parser.add_argument('--wait-for-ready', action='store_true')
    parser.add_argument('--microphone-device', type=int)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        return run(command, args.startup_timeout_s, wait_for_ready=args.wait_for_ready,
                   microphone_device=args.microphone_device)
    except (ValueError, OSError) as error:
        print(f'speech_process_failed: {type(error).__name__}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
