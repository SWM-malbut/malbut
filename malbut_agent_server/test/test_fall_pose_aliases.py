"""ID repair requires adjacent unique low-pose measurements, not elapsed time."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from malbut_agent_server.application.fall_detector_input import FallDetectorInput
from malbut_agent_server.application.fall_pose_aliases import LowPoseAliases
from malbut_agent_server.domain.fall_monitoring import SubjectFrame, SubjectCheckState, IncidentState
from malbut_agent_server.fall_runtime import event_metadata, apply_decision
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from test_cloud_fall_monitor import make, frame
from test_fall_cloud_association import pose, finding, reply, HELPER


def low(key='old', **changes):
    return replace(pose(key, **changes), state=SubjectCheckState.SUSPECTED)


def feed(aliases, stamp, people, **options):
    return aliases.observe(SubjectFrame(stamp, tuple(people), .5),
                           stationary=options.pop('stationary', True), **options)


def started():
    aliases = LowPoseAliases()
    for t in (1, 1.2, 1.4):
        feed(aliases, t, (low(),))
    return aliases


def test_adjacent_unambiguous_low_track_changes_keep_canonical_key():
    aliases = started()
    for n in range(20):
        key = f'new-{n}'
        mapped = feed(aliases, 1.6 + n * .2, (low(key),))
        assert mapped.subjects[0].subject_key == 'old'
        assert aliases.key(key) == 'old'
    assert len(aliases.evidence) == 20
    assert len(aliases.names) == 1
    assert all(e['iou'] == 1 and e['reason'] == 'continuous_low_pose_id_change'
               for e in aliases.evidence)


@pytest.mark.parametrize('fault', [
    'gap', 'missing', 'weak_old', 'weak_new', 'upright_old', 'upright_new',
    'unknown_new', 'moved', 'old_competitor', 'new_competitor', 'old_still_present',
    'camera_moving', 'new_motion', 'reset', 'short_history',
])
def test_unproven_identity_never_inherits_previous_alias(fault):
    aliases = started()
    people, stamp, options = (low('new'),), 1.6, {}
    if fault == 'gap':
        stamp = 2
    elif fault in {'missing', 'weak_old', 'upright_old', 'old_competitor'}:
        old = (() if fault == 'missing' else
               (low(usable=False),) if fault == 'weak_old' else
               (replace(low(), state=SubjectCheckState.CLEAR),) if fault == 'upright_old' else
               (low(), low('competitor', usable=False)))
        feed(aliases, 1.6, old)
        stamp = 1.8
    elif fault == 'weak_new':
        people = (low('new', usable=False),)
    elif fault in {'upright_new', 'unknown_new'}:
        state = SubjectCheckState.CLEAR if fault == 'upright_new' else SubjectCheckState.UNKNOWN
        people = (replace(low('new'), state=state),)
    elif fault == 'moved':
        people = (low('new', box=HELPER),)
    elif fault == 'new_competitor':
        people = (low('new'), low('competitor', usable=False))
    elif fault == 'old_still_present':
        people = (low('new'), low())
    elif fault == 'camera_moving':
        options['stationary'] = False
    elif fault == 'new_motion':
        options['motion_keys'] = {'new'}
    elif fault == 'reset':
        aliases.clear()
    elif fault == 'short_history':
        aliases = LowPoseAliases()
        feed(aliases, 1.4, (low(),))
    mapped = feed(aliases, stamp, people, **options)
    assert mapped.subjects[0].subject_key == 'new'
    assert not aliases.evidence


def test_two_people_and_reappearing_raw_id_are_not_given_one_identity():
    aliases = started()
    feed(aliases, 1.6, (low('new'),))
    with pytest.raises(ValueError, match='ambiguous canonical'):
        feed(aliases, 1.8, (low('new'), low('old', box=HELPER)))


def test_ten_minutes_of_id_churn_keep_one_help_incident_and_dialogue(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / 'homecam_agent/homecam_detector'))
    monkeypatch.syspath_prepend(str(root / 'homecam_agent/homecam_detector/test'))
    from homecam_detector.fall_candidate import FallCandidateDetector
    from test_fall_candidate import body, step

    m, clock, provider = make(clip_window_s=5)
    adapter = FallDetectorInput(m, max_source_age_s=2)
    adapter.configure(enabled=True, camera_enabled=True, cloud_consent=True, connected=True)
    coordinator = FallConfirmationCoordinator()
    detector = FallCandidateDetector()
    events, questions, raw_candidates = [], [], []
    provider.reply = reply(finding(body('lying').box))

    async def run():
        try:
            for n in range(3001):
                elapsed = n / 5
                clock.value, capture = 100 + elapsed, 1000 + elapsed
                adapter.rgb(frame(clock()).jpeg, capture=capture, frame_id='camera',
                            source_now=capture, now=clock())
                raw_key = f'person-{int((elapsed + 30) // 120)}'
                output = step(detector, {raw_key: body('upright' if n < 2 else 'lying')}, capture)
                output['frameId'] = 'camera'
                raw_candidates.extend(output['candidates'])
                adapter.candidates(json.dumps(output), source_now=capture, now=clock())
                await m.run_once()
                for event in m.drain_events():
                    events.append(dict(t=elapsed, kind=event.kind, incident=event.incident_id))
                    coordinator.receive(json.dumps(dict(event_metadata(event), boot_id=m.boot_id)))
                for request in tuple(coordinator.requests.values()):
                    questions.append(request.question_id)
                    assert coordinator.complete(request, situation_assessment='confirmed_incident',
                                                help_needed=True)
                for command in coordinator.drain_commands():
                    assert apply_decision(m, json.dumps(command))
            opened = [e for e in events if e['kind'] == 'incident_opened']
            assert len(raw_candidates) == 6
            assert len(adapter._pose_aliases.evidence) == 5
            assert len(opened) == len(questions) == 1
            assert m.incident(opened[0]['incident']).state is IncidentState.HELP_REQUIRED
            report = dict(duration_s=600, raw_pose_candidates=len(raw_candidates),
                          incidents=len(opened), dialogues=len(questions),
                          aliases=list(adapter._pose_aliases.evidence), events=events,
                          real_cloud_calls=0, real_audio_playbacks=0)
            (tmp_path / 'id-churn.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            coordinator.close()
            await m.close()
    asyncio.run(run())
