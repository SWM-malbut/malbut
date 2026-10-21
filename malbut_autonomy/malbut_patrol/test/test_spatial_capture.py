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
                         d=[0.] * 5, header=header),
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
    bundle['info'].d[0] = .1
    assert encode_spatial(image, bundle)[0]['status'] == 'calibration_unsupported'
    bundle['info'].d[0] = 0
    bundle['depth'].header = NS(frame_id='other_optical', stamp=image.header.stamp)
    assert encode_spatial(image, bundle)[0]['status'] == 'registration_frame_mismatch'


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
