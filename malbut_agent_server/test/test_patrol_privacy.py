"""Information survives release; RAM expiry never creates image backups."""

import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from malbut_agent_server.patrol_media import read_media, release_media
from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.patrol_observer import PatrolObservationWorker
from test_patrol_memory import ANALYSIS, ARGS, capture


@pytest.fixture
def media_module(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / 'malbut_autonomy/malbut_patrol'))
    from malbut_patrol import transient_media

    return transient_media


def volatile_capture(directory, media):
    path, metadata = capture(directory)
    media.put(path.stem, {'.jpg': path.with_suffix('.jpg').read_bytes()})
    path.with_suffix('.jpg').unlink()
    metadata.update(media_storage='volatile_v1', media_expires_at=10**12)
    path.write_text(json.dumps(metadata))
    return path, metadata


def test_transient_bounds_release_expiry_and_shutdown_have_no_disk_fallback(
        tmp_path, media_module):
    clock = [100.]
    media = media_module.TransientMedia(tmp_path, max_bytes=128, clock=lambda: clock[0])
    try:
        path, _ = volatile_capture(tmp_path, media)
        assert read_media(path, '.jpg', 128).startswith(b'\xff\xd8')
        with pytest.raises(OSError, match='capacity'):
            media.put('b' * 32, {'.jpg': b'x' * 129})
        with pytest.raises(ValueError, match='too large'):
            read_media(path, '.jpg', 2)
        release_media(path)
        with pytest.raises(FileNotFoundError):
            read_media(path, '.jpg', 128)
        path, _ = volatile_capture(tmp_path, media)
        clock[0] += media_module.RETENTION_SECONDS + 1
        # Even a matching-looking file must not become a fallback for expired RAM.
        path.with_suffix('.jpg').write_bytes(b'disk must not be used')
        with pytest.raises(FileNotFoundError):
            read_media(path, '.jpg', 128)
        release_media(path)
        assert not list(tmp_path.rglob('*.jpg'))
    finally:
        media.close()
    with pytest.raises(FileNotFoundError):
        read_media(path, '.jpg', 128)


@pytest.mark.parametrize('outcome', ['success', 'failure', 'expired_without_key'])
def test_worker_releases_pixels_and_stores_only_information(tmp_path, outcome, media_module):
    store = PatrolMemoryStore(tmp_path, 'owner')
    media = media_module.TransientMedia(tmp_path)
    try:
        path, metadata = volatile_capture(tmp_path, media)
        if outcome == 'expired_without_key':
            metadata['media_expires_at'] = 1
            path.write_text(json.dumps(metadata))

        def analyze(*_):
            if outcome == 'failure':
                raise ValueError('synthetic analysis failure')
            return copy.deepcopy(ANALYSIS)

        observer = SimpleNamespace(model='offline-fixture', analyze=analyze,
                                   available=lambda: outcome != 'expired_without_key')
        worker = PatrolObservationWorker(store, observer, autostart=False)
        for _ in range(3 if outcome == 'failure' else 1):
            with store.connect() as db:
                db.execute('UPDATE observations SET retry_at=0')
            worker.run_once()
        with pytest.raises(FileNotFoundError):
            read_media(path, '.jpg', 128)
        reopened = PatrolMemoryStore(tmp_path, 'owner')
        result = reopened.search('owner', ARGS)
        assert bool(result['observations']) == (outcome == 'success')
        with store.connect() as db:
            row = db.execute('SELECT * FROM observations').fetchone()
            assert row['status'] == ('ready' if outcome == 'success' else 'failed')
            if outcome == 'success':
                columns = [r[1] for r in db.execute(
                    'PRAGMA table_info(patrol_observation_evidence)')]
                assert columns == ['observation_id', 'metadata_sha256', 'analysis_sha256']
                assert result['observations'][0]['summary'] == ANALYSIS['summary']
                assert 'similar_observations' not in result['observations'][0]
        assert not list(tmp_path.rglob('*.jpg'))
        assert not list(tmp_path.rglob('*.npy'))
    finally:
        media.close()


def test_failed_commit_keeps_source_available_for_retry(tmp_path, monkeypatch, media_module):
    store = PatrolMemoryStore(tmp_path, 'owner')
    media = media_module.TransientMedia(tmp_path)
    try:
        path, _ = volatile_capture(tmp_path, media)
        store.ingest()

        def broken_commit(*_):
            raise sqlite3.OperationalError('synthetic disk failure')

        monkeypatch.setattr(store, 'commit_evidence', broken_commit)
        with pytest.raises(sqlite3.OperationalError):
            store.complete(path.stem, ANALYSIS, 'fixture')
        assert read_media(path, '.jpg', 128).startswith(b'\xff\xd8')
        with store.connect() as db:
            row = db.execute('SELECT * FROM observations').fetchone()
            assert row['status'] == 'pending' and row['analysis'] is None
    finally:
        media.close()


