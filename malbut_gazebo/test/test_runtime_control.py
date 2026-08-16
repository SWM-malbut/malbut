from pathlib import Path
import time

import pytest

from malbut_gazebo.runtime_control import write_runtime_request


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
