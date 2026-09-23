"""No camera/Cloud access; check independent fall-camera permission and liveness."""

from types import SimpleNamespace

import pytest

from homecam_detector.fall_pose_control import FallPoseControl


def status(**changes):
    return SimpleNamespace(**dict(dict(
        runtime_id='vlm-1', sequence=1, settings_applied=True,
        enabled=True, camera_enabled=True, accepting_images=True,
        cloud_consent=False,
    ), **changes))


def test_initial_off_and_cloud_consent_is_not_a_local_pose_gate():
    gate = FallPoseControl('vlm-1')
    assert not gate.active()
    assert gate.receive(status())
    assert gate.active()


@pytest.mark.parametrize('field', [
    'settings_applied', 'enabled', 'camera_enabled', 'accepting_images',
])
def test_any_local_permission_off_stops_processing(field):
    gate = FallPoseControl('vlm-1')
    gate.receive(status())
    gate.receive(status(sequence=2, **{field: False}))
    assert not gate.active()


def test_other_runtime_and_replayed_status_cannot_extend_lease():
    now = [100.]
    gate = FallPoseControl('vlm-1', clock=lambda: now[0])
    gate.receive(status())
    now[0] += 4.9
    assert gate.active()
    assert not gate.receive(status(runtime_id='old-vlm', sequence=99))
    assert not gate.receive(status())
    now[0] += .1
    assert not gate.active()
    assert gate.receive(status(sequence=2))
    assert gate.active()


def test_clock_regression_stops_pose():
    now = [100.]
    gate = FallPoseControl('vlm-1', clock=lambda: now[0])
    gate.receive(status())
    now[0] = 99.
    assert not gate.active()
