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


def run(command, startup_timeout_s, *, wait_for_ready=False, retry_delays=(5.0, 10.0)):
    """Forward output and retry only diagnosed CUDA OOM before the startup deadline."""
    if (not command or not math.isfinite(startup_timeout_s) or startup_timeout_s <= 0
            or len(retry_delays) > 2
            or any(not math.isfinite(delay) or delay < 0 for delay in retry_delays)):
        raise ValueError('invalid speech process configuration')
    interrupted = 0
    process = None

    def interrupt(signum, _frame):
        nonlocal interrupted
        interrupted = signum

    old_handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    deadline = time.monotonic() + startup_timeout_s
    try:
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
                env={**os.environ, 'PYTHONUNBUFFERED': '1'})
            ready = oom = finished = False
            pending = tail = b''
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    if interrupted:
                        _stop(process, interrupted)
                        return 128 + interrupted
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
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        return run(command, args.startup_timeout_s, wait_for_ready=args.wait_for_ready)
    except (ValueError, OSError) as error:
        print(f'speech_process_failed: {type(error).__name__}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
