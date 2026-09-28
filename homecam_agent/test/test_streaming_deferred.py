"""No model/API calls. Streaming bounds and causal timing, not model accuracy."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from experimental_streaming_sam import IncrementalSam, ReceivedFrame, StreamQueue
from evaluate_streaming_deferred import response_due
from report_streaming_deferred import audit_timing


def frame(t=1.0, jpeg=b'jpg'):
    return ReceivedFrame(1, t, jpeg)


def test_fifo_preserves_capture_time_and_bytes():
    queue = StreamQueue()
    first, second = frame(), frame(1.25)
    assert queue.put(first, now=1.1) and queue.put(second, now=1.3)
    assert queue.bytes == 6 and queue.peak_frames == 2
    assert queue.pop() is first and queue.pop() is second
    assert queue.bytes == 0 and queue.pop() is None


@pytest.mark.parametrize('bad', [frame(2), frame(float('nan')), frame(1, b''),
                                frame('wrong'), ReceivedFrame(-1, 1, b'jpg')])
def test_invalid_input_stops_queue(bad):
    queue = StreamQueue()
    with pytest.raises(ValueError):
        queue.put(bad, now=1)
    assert queue.closed == 'invalid_frame' and not queue.frames


def test_duplicate_remains_invalid_after_pop():
    queue = StreamQueue()
    queue.put(frame(), now=1)
    queue.pop()
    with pytest.raises(ValueError):
        queue.put(frame(), now=2)


def test_gap_fails_closed_without_skipping_to_new_frame():
    queue = StreamQueue()
    queue.put(frame(), now=1)
    assert not queue.put(frame(1.75), now=2)
    assert queue.closed == 'capture_gap' and queue.bytes == 0


@pytest.mark.parametrize('limits', [dict(max_frames=1), dict(max_bytes=4)])
def test_capacity_stops_instead_of_silently_dropping(limits):
    queue = StreamQueue(**limits)
    queue.put(frame(), now=1)
    assert not queue.put(frame(1.25), now=2)
    assert queue.closed == 'queue_capacity' and not queue.frames


def test_camera_off_drops_queued_jpeg_references():
    queue = StreamQueue()
    queue.put(frame(), now=1)
    queue.stop('camera_off')
    assert queue.pop() is None and queue.bytes == 0
    assert not queue.put(frame(1.25), now=2)


def test_full_clip_reply_cannot_arrive_before_last_input_and_measured_delay():
    case = dict(seed=dict(source_frame=12), meta=dict(fps=24))
    record = dict(evidence=dict(source_times_s=[0, 5]))
    response = dict(elapsed_s=4)
    assert response_due(case, record, response, 'cached_causal') == 9
    assert response_due(case, record, response, 'early_fixture') == 1.25


def test_normal_fixture_has_no_invented_discovery():
    assert response_due(dict(seed=None), {}, {}, 'early_fixture') is None


def test_unknown_mode_rejected():
    with pytest.raises(ValueError, match='unknown playback mode'):
        response_due({}, {}, {}, 'typo')


@pytest.mark.parametrize('elapsed', [-1, float('nan'), float('inf')])
def test_invalid_cached_delay_rejected(elapsed):
    with pytest.raises(ValueError, match='invalid cached response time'):
        response_due({}, {}, dict(elapsed_s=elapsed), 'cached_causal')


def test_exact_seed_required_before_model_execution():
    session = IncrementalSam(None, seed_box=(.1, .1, .5, .8), seed_time=1)
    with pytest.raises(ValueError, match='exact seed'):
        session.step(frame(1.25))


@pytest.mark.parametrize('time', [1, .75, 1.75])
def test_model_continuity_checked_before_execution(time):
    session = IncrementalSam(None, seed_box=(.1, .1, .5, .8), seed_time=1)
    session.last_stamp = 1
    with pytest.raises(ValueError, match='continuity'):
        session.step(frame(time))


def test_session_limit_does_not_turn_into_unbounded_stream():
    session = IncrementalSam(None, seed_box=(.1, .1, .5, .8), seed_time=1)
    session.count = 64
    with pytest.raises(ValueError, match='model_capacity'):
        session.step(frame())


@pytest.mark.parametrize('capacity', [0, 65, True])
def test_invalid_session_capacity(capacity):
    with pytest.raises(ValueError, match='model capacity'):
        IncrementalSam(None, seed_box=(.1, .1, .5, .8), seed_time=1, max_frames=capacity)


def timing_fixture():
    return dict(mode='early_fixture', events=[], delivered=[dict(captured_at=1.0,
        source_frame=1, delivered_at=1.01)], samples=[dict(source_frame=1,
        observed_at=1.0, started_at=1.02, gpu_ready_at=1.05, delivered_at=1.06,
        age_s=.06, available_input_count=1)])


def test_causal_timing_audit():
    assert audit_timing(timing_fixture())['timing_consistent']


def test_timing_audit_rejects_future_model_inputs():
    fixture = timing_fixture()
    fixture['samples'][0]['available_input_count'] = 20
    with pytest.raises(ValueError, match='future model input'):
        audit_timing(fixture)


def test_timing_audit_rejects_processing_before_input():
    fixture = timing_fixture()
    fixture['samples'][0]['started_at'] = 1.0
    with pytest.raises(ValueError, match='noncausal'):
        audit_timing(fixture)


def test_stale_control_cannot_count_a_link_as_success():
    fixture = timing_fixture()
    fixture['mode'] = 'cached_causal'
    fixture['events'] = [dict(kind='cloud_discovery_linked')]
    with pytest.raises(ValueError, match='negative control'):
        audit_timing(fixture)


def test_camera_control_requires_cleared_buffers():
    fixture = timing_fixture()
    fixture.update(mode='camera_off', queue_closed='camera_off', camera_off_at=1.03,
                   final_history_frames=1, final_queue_bytes=0, final_buffer_bytes=0,
                   sam_state_released=True)
    fixture['samples'][0]['decision'] = dict(reason='unknown_tracking_session')
    with pytest.raises(ValueError, match='retained buffered data'):
        audit_timing(fixture)
    fixture['final_history_frames'] = 0
    assert audit_timing(fixture)['canceled_gpu_result_rejected']


def test_code_manifest_ignores_virtual_torch_module_paths(monkeypatch):
    from types import SimpleNamespace
    from evaluate_motion_subject_linking import code_hashes
    monkeypatch.setitem(sys.modules, '_fixture_virtual', SimpleNamespace(__file__='_ops.py'))
    hashes = code_hashes()
    assert hashes and all(Path(p).is_file() for p in hashes)
