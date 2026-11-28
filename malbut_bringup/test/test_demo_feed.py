"""Exercise overlay timing and cross-topic playback correlation without a camera."""

from collections import OrderedDict
from types import SimpleNamespace

import pytest

from malbut_bringup.demo_overlay import aligned_people, observed_people
from malbut_bringup.demo_relay import DemoRelay


def test_interpolation_uses_exposure_time_and_only_matching_tracks():
    """A crossing or expired track must not inherit the previous person's joint."""
    a = {1: {'nose': (0.1, 0.9)}, 2: {'nose': (0.4, 0.1)}}
    b = {1: {'nose': (0.5, 0.5)}, 3: {'nose': (0.8, 0.1)}}
    samples = [(1.0, a), (1.25, b)]
    result = aligned_people(samples, 1.125)
    assert set(result) == {1}
    assert result[1]['nose'] == pytest.approx((0.3, 0.7))
    assert aligned_people(samples, 1.0) == a
    assert aligned_people(samples, 1.5) == {}


def test_predicted_pose_excluded():
    """The existence of a tracker record does not mean a person was observed."""
    assert observed_people({'persons': [{'trackId': 12, 'observed': False}]}) == {}


def test_speech_waits_for_playing_and_accepts_reordered_delivery():
    """Separate ROS topics may deliver PLAYING before the admitted text."""
    relay = object.__new__(DemoRelay)
    relay.requests, relay.started, relay.lines = OrderedDict(), OrderedDict(), OrderedDict()
    request = SimpleNamespace(playback_id='one', text='괜찮으세요?', request_type=2)
    status = SimpleNamespace(playback_id='one', state='accepted')
    relay._status(status)
    relay._request(request)
    assert not relay.lines
    status.state = 'playing'
    relay._status(status)
    relay._status(status)
    assert [line['text'] for line in relay.lines.values()] == ['괜찮으세요?']
    status.playback_id = request.playback_id = 'two'
    relay._status(status)
    relay._request(request)
    assert len(relay.lines) == 2


def test_overlay_image_keeps_stamp_encoding_and_marks_only_the_suspect():
    """Check the emitted pixel buffer including padded RGB input rows."""
    import numpy as np
    from malbut_bringup.demo_overlay import annotate

    width, height, step = 100, 80, 304
    frame = SimpleNamespace(encoding='rgb8', width=width, height=height, step=step,
                            data=bytes(height*step), header=SimpleNamespace(stamp=1234))
    people = {1: {'box_min': (.1, .1), 'box_max': (.4, .9),
                  'left_hip': (.2, .4), 'left_knee': (.2, .8)},
              2: {'box_min': (.5, .1), 'box_max': (.9, .9),
                  'left_hip': (.7, .4), 'left_knee': (.7, .8)}}
    image = annotate(frame, people, {1})
    pixels = np.frombuffer(image.data, dtype=np.uint8).reshape(height, width, 3)
    assert image.header.stamp == 1234
    assert image.step == width*3
    assert tuple(pixels[48, 20]) == (248, 77, 68)
    assert tuple(pixels[48, 69]) == (247, 249, 250)
    assert not pixels[8, 50].any()
