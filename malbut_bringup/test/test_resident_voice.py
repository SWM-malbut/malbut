"""Verify resident process ownership without starting ROS or microphone capture."""

import os
import subprocess
import sys

import pytest

from malbut_bringup.resident_voice import acquire


def test_voice_lease_rejects_second_owner_and_releases_on_exit(tmp_path):
    """Two cloud launches cannot start duplicate resident speech groups."""
    descriptor = acquire(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            acquire(tmp_path)
        assert os.get_inheritable(descriptor)
        child = subprocess.run([
            sys.executable, '-c',
            'import sys; from malbut_bringup.resident_voice import acquire; acquire(sys.argv[1])',
            str(tmp_path)], capture_output=True)
        assert child.returncode != 0
    finally:
        os.close(descriptor)
    replacement = acquire(tmp_path)
    os.close(replacement)
