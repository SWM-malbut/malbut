"""People must use visible body depth, retain history, and never inherit camera rooms."""

import copy
import json

import numpy as np
import pytest

from malbut_agent_server.patrol_memory import PatrolMemoryStore, validate_analysis
from malbut_agent_server.patrol_query import _answer_evidence
from malbut_agent_server.patrol_spatial import localize_objects
from test_patrol_spatial import capture as spatial_capture, REGIONS


PERSON = {'summary': '사람이 보인다.', 'objects': [{
    'name': '사람', 'kind': 'person', 'location': '사진 가운데', 'confidence': 'high',
    'image_bbox': [200, 100, 800, 900],
    'image_anchors': [[350, 350], [650, 350], [350, 650]]}], 'uncertainties': []}


@pytest.fixture
def capture(tmp_path):
    return spatial_capture.__wrapped__(tmp_path)


def body_depth():
    # Two legs in front of a wall; the center of the bounding box is background.
    depth = np.full((100, 100), 4., np.float32)
    depth[15:85, 28:43] = 2.
    depth[15:85, 58:73] = 2.1
    return depth


def test_person_body_patches_locate_across_rooms_and_survive_restart(capture):
    path, metadata = capture
    result = localize_objects(PERSON, metadata, REGIONS, body_depth())
    spatial = result['objects'][0]['spatial']
    assert spatial['region']['name'] == '작업실'
    assert spatial['depth_support'] == 'visible_body_patches'
    assert spatial['anchor_count'] == 3
    assert metadata['capture_rooms'] == ['거실']
    store = PatrolMemoryStore(path.parent.parent, 'owner', simulation=True)
    store.ingest()
    store.complete(path.stem, result, 'fixture')
    reopened = PatrolMemoryStore(store.directory, 'owner', simulation=True)
    recalled = reopened.search('owner', dict(query='사람', since=None, until=None, limit=5))
    assert recalled['observations'][0]['objects'] == result['objects']
    assert recalled['observations'][0]['capture_rooms'] == ['거실']


@pytest.mark.parametrize('fault', ['no_anchors', 'duplicates', 'missing', 'mixed',
                                   'outside_bbox', 'different_surface', 'boundary'])
def test_unreliable_person_depth_cannot_fall_back_to_bbox_or_camera(capture, fault):
    _, metadata = capture
    analysis, depth = copy.deepcopy(PERSON), body_depth()
    obj = analysis['objects'][0]
    if fault == 'no_anchors':
        obj.pop('image_anchors')
    elif fault == 'duplicates':
        obj['image_anchors'] = [[350, 350]]*3
    elif fault == 'missing':
        depth[15:85, 28:43] = np.nan
    elif fault == 'mixed':
        depth[:, ::2] = .7
    elif fault == 'outside_bbox':
        obj['image_anchors'][0] = [900, 500]
    elif fault == 'different_surface':
        depth[15:85, 58:73] = 4.
        obj['image_anchors'] = obj['image_anchors'][:2]
    elif fault == 'boundary':
        depth[15:85, 28:43] = .95
        depth[15:85, 58:73] = 1.05
    spatial = localize_objects(analysis, metadata, REGIONS, depth)['objects'][0]['spatial']
    assert spatial['status'] == 'unknown' and spatial['region'] is None


def test_two_valid_body_patches_can_tolerate_one_missing_patch(capture):
    _, metadata = capture
    depth = body_depth()
    depth[60:71, 30:41] = np.nan
    spatial = localize_objects(PERSON, metadata, REGIONS, depth)['objects'][0]['spatial']
    assert spatial['region']['name'] == '작업실' and spatial['anchor_count'] == 2


@pytest.mark.parametrize('outlier_depth,located', [(4., True), (.5, False)])
def test_body_consensus_only_rejects_farther_background(capture, outlier_depth, located):
    _, metadata = capture
    depth = body_depth()
    depth[15:85, 58:73] = outlier_depth
    spatial = localize_objects(PERSON, metadata, REGIONS, depth)['objects'][0]['spatial']
    assert (spatial['status'] == 'located') is located
    if located:
        assert spatial['region']['name'] == '작업실' and spatial['anchor_count'] == 2


@pytest.mark.parametrize('anchors', [[[500]], [[True, 500]], [[1001, 0]], [[500, 500]]*4])
def test_invalid_anchor_coordinates_are_rejected(anchors):
    analysis = copy.deepcopy(PERSON)
    analysis['objects'][0]['image_anchors'] = anchors
    with pytest.raises(ValueError, match='anchors'):
        validate_analysis(analysis)


def test_unknown_answer_evidence_excludes_camera_and_freeform_room_claims():
    original = {'observations': [{
        'observation_id': 'new', 'captured_at': '2026-10-22T09:00:00+00:00',
        'capture_rooms': ['촬영방'], 'capture_regions': [{'id': 'one', 'name': '촬영방'}],
        'camera_pose': {'x': 1, 'y': 2}, 'summary': '촬영방에 사람이 보인다.',
        'uncertainties': ['촬영방일 수도 있다.'], 'objects': [{
            'name': '사람', 'kind': 'person', 'location': '촬영방 통로',
            'confidence': 'high', 'image_bbox': [0, 0, 1000, 1000],
            'image_anchors': [[200, 200], [300, 300]],
            'spatial': {'status': 'unknown', 'region': None, 'reason': 'depth_missing'}}]}]}
    saved = copy.deepcopy(original)
    result = _answer_evidence(original)
    assert '촬영방' not in json.dumps(result, ensure_ascii=False)
    assert 'camera_pose' not in json.dumps(result)
    assert result['observations'][0]['objects'][0]['spatial']['status'] == 'unknown'
    assert result['observations'][0]['captured_at'] == original['observations'][0]['captured_at']
    assert original == saved  # Provenance must remain intact in durable storage.
    original['observations'][0]['objects'][0]['location'] = '소파 앞'
    assert _answer_evidence(original)['observations'][0]['objects'][0][
        'image_location'] == '소파 앞'


def test_latest_unknown_person_does_not_reuse_older_known_region():
    from test_patrol_memory import capture as legacy_capture
    from tempfile import TemporaryDirectory
    from pathlib import Path

    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = PatrolMemoryStore(root, 'owner')
        old, _ = legacy_capture(root, captured_at='2026-10-22T08:00:00+00:00')
        new, _ = legacy_capture(root, captured_at='2026-10-22T09:00:00+00:00')
        store.ingest()
        for path, region in [(old, {'id': 'workroom', 'name': '작업실'}), (new, None)]:
            analysis = copy.deepcopy(PERSON)
            analysis['objects'][0]['spatial'] = {
                'status': 'located' if region else 'unknown', 'region': region,
                'reason': None if region else 'person_depth_insufficient',
                'method': 'registered_rgbd'}
            store.complete(path.stem, analysis, 'fixture')
        evidence = _answer_evidence(store.search('owner', dict(
            query='사람', since=None, until=None, limit=5), include_recent_context=True))
        latest, previous = evidence['observations']
        assert latest['observation_id'] == new.stem
        assert latest['objects'][0]['spatial']['region'] is None
        assert previous['objects'][0]['spatial']['region']['name'] == '작업실'
