"""Verify camera encoding, bounded asynchronous capture and durable handoff."""

import json
import hashlib
import math
import gc
from threading import Event
import time
import weakref
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from malbut_patrol import photo_recorder
from malbut_patrol.coverage import CoverageGrid
from malbut_patrol.photo_recorder import PhotoRecorder, encode_image


def camera_image():
    # Two red RGB pixels followed by padding on each row.
    return SimpleNamespace(width=2, height=2, step=8, encoding='rgb8',
                           data=bytes([255, 0, 0, 255, 0, 0, 42, 42] * 2),
                           header=SimpleNamespace(
                               frame_id='camera_optical',
                               stamp=SimpleNamespace(sec=1792562400, nanosec=0)))


def begin(recorder):
    recorder.begin(CoverageGrid(np.zeros((10, 10)), 0.1, 0, 0), 'map', '/maps/home.geojson')


def offer(recorder, yaw=0.0, phase='OBSERVING', simulated=False):
    recorder._last_capture = 0
    return recorder.offer(camera_image(), x=1.0, y=2.0, yaw=yaw, room_names=['거실'],
                          phase=phase, simulated=simulated)


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_padded_rgb_rows_keep_correct_color_and_reject_invalid_images():
    decoded = cv2.imdecode(np.frombuffer(encode_image(camera_image()), dtype=np.uint8), 1)
    assert decoded.shape == (2, 2, 3)
    assert decoded[0, 0, 2] > 240 and decoded[0, 0, 0] < 15
    bad = camera_image()
    bad.encoding = '16UC1'
    with pytest.raises(ValueError):
        encode_image(bad)
    bad = camera_image()
    bad.data = b'bad'
    with pytest.raises(ValueError):
        encode_image(bad)


def test_sampling_records_initial_view_and_distinct_angles_with_a_hard_limit(tmp_path):
    recorder = PhotoRecorder(tmp_path, max_photos=3)
    begin(recorder)
    assert offer(recorder, phase='PLANNING')
    assert not offer(recorder)  # Same camera pose and direction.
    assert not offer(recorder, math.pi / 2, phase='NAVIGATING')
    assert offer(recorder, math.pi / 2)
    assert offer(recorder, math.pi)
    assert not offer(recorder, -math.pi / 2)
    recorder.close()
    status = recorder.status()
    assert status['queued'] == status['saved'] == 3 and status['failed'] == 0
    manifests = list((tmp_path / 'captures').glob('[a-f0-9]' * 32 + '.json'))
    assert len(manifests) == 3
    for path in manifests:
        metadata = json.loads(path.read_text())
        assert not path.with_suffix('.jpg').exists()
        assert metadata['media_storage'] == 'volatile_v1'
        assert path.stat().st_mode & 0o777 == 0o600
        assert metadata['capture_rooms'] == ['거실']
        assert metadata['camera_pose']['x'] == 1.0
        assert metadata['patrol_id'] == status['patrol_id']
        assert metadata['image_stamp']['sec'] == 1792562400


def test_idle_writer_does_not_retain_last_sensor_image_after_encoding(tmp_path):
    class CameraImage:
        pass

    image = CameraImage()
    image.__dict__.update(camera_image().__dict__)
    reference = weakref.ref(image)
    recorder = PhotoRecorder(tmp_path)
    begin(recorder)
    try:
        assert recorder.offer(image, x=1., y=2., yaw=0., room_names=['거실'],
                              phase='OBSERVING', simulated=True)
        del image
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if recorder.status()['saved'] == 1 and reference() is None:
                break
            gc.collect()
            time.sleep(.01)
        assert recorder.status()['saved'] == 1
        assert reference() is None
        assert not list(tmp_path.rglob('*.jpg'))
    finally:
        recorder.close()


def test_disk_writer_does_not_block_offer_and_pending_queue_is_bounded(tmp_path):
    entered, release = Event(), Event()
    recorder = PhotoRecorder(tmp_path)
    original = recorder._save

    def slow_save(image, metadata):
        entered.set()
        release.wait(3)
        original(image, metadata)

    recorder._save = slow_save
    begin(recorder)
    try:
        start = time.monotonic()
        assert offer(recorder)
        assert time.monotonic() - start < 0.5
        assert entered.wait(1)
        for n in range(1, 5):
            begin(recorder)
            assert offer(recorder)
        begin(recorder)
        assert not offer(recorder)
        assert recorder.status()['queued'] == 0
    finally:
        release.set()
        recorder.close()


