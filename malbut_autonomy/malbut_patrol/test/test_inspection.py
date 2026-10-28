"""Check evidence completion, provenance, and camera/environment calibration."""
import hashlib
import json
import math
import sqlite3

import numpy as np
import pytest

from malbut_patrol.coverage import CoverageGrid, CoveragePlanner, CoverageProfile
from malbut_patrol.inspection import InspectionCalibration, PhotoCoverage


def evidence(tmp_path):
    folder = tmp_path / 'captures'
    folder.mkdir()
    photo = b'\xff\xd8test\xff\xd9'
    metadata = {'observation_id': 'a'*32, 'patrol_id': 'b'*32, 'map_id': 'c'*64,
                'image_sha256': hashlib.sha256(photo).hexdigest(),
                'camera_pose': {'x': 1., 'y': 1., 'yaw': 0.},
                'camera_fov_rad': math.pi/2, 'image_size': [640, 400], 'simulated': True}
    (folder / ('a'*32 + '.jpg')).write_bytes(photo)
    (folder / ('a'*32 + '.json')).write_text(json.dumps(metadata))
    connection = sqlite3.connect(tmp_path / 'observations.sqlite3')
    connection.execute('CREATE TABLE observations('
                       'id TEXT,status TEXT,model TEXT,metadata TEXT,analysis TEXT)')
    connection.execute('INSERT INTO observations VALUES (?,?,?,?,?)',
                       ('a'*32, 'pending', 'tested-model', json.dumps(metadata), None))
    connection.commit()
    planner = CoveragePlanner(CoverageGrid(np.zeros((30, 30)), .1, 0, 0),
                              CoverageProfile(1., .9, .5), (1., 1.), robot_clearance_m=0.)
    return metadata, connection, planner


def test_unanalyzed_and_failed_photos_never_credit_coverage(tmp_path):
    metadata, connection, planner = evidence(tmp_path)
    tracker = PhotoCoverage(tmp_path, 'analyzed_photos')
    tracker.update(planner, [metadata])
    assert planner.coverage_ratio == 0 and tracker.pending == 1
    connection.execute("UPDATE observations SET status='failed'")
    connection.commit()
    tracker.update(planner, [metadata])
    assert planner.coverage_ratio == 0 and tracker.status()['failed'] == 1
    connection.close()


def test_successful_photo_is_credited_once_and_can_skip_same_view(tmp_path):
    metadata, connection, planner = evidence(tmp_path)
    tracker = PhotoCoverage(tmp_path, 'analyzed_photos')
    connection.execute("UPDATE observations SET status='ready',analysis=?",
                       (json.dumps({'summary': 'Visible scene'}),))
    connection.commit()
    tracker.update(planner, [metadata])
    observed = planner.observed_area_m2
    assert observed > 0 and tracker.status()['credited'] == 1
    tracker.update(planner, [metadata])
    assert planner.observed_area_m2 == observed and tracker.pending == 0
    connection.close()


@pytest.mark.parametrize('failure', ['model', 'image', 'metadata'])
def test_mismatched_evidence_cannot_be_used(tmp_path, failure):
    metadata, connection, planner = evidence(tmp_path)
    calibration = InspectionCalibration(1., 'tested-model', 640, 400, math.pi/2, True)
    tracker = PhotoCoverage(tmp_path, 'analyzed_photos', calibration)
    stored = dict(metadata)
    if failure == 'metadata':
        stored['patrol_id'] = 'd'*32
    connection.execute("UPDATE observations SET status='ready',analysis=?,model=?,metadata=?",
                       (json.dumps({'summary': 'Scene'}), 'wrong-model' if failure == 'model'
                        else 'tested-model', json.dumps(stored)))
    connection.commit()
    if failure == 'image':
        (tmp_path/'captures'/('a'*32+'.jpg')).write_bytes(b'changed')
    tracker.update(planner, [metadata])
    assert planner.coverage_ratio == 0 and tracker.status()['rejected'] == 1
    connection.close()


def test_missing_database_stays_pending_without_creating_a_database(tmp_path):
    metadata, connection, planner = evidence(tmp_path)
    connection.close()
    (tmp_path/'observations.sqlite3').unlink()
    tracker = PhotoCoverage(tmp_path, 'analyzed_photos')
    tracker.update(planner, [metadata])
    assert tracker.pending == 1 and planner.coverage_ratio == 0
    assert not (tmp_path/'observations.sqlite3').exists()


@pytest.mark.parametrize('mismatch', [
    'environment', 'size', 'fov', 'range', 'validation', 'mount',
])
def test_calibration_rejects_another_camera_or_unvalidated_result(tmp_path, mismatch):
    value = {'version': 1, 'validated': True, 'range_m': 2., 'model': 'tested-model',
             'simulated': True, 'camera': {'width': 640, 'height': 400, 'hfov_rad': 1.29}}
    if mismatch == 'environment':
        value['simulated'] = False
    if mismatch == 'size':
        value['camera']['width'] = 1280
    if mismatch == 'fov':
        value['camera']['hfov_rad'] = 1.
    if mismatch == 'range':
        value['range_m'] = float('nan')
    if mismatch == 'validation':
        value['validated'] = False
    if mismatch == 'mount':
        value['camera']['height_m'] = .5
    path = tmp_path/'calibration.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        InspectionCalibration.load(path, simulated=True, width=640, height=400, fov_rad=1.29)
