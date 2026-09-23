"""Check the robot trial configuration without hardware or paid inference."""

import builtins
import json
from pathlib import Path

import pytest

from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import RgbFrame
from malbut_agent_server.fall_runtime import FallNodeSettings
from malbut_agent_server.ros_fall_monitor import main


REPO = Path(__file__).resolve().parents[2]
EXAMPLE = Path("malbut_agent_server/config/fall_runtime.example.json")


@pytest.mark.parametrize("prefix", ["", "malbut_test"])
def test_example_is_complete_and_preserves_agreed_policy(prefix):
    settings = FallNodeSettings.parse((REPO / prefix / EXAMPLE).read_text())
    assert settings.image_topic == "/depth_cam/rgb0/image_raw"
    assert settings.control_lease_s == 5
    policy = settings.policy
    assert (policy.clip_window_s, policy.max_images) == (5, 12)
    assert (policy.scan_interval_s, policy.idle_scan_interval_s,
            policy.person_hold_s) == (60, 300, 120)
    assert (policy.cloud_timeout_s, policy.max_rechecks) == (20, 2)
    expected_frames = settings.input_fps * settings.retention_s + 1
    assert settings.buffer_frames >= expected_frames


def test_robot_copy_matches_primary_example():
    primary = (REPO / EXAMPLE).read_bytes()
    assert primary == (REPO / "malbut_test" / EXAMPLE).read_bytes()


@pytest.mark.parametrize("execute,expected", [(False, 0), (True, 2)])
def test_example_validation_and_execution_guard_have_no_runtime_side_effects(
        tmp_path, monkeypatch, capsys, execute, expected):
    data = json.loads((REPO / EXAMPLE).read_text())
    data["journal_path"] = str(tmp_path / "private" / "events.sqlite")
    data["cloud_key_file"] = str(tmp_path / "not-read.key")
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(data))
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if (name.split(".")[0] in {"rclpy", "aiohttp"}
                or name.endswith("sqlite_fall_journal")):
            pytest.fail("validation/placeholder guard started the runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    args = ["--config", str(path)] + (["--execute"] if execute else [])
    assert main(args) == expected
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "not-read.key").exists()
    output = capsys.readouterr().out
    assert ("registered robot ID" if execute else "no Cloud call") in output


def test_noisy_rgb_fits_trial_buffer_and_selects_twelve_recent_frames():
    # Deterministic synthetic pixels, not a camera or a model call.
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    settings = FallNodeSettings.parse((REPO / EXAMPLE).read_text())
    pixels = np.random.default_rng(187).integers(
        0, 256, size=(400, 640, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    jpeg = bytes(encoded)
    buffer = FallFrameBuffer(
        retention_s=settings.retention_s, max_bytes=settings.buffer_bytes,
        max_frames=settings.buffer_frames)
    for index in range(151):
        buffer.append(RgbFrame(100 + index / settings.input_fps, jpeg))
    assert buffer.stored_bytes == 51 * len(jpeg)
    assert buffer.stored_bytes <= settings.buffer_bytes
    window = buffer.window(
        end=130, duration_s=settings.policy.clip_window_s,
        max_images=settings.policy.max_images,
        max_age_s=settings.policy.max_frame_age_s)
    times = [frame.captured_at for frame in window.frames]
    assert len(times) == 12
    assert times == sorted(set(times))
    assert (times[0], times[-1]) == (125, 130)
    assert not window.history_incomplete
