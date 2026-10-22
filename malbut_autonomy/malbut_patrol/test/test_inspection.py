"""Verify analyzed-photo coverage, provenance and bounded live capture."""

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_patrol.coverage import CoverageGrid, CoveragePlanner, CoverageProfile
from malbut_patrol.inspection import InspectionCalibration, InspectionEvidence
from malbut_patrol.photo_recorder import PhotoRecorder


CALIBRATION = Path(__file__).parents[1] / 'config/inspection_sim_20261022.json'


@pytest.fixture
def photo(tmp_path):
    calibration = InspectionCalibration(CALIBRATION, True)
    image = SimpleNamespace(width=640, height=400, step=1920, encoding='rgb8',
                            data=bytes([120] * 640 * 400 * 3),
                            header=SimpleNamespace(frame_id='optical',
                                                   stamp=SimpleNamespace(sec=1, nanosec=0)))
    recorder = PhotoRecorder(tmp_path, inspection=True)
    grid = CoverageGrid(np.zeros((30, 30)), 0.1, 0, 0)
    recorder.begin(grid, 'map', '')
    camera = calibration.camera_record(640, 400, calibration.camera['hfov_rad'], 0.12)
    assert recorder.offer(image, x=1.5, y=1.5, yaw=0.0, room_names=[], phase='OBSERVING',
                          simulated=True, inspection=camera)
    deadline = time.monotonic() + 3
    while recorder.status()['saved'] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert recorder.status()['saved'] == 1
    run, references = recorder.evidence()
    identity = references[0]['observation_id']
    path = tmp_path / 'captures' / (identity + '.json')
    metadata = path.read_text()
    with sqlite3.connect(tmp_path / 'observations.sqlite3') as connection:
        connection.executescript('''
            CREATE TABLE dataset (id INTEGER PRIMARY KEY, simulated INTEGER);
            INSERT INTO dataset VALUES (1, 1);
            CREATE TABLE owner (id INTEGER PRIMARY KEY, user_id TEXT);
            INSERT INTO owner VALUES (1, 'test-owner');
            CREATE TABLE observations (id TEXT, user_id TEXT, metadata TEXT, status TEXT,
                model TEXT, error TEXT, analysis TEXT);
        ''')
        connection.execute('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (identity, 'test-owner', metadata, 'pending', None, None, None))
    evidence = InspectionEvidence(tmp_path, calibration, run)
    try:
        yield recorder, evidence, references, path, image, camera, grid
    finally:
        recorder.close()


def ready(photo, **changes):
    """Represent the existing worker's committed analysis without an API call."""
    _, evidence, references, path, *_ = photo
    values = {'status': 'ready', 'model': evidence.calibration.model,
              'analysis': json.dumps({'summary': 'visible', 'objects': [], 'uncertainties': []})}
    values.update(changes)
    with sqlite3.connect(path.parent.parent / 'observations.sqlite3') as connection:
        connection.execute('UPDATE observations SET ' + ','.join(k + '=?' for k in values)
                           + ' WHERE id=?', [*values.values(), references[0]['observation_id']])


def test_pending_photos_do_not_credit_area_and_ready_evidence_is_counted_once(photo):
    recorder, evidence, references, _, _, _, grid = photo
    planner = CoveragePlanner(grid, CoverageProfile(1, .95, .5), (1.5, 1.5))
    assert not evidence.poll(references)
    assert evidence.pending == 1 and planner.observed_area_m2 == 0
    ready(photo)
    for view in evidence.poll(references):
        planner.mark_observed(*view)
    assert planner.observed_area_m2 > 0
    assert evidence.pending == 0
    assert not evidence.poll(references)
    assert not evidence.poll(references, verify_all=True)
    assert len(evidence.credited) == 1
    assert not recorder.status()['batch_ready']  # Analysis was accepted before run sealing.


@pytest.mark.parametrize('changes', [
    {'model': 'another-model'}, {'status': 'failed'}, {'status': 'simulated'},
    {'error': 'analysis_failed'}, {'metadata': '{}'},
])
def test_invalid_analysis_cannot_credit_coverage(photo, changes):
    _, evidence, references, *_ = photo
    ready(photo, **changes)
    with pytest.raises(ValueError, match='Inspection evidence rejected'):
        evidence.poll(references)
    assert not evidence.credited


@pytest.mark.parametrize('suffix', ['.jpg', '.json', '.regions.json'])
def test_changed_evidence_revokes_completion_even_after_initial_credit(photo, suffix):
    _, evidence, references, path, *_ = photo
    ready(photo)
    assert evidence.poll(references)
    path.with_suffix(suffix).write_bytes(b'changed')
    with pytest.raises(ValueError, match='Inspection evidence rejected'):
        evidence.poll(references, verify_all=True)


def test_other_run_map_and_environment_are_not_reused(photo):
    _, evidence, references, path, *_ = photo
    ready(photo)
    for field in ('patrol_id', 'map_id', 'map_frame'):
        wrong = InspectionEvidence(evidence.directory, evidence.calibration,
                                   dict(evidence.run, **{field: 'another'}))
        with pytest.raises(ValueError):
            wrong.poll(references)
    with sqlite3.connect(path.parent.parent / 'observations.sqlite3') as connection:
        connection.execute('UPDATE dataset SET simulated=0')
    with pytest.raises(ValueError, match='environment mismatch'):
        evidence.poll(references)


def test_calibration_binds_camera_and_cannot_enable_simulated_range_on_real_robot():
    with pytest.raises(ValueError, match='environment'):
        InspectionCalibration(CALIBRATION, False)
    calibration = InspectionCalibration(CALIBRATION, True)
    for camera in ((320, 400, 1.291543646, .12), (640, 400, .8, .12),
                   (640, 400, 1.291543646, .22), (640, 400, math.nan, .12)):
        with pytest.raises(ValueError, match='Camera'):
            calibration.camera_record(*camera)
    assert not calibration.recognition_validated


def test_metadata_rebinding_cannot_change_recorded_calibration(photo):
    _, evidence, references, path, *_ = photo
    data = json.loads(path.read_text())
    data['inspection']['calibration_sha256'] = '0' * 64
    path.write_text(json.dumps(data))
    references[0]['manifest_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    ready(photo, metadata=path.read_text())
    with pytest.raises(ValueError, match='calibration mismatch'):
        evidence.poll(references)


def test_live_capture_uses_quarter_second_half_fov_and_does_not_wait_for_run_end(photo):
    recorder, _, _, path, image, camera, _ = photo
    metadata = json.loads(path.read_text())
    assert 'analysis_batch' not in metadata
    assert recorder.max_photos == 160
    half_fov = camera['hfov_rad'] / 2

    def offer(yaw, phase='OBSERVING'):
        return recorder.offer(image, x=1.5, y=1.5, yaw=yaw, room_names=[], phase=phase,
                              simulated=True, inspection=camera)

    recorder._last_capture = time.monotonic() - .20
    assert not offer(half_fov + .01)
    recorder._last_capture = time.monotonic() - .30
    assert not offer(half_fov - .01)
    assert offer(half_fov + .01)
    recorder._last_capture = 0
    assert not offer(math.pi, phase='ANALYZING')
    recorder.max_photos = 2
    assert not offer(math.pi)


def test_missing_database_remains_pending(photo):
    _, evidence, references, path, *_ = photo
    (path.parent.parent / 'observations.sqlite3').unlink()
    assert evidence.poll(references) == []
    assert evidence.pending == 1


def test_deleted_database_cannot_reuse_previously_credited_results(photo):
    _, evidence, references, path, *_ = photo
    ready(photo)
    assert evidence.poll(references)
    (path.parent.parent / 'observations.sqlite3').unlink()
    assert not evidence.poll(references, verify_all=True)
    assert evidence.pending == 1


def test_covered_room_does_not_require_entry_but_walls_still_block():
    grid = CoverageGrid(np.zeros((30, 30)), .1, 0, 0)
    rooms = {'features': [{'type': 'Feature', 'properties': {'role': 'room', 'name': 'east'},
                          'geometry': {'type': 'Polygon', 'coordinates': [
                              [[1.5, 0], [3, 0], [3, 3], [1.5, 3], [1.5, 0]]]}}]}
    planner = CoveragePlanner(grid, CoverageProfile(5, .95, .5), (1.05, 1.55),
                              robot_clearance_m=0, rooms=rooms, require_room_entry=False)
    planner.mark_observed(1.05, 1.55, 0, 2 * math.pi)
    assert planner.complete
    assert not planner.summary()['rooms']['east']['physically_entered']
    assert planner.observation_gain(1.05, 1.55, 0, 2 * math.pi) == 0
    cells = grid.cells.copy()
    cells[:25, 15] = 100
    blocked = CoveragePlanner(CoverageGrid(cells, .1, 0, 0), CoverageProfile(5, .95, .5),
                              (1.05, 1.55), robot_clearance_m=0, rooms=rooms,
                              require_room_entry=False)
    blocked.mark_observed(1.05, 1.55, 0, 2 * math.pi)
    assert not blocked.complete
    assert blocked.summary()['rooms']['east']['coverage_ratio'] < .95
