"""Deferred retries use verified recent evidence, never yesterday's person position."""

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from malbut_patrol.reobserve import read_deferred_targets


NOW = 1794445200.


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


@pytest.fixture
def history(tmp_path):
    for name in ('captures', 'runs'):
        (tmp_path / name).mkdir()
    with sqlite3.connect(tmp_path / 'observations.sqlite3') as db:
        db.executescript('''
            CREATE TABLE owner(user_id TEXT);
            INSERT INTO owner VALUES ('owner');
            CREATE TABLE observations(id TEXT, user_id TEXT, captured_at REAL,
                                      metadata TEXT, analysis TEXT, status TEXT);
            CREATE TABLE patrol_observation_evidence(observation_id TEXT,
                         metadata_sha256 TEXT, analysis_sha256 TEXT);
            CREATE INDEX owner_time ON observations(user_id, captured_at DESC);
        ''')
    return tmp_path


def observation(history, serial, *, ago=60, kind='object', name='탁자', status='ready',
                located=False, parent=None, root=None, round_=None, patrol=None,
                owner='owner', **metadata_overrides):
    identity = f'{serial:032x}'
    metadata = {
        'observation_id': identity, 'patrol_id': patrol or f'{serial+1000:032x}',
        'map_id': 'map', 'map_frame': 'map', 'regions_sha256': 'regions',
        'simulated': True, 'media_storage': 'volatile_v1',
        'captured_at': datetime.fromtimestamp(NOW-ago, timezone.utc).isoformat(),
        'camera_pose': {'x': -2., 'y': 0., 'yaw': 0.},
    }
    if parent:
        metadata.update(reobserve_of=root or parent['patrol_id'], reobserve_round=round_ or 1,
                        reobserve_target={'observation_id': parent['observation_id'],
                                          'object_index': 0})
    metadata.update(metadata_overrides)
    analysis = {'objects': [{'name': name, 'kind': kind, 'confidence': 'high',
                            'spatial': {'status': 'located' if located else 'unknown',
                                        'region': {'id': 'living'} if located else None,
                                        'reobserve_hint': {
                                            'xy': [0., 0.],
                                            'purpose': 'aim_only_not_object_position'}}}]}
    content = json.dumps(metadata).encode()
    (history / 'captures' / (identity+'.json')).write_bytes(content)
    manifest = history / 'runs' / (metadata['patrol_id']+'.json')
    sealed = (json.loads(manifest.read_text()) if manifest.exists()
              else {'patrol_id': metadata['patrol_id'], 'captures': []})
    sealed['captures'].append({'observation_id': identity,
                              'manifest_sha256': hashlib.sha256(content).hexdigest()})
    manifest.write_text(json.dumps(sealed))
    with sqlite3.connect(history / 'observations.sqlite3') as db:
        db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?)',
                   (identity, owner, NOW-ago, content.decode(), json.dumps(analysis), status))
        if status == 'ready':
            db.execute('INSERT INTO patrol_observation_evidence VALUES (?,?,?)',
                       (identity, digest(metadata), digest(analysis)))
    return metadata


def targets(history, **kwargs):
    return read_deferred_targets(history, 'map', True, now=NOW, map_frame='map',
                                 regions_sha256='regions', **kwargs)


def test_recent_unknown_static_target_needs_no_photos(history):
    metadata = observation(history, 1)
    selected = targets(history)
    assert len(selected) == 1
    assert selected[0]['observation_id'] == metadata['observation_id']
    assert selected[0]['reobserve_of'] == metadata['patrol_id']
    assert selected[0]['reobserve_round'] == 1
    assert selected[0]['xy'] == [0., 0.]
    assert not list(history.rglob('*.jpg')) and not list(history.rglob('*.npy'))


@pytest.mark.parametrize('options', [
    {'kind': 'person'}, {'ago': 86401}, {'ago': -1}, {'map_id': 'other'},
    {'map_frame': 'other'}, {'regions_sha256': 'changed'}, {'simulated': False},
    {'owner': 'visitor'}, {'located': True}, {'status': 'pending'},
])
def test_stale_people_wrong_context_and_unfinished_rows_are_not_targets(history, options):
    observation(history, 1, **options)
    assert targets(history) == []


