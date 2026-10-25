"""Latest uncertainty and timestamped confirmed history must fit in one answer context."""

import json

import pytest

from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.patrol_query import _answer_evidence
from test_patrol_memory import capture, ARGS


def observation(room=None, verbose=False):
    item = {'name': '세탁기', 'kind': 'object', 'location': '사진 중앙', 'confidence': 'high',
            'spatial': {'status': 'located' if room else 'unknown',
                        'region': {'id': room, 'name': room} if room else None,
                        'map_position': None, 'method': 'registered_rgbd',
                        'reason': None if room else 'object_depth_insufficient'}}
    return {'summary': '관찰 ' * (190 if verbose else 1),
            'objects': [item] * (20 if verbose else 1), 'uncertainties': []}


def test_confirmed_history_survives_newer_unknowns_and_context_budget(tmp_path):
    store = PatrolMemoryStore(tmp_path, 'owner')
    known, _ = capture(tmp_path, captured_at='2026-10-25T10:00:00+00:00')
    unknown = []
    for minute in range(1, 9):
        p, _ = capture(tmp_path, captured_at=f'2026-10-25T10:{minute:02d}:00+00:00')
        unknown.append(p)
    store.ingest()
    store.complete(known.stem, observation('부엌'), 'fixture')
    for p in unknown:
        store.complete(p.stem, observation(verbose=True), 'fixture')
    result = store.search('owner', dict(ARGS, query='세탁기', limit=1),
                          include_recent_context=True)
    assert len(json.dumps(result, ensure_ascii=False)) <= 4000
    latest, = result['observations']
    assert latest['observation_id'] == unknown[-1].stem
    assert latest['objects'][0]['spatial']['region'] is None
    history, = result['last_confirmed_observations']
    assert history['region']['name'] == '부엌'
    assert history['captured_at'] == '2026-10-25T10:00:00+00:00'
    assert history['observation_id'] == known.stem
    answer = _answer_evidence(result)
    assert answer['history_semantics'] == (
        'past_confirmed_sightings_not_current_or_tracked_identity')
    assert 'capture_rooms' not in answer['observations'][0]
    assert answer['last_confirmed_observations'] == [history]
    # A later confirmed move supersedes the historical location, using capture time.
    store.complete(unknown[-2].stem, observation('거실'), 'fixture')
    result = store.search('owner', dict(ARGS, query='세탁기', limit=1))
    assert result['last_confirmed_observations'][0]['region']['name'] == '거실'


@pytest.mark.parametrize('fault', ['removed', 'changed', 'time_filter', 'different_object'])
def test_history_never_recovers_unverified_or_irrelevant_positions(tmp_path, fault):
    store = PatrolMemoryStore(tmp_path, 'owner')
    path, _ = capture(tmp_path, captured_at='2026-10-25T10:00:00+00:00')
    store.ingest()
    value = observation('부엌')
    if fault == 'different_object':
        value['objects'][0]['name'] = '옷장'
        value['summary'] = '세탁기 가까이에 옷장이 있다.'
    store.complete(path.stem, value, 'fixture')
    args = dict(ARGS, query='세탁기')
    if fault == 'removed':
        path.with_suffix('.jpg').unlink()
    elif fault == 'changed':
        path.with_suffix('.jpg').write_bytes(b'changed')
    elif fault == 'time_filter':
        args['since'] = '2026-10-25T10:01:00+00:00'
    assert not store.search('owner', args).get('last_confirmed_observations')
    with pytest.raises(PermissionError):
        store.search('other-owner', args)


def test_simultaneous_same_description_does_not_invent_a_unique_object(tmp_path):
    store = PatrolMemoryStore(tmp_path, 'owner')
    paths = [capture(tmp_path, captured_at='2026-10-25T10:00:00+00:00')[0]
             for _ in range(2)]
    store.ingest()
    for path, room in zip(paths, ['부엌', '거실']):
        store.complete(path.stem, observation(room), 'fixture')
    result = store.search('owner', dict(ARGS, query='세탁기', limit=1))
    assert {h['region']['name'] for h in result['last_confirmed_observations']} == {'부엌', '거실'}
    assert result['object_identity'] == 'untracked_visual_descriptions'
