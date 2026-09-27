"""No model calls. Check posthoc person review cannot invent identity evidence."""
from pathlib import Path
import copy
from dataclasses import replace
import hashlib
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from replay_deferred_location_ablation import audit_links
from replay_deferred_discovery_links import replay
from experimental_box_association import box_subject_frame
from malbut_agent_server.domain.fall_monitoring import SubjectCheckState
from test_box_association_ablation import fixture as source_fixture


def fixture(frame=12, pose_box=None):
    box = [.1, .2, .3, .8]
    result = dict(link_events=[dict(kind='cloud_discovery_linked', discovery=dict(
        subject_key='pose:0:T1', association_link=dict(confirmed_at=1.0)))])
    rows = [dict(captured_at=1.0, candidate_payload=dict(tracks=[dict(
        targetTrackId='T1', box=pose_box or box)]))]
    tracking = dict(samples=[dict(captured_at=1.0, source_frame=frame, box=box)])
    annotation = dict(target_person_id='P1', persons=[
        dict(person_id='P1', role='target', boxes=[[12, 64, 80, 192, 320]]),
        dict(person_id='P2', role='other', boxes=[[12, 384, 80, 512, 320]])])
    meta = dict(width=640, height=400)
    criteria = dict(visible_coverage=.5, prediction_coverage=.25, margin=.1)
    return result, rows, tracking, annotation, meta, criteria


def test_unreviewed_frame_does_not_use_nearest_review():
    score = audit_links(*fixture(frame=13))[0]
    assert not score['exact_review_available']
    assert not score['correct_at_reviewed_instant']
    assert not score['wrong_person_at_reviewed_instant']


def test_no_link_is_not_a_correct_link():
    args = list(fixture())
    args[0]['link_events'] = []
    assert audit_links(*args) == []


def test_absent_pose_id_cannot_be_reviewed_as_correct():
    args = list(fixture())
    args[0]['link_events'][0]['discovery']['subject_key'] = 'pose:0:missing'
    with pytest.raises(ValueError, match='absent'):
        audit_links(*args)


def test_exact_review_of_same_person():
    score = audit_links(*fixture())[0]
    assert score['correct_at_reviewed_instant']
    assert not score['wrong_person_at_reviewed_instant']


def test_visual_target_and_other_pose_is_wrong_link():
    score = audit_links(*fixture(pose_box=[.6, .2, .8, .8]))[0]
    assert not score['correct_at_reviewed_instant']
    assert score['wrong_person_at_reviewed_instant']


def replay_fixture(tmp_path):
    rows, images, record, response, config, meta = source_fixture(anatomy=False)
    # One observed Cloud region cannot directly establish temporal identity.
    response['normalized_response']['findings'][0]['regions'] = [
        response['normalized_response']['findings'][0]['regions'][0]]
    folder = tmp_path / 'cached'
    (folder / 'images').mkdir(parents=True)
    (folder / 'masks').mkdir()
    samples = []
    for row in rows:
        index = row['source_frame']
        row['jpeg_sha256'] = hashlib.sha256(images[index]).hexdigest()
        (folder / 'images' / f'{index:05d}.jpg').write_bytes(images[index])
        mask = b'synthetic-test-mask'
        (folder / 'masks' / f'{index:05d}.png').write_bytes(mask)
        samples.append(dict(source_frame=index, captured_at=row['captured_at'],
                            box=row['candidate_payload']['tracks'][0]['box'],
                            mask_sha256=hashlib.sha256(mask).hexdigest()))
    for name, data in [('pose', rows), ('result', dict(samples=samples)),
                       ('input', record), ('response', response)]:
        (folder / f'{name}.json').write_text(json.dumps(data))
    case = dict(input=str(folder / 'input.json'), response=str(folder / 'response.json'),
                meta=meta, group='synthetic_not_accuracy')
    return case, folder, config


def test_location_boundary_changes_attachment_not_original_pose(tmp_path):
    case, folder, config = replay_fixture(tmp_path)
    original = (folder / 'pose.json').read_bytes()
    baseline, proposed = tmp_path / 'baseline', tmp_path / 'proposed'
    baseline.mkdir(mode=0o700)
    proposed.mkdir(mode=0o700)
    before = replay(case, folder, config, baseline)
    after = replay(case, folder, config, proposed, subject_frame_factory=box_subject_frame)
    assert before['attached_existing'] == 0
    assert after['attached_existing'] == 1
    assert after['created_person_cases'] == 0
    assert after['journal_reopen_verified'] and after['replay_idempotent']
    assert (folder / 'pose.json').read_bytes() == original
    output = json.loads((proposed / 'result.json').read_text())
    assert all(s['clearance'] == 'unknown' for f in output['location_timeline'] for s in f['subjects'])


def test_location_boundary_rejects_clearance_claim(tmp_path):
    case, folder, config = replay_fixture(tmp_path)
    destination = tmp_path / 'rejected'
    destination.mkdir(mode=0o700)

    def invalid_factory(row):
        frame, decisions = box_subject_frame(copy.deepcopy(row))
        return replace(frame, subjects=tuple(replace(p, state=SubjectCheckState.CLEAR)
                                             for p in frame.subjects)), decisions

    with pytest.raises(ValueError, match='never clear'):
        replay(case, folder, config, destination, subject_frame_factory=invalid_factory)
