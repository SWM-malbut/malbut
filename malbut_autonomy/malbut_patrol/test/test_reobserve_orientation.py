"""Retry capture must not trust a position-only Nav2 success as camera alignment."""

import math
from threading import Event, RLock
from types import SimpleNamespace as NS

import pytest
from builtin_interfaces.msg import Time

from malbut_patrol.patrol_manager import PatrolManager


def test_heading_uses_actual_camera_origin_and_optical_forward():
    # Camera looks along map +X; its origin is offset from the robot goal.
    transform = NS(translation=NS(x=1., y=2.),
                   rotation=NS(x=-.5, y=.5, z=-.5, w=.5))
    stamp = Time(sec=1)
    seen = []

    def lookup(*args):
        seen.append(args)
        return NS(transform=transform)

    fixture = NS(image=NS(header=NS(stamp=stamp, frame_id='raw_camera')),
                 tf=NS(lookup_transform=lookup),
                 lock=RLock(),
                 get_clock=lambda: NS(now=lambda: NS(nanoseconds=1_200_000_000)),
                 settings={'camera_optical_frame': 'optical', 'sensor_timeout_s': 3.},
                 map_message=NS(header=NS(frame_id='map')))
    fixture.rgb_images = [fixture.image]
    error = PatrolManager._reobserve_heading_error(fixture, {'xy': [1., 3.]})
    assert error == pytest.approx(math.pi/2)
    assert seen[0][:2] == ('map', 'optical')


@pytest.mark.parametrize('errors,spin_ok,expected,turns', [
    ([0.05], True, True, 0),
    ([1.2, 0.04], True, True, 1),
    ([1.2, .3, .02], True, True, 2),
    ([1.2, .3, .2], True, False, 2),
    ([1.2], False, False, 1),
])
def test_rotation_is_bounded_and_failed_alignment_prevents_capture(
        errors, spin_ok, expected, turns):
    pending = iter(errors)
    requests = []

    def run(client, request, handle, timeout):
        requests.append(request)
        return spin_ok

    fixture = NS(_pump=lambda handle: None,
                 _reobserve_heading_error=lambda target, not_before: next(pending),
                 _run_child=run, spin_client=object(), stop_event=Event(),
                 get_clock=lambda: NS(now=lambda: NS(nanoseconds=1_000_000_000)),
                 image=NS(header=NS(stamp=NS(sec=2, nanosec=0))),
                 settings={'spin_time_allowance_s': 60., 'sensor_timeout_s': 3.})
    assert PatrolManager._orient_reobservation(fixture, None, {}) is expected
    assert len(requests) == turns
    if requests:
        assert requests[0].target_yaw == pytest.approx(1.2)
    assert fixture.phase == 'REORIENTING'
