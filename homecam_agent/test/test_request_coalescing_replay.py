"""Replay must distinguish source evidence frames from actual dispatch time."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from experimental_request_coalescing import CoalescingConfig  # noqa: E402
from experimental_request_dedup import RequestDedupExperiment  # noqa: E402
from replay_request_coalescing import replay, source_aligned  # noqa: E402
from replay_pose_stability import semantic  # noqa: E402
from test_request_dedup import candidate, observation, row  # noqa: E402


def saved(frames, missing=()):
    out, legacy = [], RequestDedupExperiment()
    for f in frames:
        obs = [observation()] if f in missing else None
        cs = [candidate()] if f == 0 else [candidate('b')] if f == 2 else []
        raw = row(f, obs, cs, **({'b': 'missing'} if obs else {}))
        raw['case_id'] = 'C'
        requests, updates, _ = legacy.update(raw, image_size=(640, 400))
        r = copy.deepcopy(raw)
        r.update(retention_upstream_analysis=copy.deepcopy(raw['fall_analysis']),
                 retention_raw_candidates=[], retention_upstream_updates=[],
                 verification_updates=updates, verification_routes=dict(legacy.routes))
        r['fall_analysis']['candidates'] = requests
        out.append(r)
    return out


def execute(rows, config):
    original, dispatches = copy.deepcopy(rows), []
    diagnostics = replay(rows, {'C': dict(width=640, height=400)}, config, dispatches.append)
    assert original == rows
    scored = source_aligned(rows, dispatches, diagnostics)
    return dispatches, scored, diagnostics


def test_control_matches_old_requests_updates_and_routes_exactly():
    rows = saved([0, 2, 4, 6])
    dispatches, scored, _ = execute(rows, None)
    assert all(semantic(a) == semantic(b) for a, b in zip(rows, scored))
    assert all(e['delay_sec'] == 0 for e in dispatches)


def test_delayed_update_keeps_original_frame_and_records_later_dispatch():
    rows = saved([0, 2, 4, 6], missing=[4])
    dispatches, scored, _ = execute(rows, CoalescingConfig())
    assert len(dispatches) == 2
    event = dispatches[1]
    assert event['origin']['frame_index'] == 2 and event['decision_frame_index'] == 6
    assert event['dispatch_time_s'] == .6 and event['delay_sec'] == pytest.approx(.4)
    assert scored[1]['verification_updates'][0]['evidence'] == candidate('b')
    assert not scored[3]['verification_updates']
    assert 'NOT dispatch chronology' in scored[1]['scoring_view']


def test_deadline_callback_runs_between_frames_not_at_late_next_frame():
    dispatches, _, _ = execute(saved([0, 2, 4, 6, 8], missing=[4, 6]), CoalescingConfig())
    event = dispatches[1]
    assert event['reason'] == 'deadline' and event['dispatch_kind'] == 'request'
    assert event['dispatch_time_s'] == pytest.approx(.7)
    assert event['delay_sec'] == pytest.approx(.5)


def test_end_of_clip_does_not_drop_queued_candidate():
    dispatches, scored, _ = execute(saved([0, 2]), CoalescingConfig())
    assert len(dispatches) == 2 and dispatches[1]['reason'] == 'end_of_stream'
    assert scored[1]['fall_analysis']['candidates'] == [candidate('b')]


@pytest.mark.parametrize('damage', ['drop', 'duplicate', 'payload', 'origin_frame'])
def test_scoring_rejects_lost_rewritten_or_relocated_evidence(damage):
    rows = saved([0, 2, 4, 6])
    dispatches, _, diagnostics = execute(rows, CoalescingConfig())
    if damage == 'drop':
        dispatches.pop()
    elif damage == 'duplicate':
        dispatches.append(copy.deepcopy(dispatches[0]))
    elif damage == 'payload':
        dispatches[1]['origin']['candidate']['candidateKind'] = 'normal'
    else:
        dispatches[1]['origin']['frame_index'] = 4
    with pytest.raises(ValueError):
        source_aligned(rows, dispatches, diagnostics)
