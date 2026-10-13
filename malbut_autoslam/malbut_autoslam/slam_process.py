"""SLAM process lifetime owned by the AutoSLAM Action, never by the manager."""

import ctypes
import os
import signal
import subprocess
import sys
import time


def _die_with_parent(parent):
    ctypes.CDLL('libc.so.6', use_errno=True).prctl(1, signal.SIGTERM)
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGTERM)


class SlamProcess:
    """Start once per Action and confirm exit before returning map ownership."""

    def __init__(self, command, stop_timeout_s=10.0):
        self.command = command
        self.stop_timeout_s = stop_timeout_s
        self._process = None

    @property
    def alive(self):
        return self._process is not None and self._process.poll() is None

    def start(self):
        if self.alive:
            return
        self._process = subprocess.Popen(
            [sys.executable, '-m', 'malbut_autoslam.slam_process',
             str(os.getpid()), *self.command],
            stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.0)
        if not self.alive:
            raise RuntimeError(f'slam_toolbox exited during startup ({self._process.returncode})')

    def stop(self):
        if self._process is None:
            return
        for sig, timeout in ((signal.SIGINT, self.stop_timeout_s),
                             (signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
            if self._process.poll() is not None:
                break
            self._process.send_signal(sig)
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
        if self._process.poll() is None:
            raise RuntimeError('slam_toolbox exit is unconfirmed')
        returncode = self._process.returncode
        self._process = None
        if returncode != 0:
            raise RuntimeError(f'slam_toolbox exited abnormally ({returncode})')


def main():
    """Set parent-death ownership before exec, outside ROS executor threads."""
    # preexec_fn is unsafe after fork in a multithreaded ROS process. This
    # single-threaded helper sets the same kernel ownership then execs SLAM.
    _die_with_parent(int(sys.argv[1]))
    os.execv(sys.argv[2], sys.argv[2:])


if __name__ == '__main__':
    main()
