"""Hold one process-owned speech lease across the resident LaunchService."""

import fcntl
import os
from pathlib import Path
import sys


def acquire(directory):
    """Lock before any speech process starts; process exit releases the lease."""
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(directory / 'resident-voice.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(descriptor)
        raise
    os.set_inheritable(descriptor, True)
    return descriptor


def main(argv=None):
    """Exec only the packaged speech launch, retaining the lock in its process."""
    try:
        descriptor = acquire(Path.home() / '.ros/malbut')
    except BlockingIOError:
        print('Resident voice is already owned by another launch', file=sys.stderr)
        return 2
    try:
        command = ['ros2', 'launch', 'malbut_bringup', 'speech.launch.py',
                   *(sys.argv[1:] if argv is None else argv)]
        os.execvp('ros2', command)
    finally:
        os.close(descriptor)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
