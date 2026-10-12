"""Distinguish a confirmed abnormal exit from a backend still running."""

import signal
import subprocess
from unittest.mock import Mock

import pytest

from malbut_autoslam.slam_process import SlamProcess


def test_clean_stop_waits_and_releases_process():
    """SIGINT completion, not just signal delivery, releases map ownership."""
    owner = SlamProcess(['unused'])
    process = Mock(returncode=0)
    process.poll.side_effect = [None, 0, 0]
    owner._process = process
    owner.stop()
    process.send_signal.assert_called_once_with(signal.SIGINT)
    process.wait.assert_called_once_with(timeout=10.0)
    assert not owner.alive
    owner.stop()


def test_abnormal_exit_is_reported_but_confirmed_dead():
    """The Action must fail, but AMCL can regain /map after a dead backend."""
    owner = SlamProcess(['unused'])
    owner._process = Mock(returncode=-signal.SIGABRT)
    owner._process.poll.return_value = -signal.SIGABRT
    with pytest.raises(RuntimeError, match='exited abnormally'):
        owner.stop()
    assert not owner.alive
    owner.stop()


def test_unconfirmed_stop_keeps_process_ownership():
    """No map handoff is allowed when even SIGKILL exit is unconfirmed."""
    owner = SlamProcess(['unused'])
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired('unused', 1)
    owner._process = process
    with pytest.raises(RuntimeError, match='exit is unconfirmed'):
        owner.stop()
    assert owner.alive
    assert [call.args[0] for call in process.send_signal.call_args_list] == [
        signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
