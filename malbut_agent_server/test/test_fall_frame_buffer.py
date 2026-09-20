import pytest

from malbut_agent_server.application.fall_frame_buffer import FallFrameBuffer
from malbut_agent_server.domain.fall_monitoring import RgbFrame


def frame(t):
    return RgbFrame(t, b'\xff\xd8example\xff\xd9')


def window(buffer, end=10, duration=10, images=3, age=2):
    return buffer.window(end=end, duration_s=duration,
                         max_images=images, max_age_s=age)


def test_buffer_accepts_rgb_without_any_yolo_detection_and_samples_endpoints():
    b = FallFrameBuffer(retention_s=20, max_bytes=10000, max_frames=100)
    for t in range(11):
        b.append(frame(t))
    w = window(b)
    assert [f.captured_at for f in w.frames] == [0, 5, 10]
    assert not w.history_incomplete
    assert window(b, images=1).frames == (frame(10),)


@pytest.mark.parametrize('capacity', ['bytes', 'frames', 'time'])
def test_eviction_is_bounded_and_missing_history_is_explicit(capacity):
    b = FallFrameBuffer(retention_s=1 if capacity == 'time' else 20,
                        max_bytes=22 if capacity == 'bytes' else 10000,
                        max_frames=2 if capacity == 'frames' else 100)
    for t in range(11):
        b.append(frame(t))
    w = window(b, images=20)
    assert [f.captured_at for f in w.frames] == [9, 10]
    assert w.history_incomplete
    assert b.stored_bytes == 22


def test_future_frames_are_not_returned_and_stale_frames_are_not_fresh():
    b = FallFrameBuffer(retention_s=20, max_bytes=10000, max_frames=100)
    b.append(frame(1))
    b.append(frame(10))
    assert window(b, end=2).frames == (frame(1),)
    with pytest.raises(ValueError, match='fresh RGB'):
        window(b, end=7)
    with pytest.raises(ValueError):
        window(b, end=40)
    assert b.stored_bytes == 0


def test_duplicate_rollback_and_oversize_frames_do_not_replace_valid_history():
    b = FallFrameBuffer(retention_s=20, max_bytes=22, max_frames=100)
    b.append(frame(10))
    for t in (10, 9):
        with pytest.raises(ValueError):
            b.append(frame(t))
    with pytest.raises(ValueError):
        b.append(RgbFrame(11, b'\xff\xd8' + b'x' * 30 + b'\xff\xd9'))
    assert window(b).frames == (frame(10),)
    b.clear()
    assert b.stored_bytes == 0
    b.append(frame(0))


@pytest.mark.parametrize('value', [-1, float('inf'), True])
def test_invalid_timestamp(value):
    with pytest.raises(ValueError):
        frame(value)


def test_bytes_never_appear_in_repr():
    b = FallFrameBuffer(retention_s=20, max_bytes=10000, max_frames=100)
    b.append(frame(10))
    assert 'example' not in repr(frame(10))
    assert 'example' not in repr(window(b))
