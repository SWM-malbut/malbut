"""Verify camera encoding, bounded asynchronous capture and durable handoff."""

import json
import math
from threading import Event
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

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
        assert path.with_suffix('.jpg').exists()
        assert path.stat().st_mode & 0o777 == 0o600
        assert metadata['capture_rooms'] == ['거실']
        assert metadata['camera_pose']['x'] == 1.0
        assert metadata['patrol_id'] == status['patrol_id']
        assert metadata['image_stamp']['sec'] == 1792562400


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
