"""Legacy erasure preserves proven memories and is restartable after failures."""

import fcntl
import json
import sqlite3
from pathlib import Path

import pytest

from malbut_agent_server.patrol_memory import PatrolMemoryStore
from malbut_agent_server.patrol_privacy import migrate
from test_patrol_memory import ANALYSIS, ARGS, capture


def legacy(directory):
    store = PatrolMemoryStore(directory, 'owner')
    path, metadata = capture(directory)
    store.ingest()
    with store.connect() as db:
        db.execute("UPDATE observations SET status='ready', analysis=?, model='legacy', "
                   "analyzed_at=1234", (json.dumps(ANALYSIS, ensure_ascii=False),))
    return store, path, metadata


def test_migration_preserves_memory_and_times_and_removes_all_legacy_media(tmp_path):
    store, path, metadata = legacy(tmp_path)
    pending, _ = capture(tmp_path)
    store.ingest()
    batches = tmp_path / 'batches'
    batches.mkdir()
    for folder in (path.parent, batches):
        for suffix in ('.jpg', '.jpeg', '.png', '.depth.npy', '.lidar.npy'):
            for ending in ('', '.tmp'):
                (folder / ('leftover' + suffix + ending)).write_bytes(b'raw')
    manifest = batches / 'keep.json'
    manifest.write_text('{}')
    report = migrate(store)
    assert report == {'converted': 1, 'unverifiable': 0, 'raw_files_deleted': 22}
    reopened = PatrolMemoryStore(tmp_path, 'owner')
    remembered, = reopened.search('owner', ARGS)['observations']
    assert remembered['captured_at'] == metadata['captured_at']
    assert remembered['summary'] == ANALYSIS['summary']
    assert remembered['media_retention'] == 'information_only'
    with store.connect() as db:
        row = db.execute('SELECT analyzed_at, model FROM observations WHERE id=?',
                         (path.stem,)).fetchone()
        assert tuple(row) == (1234, 'legacy')
        assert db.execute('SELECT error FROM observations WHERE id=?',
                          (pending.stem,)).fetchone()[0] == 'raw_media_removed'
    assert manifest.exists() and path.exists()
    assert migrate(store) == {'converted': 0, 'unverifiable': 0, 'raw_files_deleted': 0}


@pytest.mark.parametrize('damage', ['photo', 'metadata', 'analysis', 'receipt'])
def test_unverifiable_history_is_not_promoted_to_trusted_memory(tmp_path, damage):
    store, path, _ = legacy(tmp_path)
    if damage == 'photo':
        path.with_suffix('.jpg').write_bytes(b'corrupt')
    else:
        with store.connect() as db:
            if damage == 'receipt':
                store.commit_evidence(db, path.stem, ANALYSIS)
                db.execute("UPDATE observations SET analysis='{}'")
            else:
                db.execute(f"UPDATE observations SET {damage}='{{}}'")
    assert migrate(store)['unverifiable'] == 1
    assert not path.with_suffix('.jpg').exists()
    assert not store.search('owner', ARGS)['observations']


def test_failed_commit_preserves_raw_source_for_retry(tmp_path, monkeypatch):
    store, path, _ = legacy(tmp_path)
    original = store.commit_evidence

    def fail(*_):
        raise sqlite3.OperationalError('disk failure')

    monkeypatch.setattr(store, 'commit_evidence', fail)
    with pytest.raises(sqlite3.OperationalError):
        migrate(store)
    assert path.with_suffix('.jpg').exists()
    monkeypatch.setattr(store, 'commit_evidence', original)
    assert migrate(store)['converted'] == 1


def test_interrupted_erasure_keeps_receipt_and_can_resume(tmp_path, monkeypatch):
    store, path, _ = legacy(tmp_path)
    unlink = Path.unlink

    def fail(self, *args, **kwargs):
        if self.suffix == '.jpg':
            raise OSError('erasure interrupted')
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail)
    with pytest.raises(OSError, match='interrupted'):
        migrate(store)
    assert store.search('owner', ARGS)['observations'][0]['media_retention'] == 'information_only'
    monkeypatch.setattr(Path, 'unlink', unlink)
    assert migrate(store) == {'converted': 0, 'unverifiable': 0, 'raw_files_deleted': 1}


def test_worker_lock_prevents_concurrent_migration(tmp_path):
    store, path, _ = legacy(tmp_path)
    with (tmp_path / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            migrate(store)
    assert path.with_suffix('.jpg').exists()


@pytest.mark.parametrize('name', ['captures', 'batches', 'worker.lock'])
def test_directory_and_lock_links_are_rejected(tmp_path, name):
    outside = tmp_path / 'outside'
    outside.mkdir()
    photo = outside / 'keep.jpg'
    photo.write_bytes(b'unrelated')
    store = PatrolMemoryStore(tmp_path / 'memory', 'owner')
    (store.directory / name).symlink_to(outside)
    with pytest.raises(ValueError, match='symlinked'):
        migrate(store)
    assert photo.read_bytes() == b'unrelated'


def test_media_links_do_not_delete_external_targets(tmp_path):
    store = PatrolMemoryStore(tmp_path / 'memory', 'owner')
    outside = tmp_path / 'keep.jpg'
    outside.write_bytes(b'unrelated')
    captures = store.directory / 'captures'
    captures.mkdir()
    link = captures / 'old.jpg'
    link.symlink_to(outside)
    assert migrate(store)['raw_files_deleted'] == 1
    assert outside.read_bytes() == b'unrelated'
    assert not link.exists()


@pytest.mark.parametrize('metadata', ['[', 'null', '{"media_storage":"volatile_v1"}'])
def test_pending_ram_observations_survive_but_legacy_pending_is_failed(tmp_path, metadata):
    store = PatrolMemoryStore(tmp_path, 'owner')
    path, _ = capture(tmp_path)
    store.ingest()
    with store.connect() as db:
        db.execute('UPDATE observations SET metadata=?', (metadata,))
    migrate(store)
    with store.connect() as db:
        row = db.execute('SELECT status, error FROM observations').fetchone()
        expected = ('pending', None) if 'volatile_v1' in metadata else ('failed', 'raw_media_removed')
        assert tuple(row) == expected
    assert not path.with_suffix('.jpg').exists()
