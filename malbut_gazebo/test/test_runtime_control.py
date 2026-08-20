from pathlib import Path
import os
import subprocess
import time

import pytest

from malbut_gazebo.runtime_control import (
    supervisor_available,
    supervisor_path,
    write_runtime_request,
)


def test_runtime_request_is_atomic_bounded_and_delayed(tmp_path: Path):
    request = tmp_path / "private" / "mode-request"

    assert write_runtime_request(
        request, "mapping", delay_seconds=1.0
    ) is True
    mode, not_before = request.read_text(encoding="ascii").split()

    assert mode == "mapping"
    assert int(not_before) >= int(time.time())
    assert request.stat().st_mode & 0o077 == 0
    assert list(request.parent.iterdir()) == [request]


def test_runtime_request_rejects_unknown_modes(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported runtime mode"):
        write_runtime_request(tmp_path / "mode-request", "unsafe")


def test_runtime_request_is_optional():
    assert write_runtime_request(None, "navigation") is False


def _claim_supervisor(request: Path, pid: int, age_seconds: float = 0.0):
    marker = supervisor_path(request)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"{pid} {time.time() - age_seconds}\n", encoding="ascii"
    )
    return marker


def test_supervisor_is_available_while_the_claiming_process_runs(
    tmp_path: Path,
):
    request = tmp_path / "mode-request"
    _claim_supervisor(request, os.getpid())

    assert supervisor_available(request) is True


def test_supervisor_is_unavailable_without_a_claim(tmp_path: Path):
    assert supervisor_available(tmp_path / "mode-request") is False
    assert supervisor_available(None) is False


def test_supervisor_is_unavailable_once_the_claim_goes_stale(tmp_path: Path):
    request = tmp_path / "mode-request"
    _claim_supervisor(request, os.getpid(), age_seconds=120.0)

    assert supervisor_available(request) is False
    assert supervisor_available(request, max_age_seconds=600.0) is True


def test_supervisor_is_unavailable_when_the_claiming_process_is_gone(
    tmp_path: Path,
):
    request = tmp_path / "mode-request"
    finished = subprocess.Popen(["true"])
    finished.wait()
    _claim_supervisor(request, finished.pid)

    assert supervisor_available(request) is False


def test_supervisor_claim_rejects_unreadable_content(tmp_path: Path):
    request = tmp_path / "mode-request"
    marker = supervisor_path(request)
    marker.write_text("not-a-pid\n", encoding="ascii")

    assert supervisor_available(request) is False