def test_reobservation_uses_committed_information_after_all_pixels_are_released(
        tmp_path, media_module):
    from malbut_patrol.reobserve import read_results, unresolved_targets

    store = PatrolMemoryStore(tmp_path, 'owner')
    media = media_module.TransientMedia(tmp_path)
    try:
        path, metadata = volatile_capture(tmp_path, media)
        runs = tmp_path / 'runs'
        runs.mkdir()
        (runs / (metadata['patrol_id'] + '.json')).write_text(json.dumps({
            'patrol_id': metadata['patrol_id'], 'captures': [{
                'observation_id': path.stem,
                'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}))
        store.ingest()
        analysis = copy.deepcopy(ANALYSIS)
        analysis['objects'][0]['spatial'] = {
            'status': 'unknown', 'region': None, 'method': 'registered_rgbd',
            'reason': 'region_boundary', 'reobserve_hint': {
                'xy': [2., 2.], 'purpose': 'aim_only_not_object_position'}}
        store.complete(path.stem, analysis, 'fixture')
        with pytest.raises(FileNotFoundError):
            read_media(path, '.jpg', 128)
        args = (tmp_path, metadata['patrol_id'], metadata['map_id'], False)
        targets = unresolved_targets(read_results(*args))
        assert len(targets) == 1 and targets[0]['xy'] == [2., 2.]
        remembered = store.search('owner', ARGS)['observations'][0]
        assert remembered['objects'][0]['spatial']['status'] == 'unknown'
        assert 'reobserve_hint' not in remembered['objects'][0]['spatial']
        with store.connect() as db:
            db.execute('DELETE FROM patrol_observation_evidence')
        assert read_results(*args) == []
    finally:
        media.close()


def test_cleanup_error_does_not_downgrade_committed_information(
        tmp_path, media_module, monkeypatch):
    from malbut_agent_server import patrol_media

    store = PatrolMemoryStore(tmp_path, 'owner')
    media = media_module.TransientMedia(tmp_path)
    try:
        path, _ = volatile_capture(tmp_path, media)
        released = patrol_media.release_media

        def interrupted_release(_path):
            raise OSError('interrupted cleanup')

        monkeypatch.setattr(patrol_media, 'release_media', interrupted_release)
        observer = SimpleNamespace(model='fixture', available=lambda: True,
                                   analyze=lambda *_: copy.deepcopy(ANALYSIS))
        worker = PatrolObservationWorker(store, observer, autostart=False)
        assert worker.run_once()
        assert not worker.run_once()  # Never pay to analyze an already committed result again.
        assert store.search('owner', ARGS)['counts'] == {'ready': 1}
        assert store.search('owner', ARGS)['observations']
        released(path)
    finally:
        media.close()


def test_existing_ready_observation_remains_readable_with_verified_legacy_source(tmp_path):
    store = PatrolMemoryStore(tmp_path, 'owner')
    path, _ = capture(tmp_path)
    store.ingest()
    # A database created by main has ready rows but no information-only receipts.
    with store.connect() as db:
        db.execute("UPDATE observations SET status='ready', analysis=?, model='legacy'",
                   (json.dumps(ANALYSIS, ensure_ascii=False),))
    result = store.search('owner', ARGS)
    assert result['observations'][0]['media_retention'] == 'legacy'
    path.with_suffix('.jpg').write_bytes(b'changed')
    assert store.search('owner', ARGS)['observations'] == []


@pytest.mark.parametrize('storage,expiry', [
    ('unknown', 100), ('volatile_v1', None), ('volatile_v1', float('inf')),
])
def test_invalid_volatile_policy_cannot_fall_back_to_legacy_disk(tmp_path, storage, expiry):
    from malbut_agent_server.patrol_memory import read_capture

    path, metadata = capture(tmp_path)
    metadata.update(media_storage=storage, media_expires_at=expiry)
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match='media policy'):
        read_capture(path)


def transient_rgbd_capture(directory, media, *, batched):
    """Build full socket evidence so post-inference checks exercise every source."""
    from test_patrol_spatial import capture as spatial_capture
    from test_patrol_lidar import scan

    path, metadata = spatial_capture.__wrapped__(directory)
    image = cv2.imencode('.jpg', np.full((100, 100, 3), 120, np.uint8))[1].tobytes()
    laser = scan()
    stream = io.BytesIO()
    np.save(stream, laser.pop('ranges'), allow_pickle=False)
    lidar = stream.getvalue()
    files = {'.jpg': image, '.lidar.npy': lidar}
    for suffix in ('.depth.npy', '.regions.json'):
        files[suffix] = path.with_suffix(suffix).read_bytes()
    metadata.update(
        image_sha256=hashlib.sha256(image).hexdigest(), media_storage='volatile_v1',
        media_expires_at=time.time() + 900,
        lidar=dict(laser, count=47, sha256=hashlib.sha256(lidar).hexdigest()))
    if batched:
        metadata['analysis_batch'] = 'patrol_images_v1'
    path.write_text(json.dumps(metadata, ensure_ascii=False))
    media.put(path.stem, files)
    for suffix in files:
        path.with_suffix(suffix).unlink(missing_ok=True)
    if batched:
        (directory / 'runs').mkdir()
        (directory / 'runs' / (metadata['patrol_id'] + '.json')).write_text(json.dumps({
            'version': 1, 'patrol_id': metadata['patrol_id'], 'outcome': 'completed',
            'capture_failures': 0, 'captures': [{'observation_id': path.stem,
                'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}))
    return path, metadata, files


def spatial_observer(*, batched, after_request):
    if batched:
        from test_patrol_batch import observer

        def mutate(response):
            after_request()
            return response

        return observer([], mutate=mutate)
    from test_patrol_spatial import ANALYSIS as spatial_analysis

    def analyze(*_):
        after_request()
        return copy.deepcopy(spatial_analysis)

    return SimpleNamespace(available=lambda: True, model='fixture', analyze=analyze)


@pytest.mark.parametrize('batched', [False, True])
def test_transient_spatial_bundle_reuses_reads_without_skipping_validation_stages(
        tmp_path, media_module, monkeypatch, batched):
    from collections import Counter
    from malbut_agent_server import patrol_media

    media = media_module.TransientMedia(tmp_path)
    try:
        path, _, _ = transient_rgbd_capture(tmp_path, media, batched=batched)
        reads = Counter()
        original = patrol_media.read_media

        def read(path, suffix, limit):
            reads[suffix] += 1
            return original(path, suffix, limit)

        monkeypatch.setattr(patrol_media, 'read_media', read)
        store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
        worker = PatrolObservationWorker(store, spatial_observer(
            batched=batched, after_request=lambda: None), autostart=False)
        assert worker.run_once()
        # Ingest, before API, after API; localization reuses that last checked bundle.
        assert reads == {'.jpg': 3, '.depth.npy': 3, '.regions.json': 3, '.lidar.npy': 3}
        result = store.search('owner', dict(ARGS, query='상자'))
        obj = result['observations'][0]['objects'][0]
        assert obj['spatial']['region'] == {'id': 'workroom', 'name': '작업실'}
        with pytest.raises(FileNotFoundError):
            read_media(path, '.jpg', 1024 * 1024)
        assert not list(tmp_path.rglob('*.jpg')) and not list(tmp_path.rglob('*.npy'))
    finally:
        media.close()


@pytest.mark.parametrize('batched', [False, True])
@pytest.mark.parametrize('fault', [
    'expiry', 'release', 'manifest_removed', 'metadata',
    '.jpg', '.depth.npy', '.regions.json', '.lidar.npy',
])
def test_evidence_changed_during_api_never_commits_cached_spatial_bundle(
        tmp_path, media_module, batched, fault):
    clock = [100.]
    media = media_module.TransientMedia(tmp_path, clock=lambda: clock[0])
    try:
        path, metadata, files = transient_rgbd_capture(tmp_path, media, batched=batched)

        def invalidate():
            if fault == 'expiry':
                clock[0] += media_module.RETENTION_SECONDS + 1
            elif fault == 'release':
                media.discard(path.stem)
            elif fault == 'manifest_removed':
                path.unlink()
            elif fault == 'metadata':
                changed = copy.deepcopy(metadata)
                changed['camera_pose']['x'] += 1
                path.write_text(json.dumps(changed, ensure_ascii=False))
            else:
                media.put(path.stem, {**files, fault: files[fault] + b'changed'})

        store = PatrolMemoryStore(tmp_path, 'owner', simulation=True)
        worker = PatrolObservationWorker(store, spatial_observer(
            batched=batched, after_request=invalidate), autostart=False)
        assert worker.run_once()
        with store.connect() as db:
            row = db.execute('SELECT status, analysis FROM observations').fetchone()
            assert row['status'] != 'ready' and row['analysis'] is None
            evidence_table = db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                        "AND name='patrol_observation_evidence'").fetchone()
            if evidence_table is not None:
                assert db.execute(
                    'SELECT count(*) FROM patrol_observation_evidence').fetchone()[0] == 0
        assert not list(tmp_path.rglob('*.jpg')) and not list(tmp_path.rglob('*.npy'))
    finally:
        media.close()