def test_full_storage_reports_failure_without_a_committed_manifest(tmp_path):
    recorder = PhotoRecorder(tmp_path, max_bytes=1)
    begin(recorder)
    assert offer(recorder)
    recorder.close()
    assert recorder.status()['failed'] == 1
    assert not list((tmp_path / 'captures').glob('[a-f0-9]' * 32 + '.json'))


def test_new_patrol_has_distinct_identity_but_same_map(tmp_path):
    recorder = PhotoRecorder(tmp_path)
    begin(recorder)
    assert offer(recorder)
    first = recorder.status()['patrol_id']
    begin(recorder)
    assert offer(recorder)
    assert recorder.status()['patrol_id'] != first
    recorder.close()
    records = [json.loads(path.read_text()) for path in
               (tmp_path / 'captures').glob('[a-f0-9]' * 32 + '.json')]
    assert len({record['map_id'] for record in records}) == 1
    assert len({record['patrol_id'] for record in records}) == 2


def test_batch_is_sealed_only_after_patrol_finishes_and_all_writes_complete(tmp_path):
    entered, release = Event(), Event()
    recorder = PhotoRecorder(tmp_path)
    original = recorder._save

    def delayed(image, metadata):
        entered.set()
        assert release.wait(3)
        original(image, metadata)

    recorder._save = delayed
    begin(recorder)
    identity = recorder.status()['patrol_id']
    manifest = tmp_path / 'runs' / (identity + '.json')
    try:
        assert offer(recorder)
        assert entered.wait(1)
        recorder.finish('canceled')
        assert not offer(recorder, math.pi)
        assert not manifest.exists()
    finally:
        release.set()
        recorder.close()
    saved = json.loads(manifest.read_text())
    assert saved['outcome'] == 'canceled' and len(saved['captures']) == 1
    item = saved['captures'][0]
    source = tmp_path / 'captures' / (item['observation_id'] + '.json')
    assert item['manifest_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert recorder.status()['batch_ready']


def test_restart_seals_committed_photos_from_interrupted_patrol(tmp_path):
    recorder = PhotoRecorder(tmp_path)
    begin(recorder)
    assert offer(recorder)
    recorder.close()
    identity = recorder.status()['patrol_id']
    manifest = tmp_path / 'runs' / (identity + '.json')
    manifest.unlink()  # Disk state after committed photos but before final run publication.
    manifest.with_suffix('.json.tmp').write_bytes(b'{"unfinished":')
    recovered = PhotoRecorder(tmp_path)
    recovered.close()
    saved = json.loads(manifest.read_text())
    assert saved['outcome'] == 'interrupted' and len(saved['captures']) == 1
    assert saved['capture_failures'] is None


def test_held_analysis_waits_across_run_transitions_then_releases_all_batches(tmp_path):
    recorder = PhotoRecorder(tmp_path)
    recorder.hold_analysis()
    try:
        begin(recorder)
        first = recorder.identity()['patrol_id']
        assert offer(recorder)
        wait_until(lambda: recorder.status()['saved'] == 1)
        recorder.finish()
        begin(recorder)
        second = recorder.identity()['patrol_id']
        assert offer(recorder)
        wait_until(lambda: recorder.status()['saved'] == 1)
        recorder.finish()
        # A settled capture and the next run's begin both used to publish here.
        recorder._flush_batches()
        assert not list((tmp_path / 'runs').glob('*.json'))
        assert recorder.status()['analysis_held']
        assert recorder.status()['analysis_pending_batches'] == 2
        assert len(recorder._media._items) == 2
        assert not list(tmp_path.rglob('*.jpg'))
        recorder.release_analysis()
        wait_until(lambda: len(list((tmp_path / 'runs').glob('*.json'))) == 2)
        assert not recorder.status()['analysis_held']
        for identity in (first, second):
            batch = json.loads((tmp_path / 'runs' / (identity + '.json')).read_text())
            assert batch['outcome'] == 'completed'
            assert len(batch['captures']) == 1
        # Already released batches retain pixels for the consumer until normal
        # release/expiry, rather than close's held-data discard path.
        assert len(recorder._media._items) == 2
    finally:
        recorder.close()


def test_release_seals_open_batch_but_waits_for_accepted_encoding(tmp_path):
    entered, release = Event(), Event()
    recorder = PhotoRecorder(tmp_path)
    original = recorder._save

    def delayed(image, metadata):
        entered.set()
        assert release.wait(3)
        original(image, metadata)

    recorder._save = delayed
    recorder.hold_analysis()
    begin(recorder)
    manifest = tmp_path / 'runs' / (recorder.identity()['patrol_id'] + '.json')
    try:
        assert offer(recorder)
        assert entered.wait(1)
        recorder.release_analysis('aborted')
        assert not offer(recorder, math.pi)
        assert not manifest.exists()
        release.set()
        wait_until(manifest.exists)
        batch = json.loads(manifest.read_text())
        assert batch['outcome'] == 'aborted'
        assert len(batch['captures']) == 1
    finally:
        release.set()
        recorder.close()


@pytest.mark.parametrize('shutdown', [False, True])
def test_held_discard_removes_media_metadata_and_prevents_restart_recovery(tmp_path, shutdown):
    recorder = PhotoRecorder(tmp_path)
    recorder.hold_analysis()
    begin(recorder)
    assert offer(recorder)
    wait_until(lambda: recorder.status()['saved'] == 1)
    recorder.finish()
    if shutdown:
        recorder.close()
    else:
        recorder.discard_held_analysis()
    assert recorder.status()['discarded'] == 1
    assert recorder.status()['saved'] == 0
    assert not recorder._media._items
    assert not list((tmp_path / 'captures').glob('*.json'))
    assert not list((tmp_path / 'runs').glob('*.json'))
    recorder.close()
    recovered = PhotoRecorder(tmp_path)
    recovered.close()
    assert not list((tmp_path / 'runs').glob('*.json'))


def test_discard_during_encoding_drops_inflight_and_queued_captures(tmp_path):
    entered, release = Event(), Event()
    recorder = PhotoRecorder(tmp_path)
    original = recorder._save

    def delayed(image, metadata):
        entered.set()
        assert release.wait(3)
        original(image, metadata)

    recorder._save = delayed
    recorder.hold_analysis()
    begin(recorder)
    try:
        assert offer(recorder)
        assert entered.wait(1)
        assert offer(recorder, math.pi)
        recorder.discard_held_analysis()
        release.set()
        wait_until(lambda: recorder.status()['discarded'] == 2)
        assert recorder.status()['saved'] == 0
        assert recorder.status()['failed'] == 0
        assert not recorder._media._items
        assert not list((tmp_path / 'captures').glob('*.json'))
        assert not list((tmp_path / 'runs').glob('*.json'))
        # Starting another patrol does not inherit the previous discard state.
        recorder._save = original
        begin(recorder)
        assert offer(recorder)
        recorder.finish()
        wait_until(lambda: recorder.status()['batch_ready'])
        assert recorder.status()['saved'] == 1
    finally:
        release.set()
        recorder.close()


def test_run_context_matches_capture_map_frame_and_region_fingerprint(tmp_path):
    recorder = PhotoRecorder(tmp_path)
    try:
        assert recorder.context() is None
        begin(recorder)
        context = recorder.context()
        assert offer(recorder)
        wait_until(lambda: recorder.status()['saved'] == 1)
        metadata = json.loads(next((tmp_path / 'captures').glob('*.json')).read_text())
        for field in ('patrol_id', 'map_id', 'map_frame', 'regions_sha256'):
            assert context[field] == metadata[field]
    finally:
        recorder.close()


@pytest.mark.parametrize('folder', ['captures', 'runs'])
def test_slow_metadata_commit_does_not_block_navigation_capture(tmp_path, monkeypatch, folder):
    entered, release = Event(), Event()
    original = photo_recorder._atomic_write

    def delayed(path, data):
        if path.parent.name == folder:
            entered.set()
            assert release.wait(3)
        original(path, data)

    monkeypatch.setattr(photo_recorder, '_atomic_write', delayed)
    recorder = PhotoRecorder(tmp_path)
    begin(recorder)
    try:
        assert offer(recorder)
        recorder.finish()
        assert entered.wait(1)
        begin(recorder)
        start = time.monotonic()
        assert offer(recorder)
        assert time.monotonic() - start < .5
    finally:
        release.set()
        recorder.close()
