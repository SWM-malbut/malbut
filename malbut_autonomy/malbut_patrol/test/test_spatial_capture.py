"""Check the RGB/depth registration contract and durable asynchronous sidecars."""

import io
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from malbut_patrol.coverage import CoverageGrid
from malbut_patrol.photo_recorder import PhotoRecorder
from malbut_patrol.spatial_capture import encode_spatial


def sensors(encoding='32FC1', bigendian=False):
    header = NS(frame_id='optical', stamp=NS(sec=100, nanosec=0))
    image = NS(width=20, height=10, step=60, encoding='rgb8',
               data=bytes([200, 0, 0] * 200), header=header)
    dtype = np.dtype(('>' if bigendian else '<') + ('f4' if encoding == '32FC1' else 'u2'))
    values = np.full((10, 22), 2. if encoding == '32FC1' else 2000, dtype=dtype)
    depth = NS(width=20, height=10, step=22*dtype.itemsize, encoding=encoding,
               is_bigendian=int(bigendian), data=values.tobytes(), header=header)
    bundle = {'aligned': True, 'depth': depth, 'optical_frame': 'optical',
              'info': NS(width=20, height=10, k=[20., 0., 10., 0., 20., 5., 0., 0., 1.],
                         d=[0.] * 5, distortion_model='plumb_bob', header=header,
                         p=[20., 0., 10., 0., 0., 20., 5., 0., 0., 0., 1., 0.],
                         r=np.eye(3).ravel().tolist()),
              'transform': NS(translation=NS(x=-1., y=0., z=.2),
                              rotation=NS(x=-.5, y=.5, z=-.5, w=.5))}
    return image, bundle


@pytest.mark.parametrize('encoding', ['32FC1', '16UC1'])
@pytest.mark.parametrize('bigendian', [False, True])
def test_depth_units_padding_and_byte_order(encoding, bigendian):
    image, bundle = sensors(encoding, bigendian)
    spatial, data = encode_spatial(image, bundle)
    assert spatial['status'] == 'available'
    depth = np.load(io.BytesIO(data), allow_pickle=False)
    assert depth.shape == (10, 20) and depth.dtype == np.float32
    np.testing.assert_allclose(depth, 2.)


def test_unsynchronized_unregistered_or_uncalibrated_depth_is_not_saved():
    image, bundle = sensors()
    bundle['aligned'] = False
    assert encode_spatial(image, bundle) == ({'status': 'depth_not_registered'}, None)
    bundle['aligned'] = True
    bundle['depth'].header = NS(frame_id='optical', stamp=NS(sec=99, nanosec=0))
    assert encode_spatial(image, bundle)[0]['status'] == 'depth_unsynchronized'
    bundle['depth'].header = image.header
    bundle['depth'].width = 19
    assert encode_spatial(image, bundle)[0]['status'] == 'calibration_size_mismatch'
    bundle['depth'].width = 20
    bundle['info'].distortion_model = 'equidistant'
    assert encode_spatial(image, bundle)[0]['status'] == 'calibration_unsupported'
    bundle['info'].distortion_model = 'plumb_bob'
    bundle['depth'].header = NS(frame_id='other_optical', stamp=image.header.stamp)
    assert encode_spatial(image, bundle)[0]['status'] == 'registration_frame_mismatch'


@pytest.mark.parametrize('model,coefficients', [
    ('plumb_bob', [.00484363, -.03018760, .00011549, -.00060054, .01503610]),
    ('rational_polynomial', [.1, -.2, 0., 0., .03, .01, .02, .03]),
])
def test_raw_camera_distortion_is_persisted_without_warping_depth(model, coefficients):
    image, bundle = sensors()
    bundle['info'].distortion_model = model
    bundle['info'].d = coefficients
    spatial, data = encode_spatial(image, bundle)
    assert spatial['status'] == 'available'
    assert spatial['calibration'] == dict(
        image_space='raw', distortion_model=model, distortion_coefficients=coefficients)
    np.testing.assert_allclose(np.load(io.BytesIO(data), allow_pickle=False), 2.)


def test_rectified_image_uses_projection_matrix_and_never_reapplies_raw_distortion():
    image, bundle = sensors()
    bundle['image_rectified'] = True
    bundle['info'].d = [.3, -.1, .01, -.01, .02]
    bundle['info'].p[:3] = [25., 0., 9.]
    spatial, _ = encode_spatial(image, bundle)
    assert spatial['status'] == 'available'
    assert spatial['intrinsics'] == dict(fx=25., fy=20., cx=9., cy=5.)
    assert spatial['calibration'] == dict(
        image_space='rectified', distortion_model='none', distortion_coefficients=[])
    bundle['info'].p[3] = -.1
    assert encode_spatial(image, bundle)[0]['status'] == 'calibration_unsupported'


@pytest.mark.parametrize('bigendian', [False, True])
def test_mono16_requires_explicit_units_and_registered_frame_contract(bigendian):
    image, bundle = sensors('mono16', bigendian)
    bundle['depth'].header = NS(frame_id='physical_depth', stamp=image.header.stamp)
    assert encode_spatial(image, bundle)[0]['status'] == 'registration_frame_mismatch'
    bundle['depth_registered_frame'] = 'optical'
    assert encode_spatial(image, bundle)[0]['status'] == 'depth_unit_unknown'
    bundle['depth_scale_m'] = .001
    spatial, data = encode_spatial(image, bundle)
    assert spatial['status'] == 'available'
    assert spatial['depth_source_frame'] == 'physical_depth'
    assert spatial['depth_registered_frame'] == 'optical'
    assert spatial['depth_scale_m'] == .001
    np.testing.assert_allclose(np.load(io.BytesIO(data), allow_pickle=False), 2.)
    bundle['aligned'] = False
    assert encode_spatial(image, bundle)[0]['status'] == 'depth_not_registered'


@pytest.mark.parametrize('scale', [0, -1, float('nan'), float('inf'), True])
def test_mono16_rejects_unknown_or_invalid_units(scale):
    image, bundle = sensors('mono16')
    bundle['depth_scale_m'] = scale
    assert encode_spatial(image, bundle) == ({'status': 'depth_unit_unknown'}, None)


def test_room_edit_after_capture_does_not_change_snapshot(tmp_path):
    rooms = {'features': [{'id': 'living', 'properties': {'name': '거실'},
                          'geometry': {'type': 'Polygon', 'coordinates': [
                              [[-3, -2], [0, -2], [0, 2], [-3, 2], [-3, -2]]]}}]}
    recorder = PhotoRecorder(tmp_path)
    recorder.begin(CoverageGrid(np.zeros((10, 10)), .1, 0, 0), 'map', 'rooms.json', rooms)
    image, bundle = sensors()
    assert recorder.offer(image, x=-1., y=0., yaw=0., room_names=['거실'],
                          phase='OBSERVING', spatial=bundle, simulated=True)
    rooms['features'][0]['properties']['name'] = '새 이름'
    recorder.close()
    assert recorder.status()['saved'] == 1
    manifest, = (tmp_path / 'captures').glob('[a-f0-9]' * 32 + '.json')
    metadata = json.loads(manifest.read_text())
    snapshot = json.loads(manifest.with_name(manifest.stem + '.regions.json').read_text())
    assert metadata['capture_regions'] == [{'id': 'living', 'name': '거실'}]
    assert snapshot['regions'][0]['name'] == '거실'
    assert metadata['spatial']['status'] == 'available'
    assert manifest.with_name(manifest.stem + '.depth.npy').stat().st_mode & 0o777 == 0o600
