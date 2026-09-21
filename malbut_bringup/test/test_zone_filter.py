"""Keep the keepout mask in step with the selected map, without Nav2."""

from concurrent.futures import Future
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
from nav2_msgs.srv import LoadMap
import numpy as np
import pytest
from std_msgs.msg import String

from malbut_bringup.zone_filter import ZoneFilter
from malbut_bringup.zones import write_zones, zone_feature, zones_path


@pytest.fixture
def saved_map(tmp_path):
    """Write a small saved map beside which Zones are stored."""
    cv2.imwrite(str(tmp_path / 'home.pgm'), np.full((40, 80), 254, dtype=np.uint8))
    path = tmp_path / 'home.yaml'
    path.write_text('image: home.pgm\nresolution: 0.05\norigin: [-1.0, -1.0, 0.0]\n'
                    'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
    return path


def _done(result):
    future = Future()
    future.set_result(SimpleNamespace(result=result))
    return future


def _node(tmp_path, clock):
    node = object.__new__(ZoneFilter)
    node.directory = tmp_path / 'cache'
    node.buffer_m = 0.2
    node.client = Mock()
    node.client.service_is_ready.return_value = True
    node.client.call_async.side_effect = lambda _: _done(LoadMap.Response.RESULT_SUCCESS)
    node.status = Mock()
    node.map_file = None
    node.applied = ()
    node.pending = None
    node.retry_at = 0.0
    node.last_report = None
    node.get_logger = Mock()
    return node


@pytest.fixture
def clock(monkeypatch):
    """Control the retry and response deadlines."""
    now = SimpleNamespace(value=100.0)
    monkeypatch.setattr('malbut_bringup.zone_filter.time.monotonic', lambda: now.value)
    return now


def _select(node, mode, map_file=None):
    node._localization(String(data=json.dumps({'mode': mode, 'map': map_file and str(map_file)})))


def _loaded(node):
    """Return the mask image that was sent last, then finish the load."""
    request = node.client.call_async.call_args.args[0]
    node._tick()
    mask = cv2.imread(str(request.map_url).replace('.yaml', '.pgm'), cv2.IMREAD_UNCHANGED)
    return mask, json.loads(node.status.publish.call_args.args[0].data)


def test_start_clears_any_mask_until_a_saved_map_is_selected(tmp_path, clock):
    """Mapping and startup load a one-cell free mask, so no old Zone stays active."""
    node = _node(tmp_path, clock)
    node._tick()
    mask, state = _loaded(node)
    assert mask.tolist() == [[0]]
    assert state == {'state': 'CLEARED', 'map': None, 'zones': 0,
                     'message': 'no saved map selected'}
    node._tick()
    node.client.call_async.assert_called_once()


def test_selected_map_zones_are_applied_and_edits_reload(tmp_path, clock, saved_map):
    """The mask follows the map's Zone file; an unchanged file is not reloaded."""
    node = _node(tmp_path, clock)
    write_zones(saved_map, [zone_feature(
        'restricted', [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]])])
    _select(node, 'LOCALIZATION', saved_map)
    mask, state = _loaded(node)
    assert mask.shape == (40, 80) and mask.max() == 100
    assert state['state'] == 'APPLIED' and state['zones'] == 1
    assert state['map'] == str(saved_map)
    node._tick()
    assert node.client.call_async.call_count == 1
    write_zones(saved_map, [])
    stat = zones_path(saved_map).stat()
    os.utime(zones_path(saved_map), ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    node._tick()
    mask, state = _loaded(node)
    assert mask.tolist() == [[0]] and state['message'] == 'no zones for this map'


def test_switching_clears_the_previous_map_mask_at_once(tmp_path, clock, saved_map):
    """A mask never outlives the map it was drawn for."""
    node = _node(tmp_path, clock)
    write_zones(saved_map, [zone_feature(
        'avoid', [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]])])
    _select(node, 'LOCALIZATION', saved_map)
    _loaded(node)
    _select(node, 'SWITCHING', saved_map)
    mask, state = _loaded(node)
    assert mask.tolist() == [[0]] and state['state'] == 'CLEARED'


def test_zones_for_another_map_version_are_not_applied(tmp_path, clock, saved_map):
    """A broken or foreign Zone file loads the free mask and reports why."""
    node = _node(tmp_path, clock)
    zones_path(saved_map).write_text(json.dumps({
        'type': 'FeatureCollection', 'format': 'malbut-semantic-zones-v1',
        'map_id': 'another-map', 'features': []}))
    _select(node, 'LOCALIZATION', saved_map)
    mask, state = _loaded(node)
    assert mask.tolist() == [[0]]
    assert state['state'] == 'ERROR' and 'different version' in state['message']


def test_unavailable_or_failing_mask_server_is_reported_and_retried(tmp_path, clock):
    """Waiting, no reply and rejection are visible and retried later."""
    node = _node(tmp_path, clock)
    node.client.service_is_ready.return_value = False
    node._tick()
    assert json.loads(node.status.publish.call_args.args[0].data)['state'] == 'WAITING'
    node.client.service_is_ready.return_value = True
    node.client.call_async.side_effect = lambda _: Future()
    node._tick()
    clock.value += 11.0
    node._tick()
    state = json.loads(node.status.publish.call_args.args[0].data)
    assert state['state'] == 'ERROR' and 'did not respond' in state['message']
    node.client.remove_pending_request.assert_called_once()
    node._tick()
    assert node.client.call_async.call_count == 1  # Retry waits a moment.
    clock.value += 6.0
    node.client.call_async.side_effect = lambda _: _done(LoadMap.Response.RESULT_INVALID_MAP_DATA)
    node._tick()
    node._tick()
    state = json.loads(node.status.publish.call_args.args[0].data)
    assert state['state'] == 'ERROR'
    assert 'rejected the mask: invalid mask image' in state['message']
    assert node.applied == ()


def test_mask_server_that_is_not_active_yet_is_waited_for(tmp_path, clock):
    """A slow container answers UNDEFINED_FAILURE before ACTIVE; that is not an error."""
    node = _node(tmp_path, clock)
    node.client.call_async.side_effect = lambda _: _done(LoadMap.Response.RESULT_UNDEFINED_FAILURE)
    node._tick()
    node._tick()
    state = json.loads(node.status.publish.call_args.args[0].data)
    assert state['state'] == 'WAITING' and 'activate' in state['message']
    node._tick()
    assert node.client.call_async.call_count == 1
    clock.value += 1.5
    node.client.call_async.side_effect = lambda _: _done(LoadMap.Response.RESULT_SUCCESS)
    node._tick()
    node._tick()
    assert json.loads(node.status.publish.call_args.args[0].data)['state'] == 'CLEARED'
    assert node.applied is None  # The empty mask for 'no saved map' is now in place.


def test_reports_of_different_severity_use_a_real_logger(tmp_path, clock):
    """The rclpy logger rejects a call site that changes severity; each state must log."""
    import rclpy

    context = rclpy.Context()
    rclpy.init(context=context, domain_id=100 + os.getpid() % 30)
    real = None
    try:
        real = ZoneFilter(context=context, parameter_overrides=[
            rclpy.parameter.Parameter('cache_directory', value=str(tmp_path / 'cache'))])
        node = _node(tmp_path, clock)
        node.get_logger = real.get_logger
        node._report('WAITING', None, 0, 'waiting for the zone mask server')
        node._report('ERROR', None, 0, 'zone mask server rejected the mask: result 3')
        node._report('CLEARED', None, 0, 'no saved map selected')
    finally:
        if real is not None:
            real.destroy_node()
        context.try_shutdown()