@pytest.mark.parametrize('status', ['pending', 'failed', 'ready'])
def test_a_linked_attempt_consumes_the_parent_even_before_new_analysis(history, status):
    root = observation(history, 1, ago=300)
    observation(history, 2, ago=60, parent=root, status=status, located=True)
    assert targets(history) == []


def test_only_fresh_unknown_retry_may_request_second_and_last_round(history):
    root = observation(history, 1, ago=300)
    child = observation(history, 2, ago=180, parent=root)
    selected = targets(history)
    assert len(selected) == 1
    assert selected[0]['observation_id'] == child['observation_id']
    assert selected[0]['reobserve_of'] == root['patrol_id']
    assert selected[0]['reobserve_round'] == 2
    assert targets(history, max_rounds=1) == []
    observation(history, 3, parent=child, root=root['patrol_id'], round_=2)
    assert targets(history) == []


def test_unlinked_same_name_is_not_assumed_to_be_the_same_object(history):
    original = observation(history, 1, ago=300)
    observation(history, 2, ago=60, located=True)
    assert targets(history)[0]['observation_id'] == original['observation_id']


def test_selects_only_one_root_and_caps_two_extra_captures(history):
    observation(history, 1, ago=300, name='old')
    root = observation(history, 2, ago=90, name='탁자')
    observation(history, 3, ago=80, name='상자', patrol=root['patrol_id'])
    observation(history, 4, ago=70, name='의자', patrol=root['patrol_id'])
    selected = targets(history, maximum=20)
    assert len(selected) == 2
    assert {t['reobserve_of'] for t in selected} == {root['patrol_id']}
    assert {t['name'] for t in selected} == {'상자', '의자'}


def test_unsealed_current_capture_does_not_hide_older_ready_work(history):
    old = observation(history, 1, ago=300)
    current = observation(history, 2, ago=60, status='pending')
    (history / 'runs' / (current['patrol_id']+'.json')).unlink()
    assert targets(history)[0]['observation_id'] == old['observation_id']


@pytest.mark.parametrize('corruption', [
    'capture', 'analysis', 'receipt', 'schema', 'invalid_json', 'symlink'])
def test_unverified_or_corrupt_data_cannot_initiate_reobservation(history, corruption):
    metadata = observation(history, 1)
    capture = history / 'captures' / (metadata['observation_id']+'.json')
    if corruption == 'capture':
        capture.write_text('{}')
    elif corruption == 'symlink':
        original = history / 'original.json'
        capture.rename(original)
        capture.symlink_to(original)
    else:
        with sqlite3.connect(history / 'observations.sqlite3') as db:
            if corruption == 'analysis':
                db.execute('UPDATE observations SET analysis=?', ('{"objects":[]}',))
            elif corruption == 'receipt':
                db.execute('DELETE FROM patrol_observation_evidence')
            elif corruption == 'schema':
                db.execute('DROP TABLE observations')
            elif corruption == 'invalid_json':
                db.execute('UPDATE observations SET metadata=?', ('{oops',))
    assert targets(history) == []


def test_forged_or_cyclic_retry_lineage_is_not_a_target(history):
    observation(history, 1, reobserve_of='f'*32, reobserve_round=1,
                reobserve_target={'observation_id': '1'*32, 'object_index': 0})
    assert targets(history) == []


def test_bound_does_not_rescan_all_historical_batches(history):
    for serial in range(1, 12):
        observation(history, serial, ago=serial, located=serial <= 8)
    # Only the eight newest batches are considered; old work is optional.
    assert targets(history) == []


def test_missing_or_non_sqlite_database_is_safe_empty(tmp_path):
    assert targets(tmp_path) == []
    (tmp_path / 'observations.sqlite3').write_text('not SQLite')
    assert targets(tmp_path) == []
