"""Exercise held RGB-D captures through the real post-return memory pipeline."""

import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_agent_server.patrol_media import read_media
from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.patrol_observer import PatrolObservationWorker
from test_patrol_batch import observer


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def held_capture(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / 'malbut_autonomy/malbut_patrol'))
    from malbut_patrol.coverage import CoverageGrid
    from malbut_patrol.photo_recorder import PhotoRecorder

    rooms = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'room_id': identity, 'name': name},
         'geometry': {'type': 'Polygon', 'coordinates': [
             [[x0, -2], [x1, -2], [x1, 2], [x0, 2], [x0, -2]]]}}
        for identity, name, x0, x1 in [('living', '거실', -3, 0),
                                      ('workroom', '작업실', 0, 3)]]}
    recorder = PhotoRecorder(tmp_path)
    try:
        recorder.hold_analysis()
        recorder.begin(CoverageGrid(np.zeros((60, 60)), .1, -3, -3),
                       'map', 'home.geojson', rooms)
        for index, y in enumerate((0., .8)):
            header = SimpleNamespace(frame_id='optical', stamp=SimpleNamespace(
                sec=42 + index, nanosec=0))
            image = SimpleNamespace(
                width=100, height=100, step=300, encoding='bgr8', header=header,
                data=np.full((100, 100, 3), (0, 0, 200), np.uint8).tobytes())
            depth = SimpleNamespace(
                width=100, height=100, step=400, encoding='32FC1', header=header,
                is_bigendian=False, data=np.full((100, 100), 2., np.float32).tobytes())
            info = SimpleNamespace(width=100, height=100, header=header,
                                   k=[100., 0., 50., 0., 100., 50., 0., 0., 1.],
                                   d=[], distortion_model='')
            # Optical +Z points along map +X: the camera is in the living room,
            # while the observed box surface is two metres ahead in the workroom.
            transform = SimpleNamespace(translation=SimpleNamespace(x=-1., y=y, z=.2),
                                        rotation=SimpleNamespace(x=-.5, y=.5, z=-.5, w=.5))
            recorder._last_capture = 0
            assert recorder.offer(
                image, x=-1., y=y, yaw=0., room_names=['거실'], phase='OBSERVING',
                simulated=True, spatial={'aligned': True, 'depth': depth, 'info': info,
                                         'transform': transform, 'optical_frame': 'optical'})
        recorder.finish()
        wait_until(lambda: recorder.status()['saved'] == 2)
        paths = sorted((tmp_path / 'captures').glob('*.json'))
        assert len(paths) == 2 and not list(tmp_path.rglob('*.jpg'))
        assert not list(tmp_path.rglob('*.npy'))
        yield recorder, paths
    finally:
        recorder.close()


def release_after_return(recorder, directory):
    recorder.release_analysis()
    run = directory / 'runs' / (recorder.identity()['patrol_id'] + '.json')
    wait_until(run.exists)


def test_return_releases_one_batch_without_changing_object_regions_or_retaining_pixels(
        tmp_path, held_capture):
    recorder, paths = held_capture
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []
    worker = PatrolObservationWorker(store, observer(calls), autostart=False)

    # Sealing the captures during movement must not enable either batch or
    # individual-image inference. Capture metadata alone may already be pending.
    assert not worker.run_once()
    assert not worker.run_once()
    assert not calls and not list((tmp_path / 'runs').glob('*.json'))
    with store.connect() as db:
        assert [row[0] for row in db.execute('SELECT status FROM observations')] == [
            'pending', 'pending']

    release_after_return(recorder, tmp_path)
    assert worker.run_once()
    assert len(calls) == 1 and not worker.run_once()
    content = calls[0]['input'][0]['content']
    assert sum(part['type'] == 'input_image' for part in content) == 2
    with store.connect() as db:
        rows = db.execute('SELECT * FROM observations').fetchall()
        assert len(rows) == 2 and all(row['status'] == 'ready' for row in rows)
        assert db.execute('SELECT count(*) FROM patrol_analysis_batches').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM patrol_observation_evidence').fetchone()[0] == 2
        for row in rows:
            assert json.loads(row['metadata'])['capture_rooms'] == ['거실']
            spatial = json.loads(row['analysis'])['objects'][0]['spatial']
            assert spatial['status'] == 'located'
            assert spatial['region'] == {'id': 'workroom', 'name': '작업실'}
            assert spatial['map_position']['x'] == pytest.approx(1.)

    for path in paths:
        for suffix in ('.jpg', '.depth.npy', '.regions.json'):
            with pytest.raises(FileNotFoundError):
                read_media(path, suffix, 4 * 1024 * 1024)
    assert not list(tmp_path.rglob('*.jpg')) and not list(tmp_path.rglob('*.npy'))
    reopened = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    result = reopened.search('owner', {'query': '작업실 상자', 'since': None,
                                       'until': None, 'limit': 5})
    assert len(result['observations']) == 2


def test_expiry_while_waiting_for_return_never_calls_model_or_saves_ready_memory(
        tmp_path, held_capture, monkeypatch):
    from malbut_agent_server import patrol_memory
    from malbut_patrol.transient_media import RETENTION_SECONDS

    recorder, paths = held_capture
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []
    worker = PatrolObservationWorker(store, observer(calls), autostart=False)
    assert not worker.run_once() and not calls
    # Advance both clocks past the original hard lifetime; do not rewrite the
    # manifests or renew their hashes when releasing the finished patrol.
    expired_wall = max(json.loads(path.read_text())['media_expires_at'] for path in paths) + 1
    expired_monotonic = time.monotonic() + RETENTION_SECONDS + 1
    recorder._media.clock = lambda: expired_monotonic
    monkeypatch.setattr(patrol_memory, 'time', SimpleNamespace(
        time=lambda: expired_wall, monotonic=time.monotonic))
    release_after_return(recorder, tmp_path)
    assert worker.run_once() and not calls
    assert not worker.run_once()
    with store.connect() as db:
        rows = db.execute('SELECT status, error FROM observations').fetchall()
        assert len(rows) == 2
        assert all(tuple(row) == ('failed', 'transient_capture_expired') for row in rows)
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                          "name='patrol_observation_evidence'").fetchone() is None
    for path in paths:
        with pytest.raises(FileNotFoundError):
            read_media(path, '.jpg', 4 * 1024 * 1024)
    assert not list(tmp_path.rglob('*.jpg')) and not list(tmp_path.rglob('*.npy'))


def test_post_return_commit_failure_rolls_back_batch_and_keeps_pixels_for_retry(
        tmp_path, held_capture, monkeypatch):
    recorder, paths = held_capture
    store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
    calls = []
    worker = PatrolObservationWorker(store, observer(calls), autostart=False)
    release_after_return(recorder, tmp_path)

    def fail_receipt(*_args):
        raise sqlite3.OperationalError('synthetic transaction failure')

    monkeypatch.setattr(store, 'commit_evidence', fail_receipt)
    assert worker.run_once() and len(calls) == 1
    with store.connect() as db:
        rows = db.execute('SELECT status, analysis FROM observations').fetchall()
        assert len(rows) == 2 and all(tuple(row) == ('pending', None) for row in rows)
        assert db.execute('SELECT count(*) FROM patrol_analysis_batches').fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                          "name='patrol_observation_evidence'").fetchone() is None
    for path in paths:
        assert read_media(path, '.jpg', 4 * 1024 * 1024).startswith(b'\xff\xd8')
    assert not list(tmp_path.rglob('*.jpg')) and not list(tmp_path.rglob('*.npy'))
