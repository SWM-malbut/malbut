"""Test idle map-profile switches without running Nav2 or DDS."""

from types import SimpleNamespace

from nav2_msgs.srv import ManageLifecycleNodes
import pytest
from rcl_interfaces.msg import ParameterType
from rcl_interfaces.srv import SetParameters
from std_srvs.srv import SetBool

from malbut_system_manager.navigation import NavigationProfile


def _profile(*, mapped=False, fail_start=False):
    events = []
    profiles = {
        'mapped': {'/global_costmap/global_costmap': {
            'global_frame': 'map', 'filters': ['keepout_filter']}},
        'mapless': {'/global_costmap/global_costmap': {
            'global_frame': 'odom', 'filters': []}},
    }
    node = SimpleNamespace(create_client=lambda kind, name, **kwargs: name)

    def call(client, request, label):
        if isinstance(request, SetParameters.Request):
            events.append(request)
            return SimpleNamespace(results=[SimpleNamespace(successful=True)
                                            for _ in request.parameters])
        events.append(request.command)
        return SimpleNamespace(success=not (
            fail_start and request.command == ManageLifecycleNodes.Request.STARTUP))

    return NavigationProfile(node, None, profiles, mapped, call), events


def test_profile_changes_parameters_before_cleanup_and_preserves_empty_array_type():
    """Inactive costmap still spins; cleanup removes its parameter service."""
    profile, events = _profile(mapped=True)
    profile.pause(False)
    profile.activate(False)
    assert events[0] == ManageLifecycleNodes.Request.PAUSE
    assert isinstance(events[1], SetParameters.Request)
    frame, filters = events[1].parameters
    assert frame.value.string_value == 'odom'
    assert filters.value.type == ParameterType.PARAMETER_STRING_ARRAY
    assert list(filters.value.string_array_value) == []
    assert events[2:] == [ManageLifecycleNodes.Request.RESET,
                          ManageLifecycleNodes.Request.STARTUP]
    assert profile.mapped is False and not profile.paused
    assert profile.manager == '/lifecycle_manager_navigation/manage_nodes'


def test_same_profile_does_not_restart_navigation():
    profile, events = _profile()
    profile.pause(False)
    profile.activate(False)
    assert not events


def test_failed_mapped_start_can_restore_original_mapless_profile():
    """Failure after RESET must not make mapless cleanup a no-op."""
    profile, events = _profile(fail_start=True)
    profile.pause(True)
    with pytest.raises(RuntimeError, match='transition failed'):
        profile.activate(True)
    assert profile.paused and not profile.mapped
    profile.call = lambda client, request, label: (
        SimpleNamespace(results=[SimpleNamespace(successful=True)
                                 for _ in request.parameters])
        if isinstance(request, SetParameters.Request) else SimpleNamespace(success=True))
    profile.activate(False)
    assert not profile.paused and not profile.mapped


def test_rejected_parameter_does_not_cleanup_or_claim_success():
    profile, events = _profile()
    profile.pause(True)
    profile.call = lambda *args: SimpleNamespace(results=[SimpleNamespace(successful=False)])
    with pytest.raises(RuntimeError, match='rejected navigation profile'):
        profile.activate(True)
    assert events == [ManageLifecycleNodes.Request.PAUSE]
    assert profile.paused and not profile.mapped


def test_running_local_filter_uses_its_toggle_service_without_motion_restart():
    """Changing parameters alone never updates Humble CostmapFilter's enabled flag."""
    events = []
    profiles = {
        name: {'/local_costmap/local_costmap': {'keepout_filter.enabled': enabled}}
        for name, enabled in [('mapped', True), ('mapless', False)]}
    node = SimpleNamespace(create_client=lambda kind, name, **kwargs: name)

    def call(client, request, label):
        if isinstance(request, SetParameters.Request):
            return SimpleNamespace(results=[SimpleNamespace(successful=True)])
        if isinstance(request, SetBool.Request):
            events.append((client, request.data))
        else:
            assert client == '/lifecycle_manager_navigation/manage_nodes'
        return SimpleNamespace(success=True)

    profile = NavigationProfile(node, None, profiles, True, call)
    profile.pause(False)
    profile.activate(False)
    profile.pause(True)
    profile.activate(True)
    assert events == [('/local_costmap/keepout_filter/toggle_filter', False),
                      ('/local_costmap/keepout_filter/toggle_filter', True)]
