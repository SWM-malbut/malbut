"""Exercise map rendering and coordinate transforms without ROS processes."""

from array import array
from concurrent.futures import ThreadPoolExecutor
import math
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from malbut_bringup.web_map import grid_to_world, MapCache, world_to_grid


def _map(width=3, height=2, data=None, yaw=0.0):
    """Construct a message-shaped map with the same signed data as ROS."""
    return SimpleNamespace(
        header=SimpleNamespace(frame_id='map'),
        info=SimpleNamespace(
            width=width, height=height, resolution=0.5,
            origin=SimpleNamespace(
                position=SimpleNamespace(x=2.0, y=-3.0, z=0.0),
                orientation=SimpleNamespace(x=0.0, y=0.0,
                                            z=math.sin(yaw / 2), w=math.cos(yaw / 2)))),
        data=array('b', data if data is not None else [-1, 0, 100, 25, 50, 75]))


def test_unreceived_and_inactive_maps_are_distinguished():
    """A stopped publisher does not erase a once-published static map."""
    cache = MapCache()
    assert cache.snapshot(active=True) == {
        'available': False, 'active': True, 'version': 0,
        'pose': None, 'pose_available': False}
    assert cache.png() is None
    cache.update(_map())
    snapshot = cache.snapshot(active=False)
    assert snapshot['available'] and not snapshot['active']
    assert snapshot['frame_id'] == 'map'
    assert cache.png()[0]['version'] == snapshot['version'] == 1


def test_png_shades_and_vertical_flip_preserve_original_message():
    """Free, occupied, unknown and intermediate cells use distinct map shades."""
    message = _map()
    original = message.data[:]
    cache = MapCache()
    cache.update(message)
    _, png = cache.png()
    pixels = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    assert pixels.tolist() == [[191, 128, 64], [127, 255, 0]]
    assert message.data == original


def test_clear_forgets_map_and_never_reuses_its_image_version():
    """A restarted runtime cannot expose the previous runtime's cached image."""
    cache = MapCache()
    first = cache.update(_map())
    assert cache.png()[0]['version'] == first
    cache.clear()
    cleared = cache.snapshot(active=True, pose={'x': 0, 'y': 0, 'yaw': 0})
    assert cleared['version'] > first
    assert not cleared['available'] and not cleared['pose_available']
    assert 'origin' not in cleared
    assert cache.png() is None
    latest = cache.update(_map(width=1, height=1, data=[100]))
    assert latest > cleared['version']
    assert cache.png()[0]['version'] == latest


def test_encoding_is_deferred_and_shared_by_concurrent_requests(monkeypatch):
    """Receiving and inspecting a map never encode it; readers share one PNG."""
    encoder = Mock(return_value=b'png')
    monkeypatch.setattr('malbut_bringup.web_map._encode_png', encoder)
    cache = MapCache()
    cache.update(_map())
    cache.snapshot(active=True)
    encoder.assert_not_called()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: cache.png(), range(8)))
    assert all(metadata['version'] == 1 and png == b'png' for metadata, png in results)
    encoder.assert_called_once()
    cache.update(_map(width=2, height=1, data=[100, 0], yaw=math.pi / 2))
    metadata, png = cache.png()
    assert metadata['version'] == 2 and png == b'png'
    assert (metadata['width'], metadata['height']) == (2, 1)
    assert encoder.call_count == 2
    snapshot = cache.snapshot(active=True)
    assert (snapshot['width'], snapshot['height']) == (2, 1)
    assert snapshot['origin']['yaw'] == pytest.approx(math.pi / 2)


def test_update_during_encoding_cannot_cache_an_old_png_for_a_new_map(monkeypatch):
    """Encoding an old snapshot leaves the newer map and its version intact."""
    entered, release = threading.Event(), threading.Event()

    def encode(message):
        entered.set()
        assert release.wait(timeout=2)
        return bytes([message.info.width])

    monkeypatch.setattr('malbut_bringup.web_map._encode_png', encode)
    cache = MapCache()
    cache.update(_map())
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(cache.png)
        assert entered.wait(timeout=2)
        try:
            cache.update(_map(width=1, height=1, data=[0]))
            assert cache.snapshot(active=True)['version'] == 2
        finally:
            release.set()
        metadata, png = pending.result(timeout=2)
        assert (metadata['version'], metadata['width'], png) == (1, 3, b'\x03')
    metadata, png = cache.png()
    assert (metadata['version'], metadata['width'], png) == (2, 1, b'\x01')


@pytest.mark.parametrize('mutate', [
    lambda message: setattr(message.info, 'width', 0),
    lambda message: setattr(message.info, 'height', 1.5),
    lambda message: setattr(message.info, 'resolution', 0.0),
    lambda message: setattr(message.info, 'resolution', math.nan),
    lambda message: setattr(message, 'data', array('b', [0])),
    lambda message: setattr(message, 'data', array('b', [-2, 0, 0, 0, 0, 0])),
    lambda message: setattr(message, 'data', array('b', [101, 0, 0, 0, 0, 0])),
    lambda message: setattr(message.info.origin.position, 'x', math.inf),
    lambda message: setattr(message.info.origin.orientation, 'w', 0.0),
    lambda message: setattr(message.info.origin.orientation, 'x', 0.1),
    lambda message: setattr(message.header, 'frame_id', ''),
])
def test_invalid_update_keeps_last_valid_map(mutate):
    """Malformed geometry or cell values cannot replace a valid cached map."""
    cache = MapCache()
    cache.update(_map())
    original_png = cache.png()
    invalid = _map()
    mutate(invalid)
    with pytest.raises(ValueError):
        cache.update(invalid)
    assert cache.snapshot(active=True)['version'] == 1
    assert cache.png() == original_png


@pytest.mark.parametrize('yaw', [0.0, math.pi / 2, -math.pi / 2, math.pi])
def test_rotated_grid_and_world_coordinates(yaw):
    """Origin rotation and cell centers round-trip without a half-cell offset."""
    cache = MapCache()
    cache.update(_map(yaw=yaw))
    snapshot = cache.snapshot(active=True)
    origin = snapshot['origin']
    for column, row in ((0.0, 0.0), (0.5, 0.5), (2.5, 1.5), (-1.0, 2.0)):
        x, y = grid_to_world(column, row, snapshot['resolution'], origin)
        assert world_to_grid(x, y, snapshot['resolution'], origin) == pytest.approx(
            (column, row))
    if yaw == math.pi / 2:
        assert grid_to_world(2.0, 0.0, 0.5, origin) == pytest.approx((2.0, -2.0))


def test_pose_and_origin_in_snapshot_cannot_mutate_cached_state():
    """Expose finite pose coordinates only, without sharing mutable dictionaries."""
    cache = MapCache()
    cache.update(_map())
    pose = {'x': 3.0, 'y': 4.0, 'yaw': 0.2}
    snapshot = cache.snapshot(active=True, pose=pose)
    assert snapshot['pose_available']
    snapshot['origin']['x'] = -999
    snapshot['pose']['x'] = -999
    assert cache.snapshot(active=True)['origin']['x'] == 2.0
    assert pose['x'] == 3.0
    assert not cache.snapshot(active=True)['pose_available']
    with pytest.raises(ValueError):
        cache.snapshot(active=True, pose={'x': math.nan, 'y': 0.0, 'yaw': 0.0})
