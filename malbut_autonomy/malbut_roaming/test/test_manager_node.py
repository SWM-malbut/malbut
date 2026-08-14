"""ROS graph-level tests for the roaming manager's public interface."""

from pathlib import Path

from nav_msgs.msg import OccupancyGrid
import pytest
import rclpy
import yaml

from malbut_roaming.roaming_manager import RoamingManager


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PACKAGE_ROOT / 'config' / 'roaming.yaml'


@pytest.fixture
def manager():
    rclpy.init(args=['--ros-args', '--params-file', str(CONFIG_FILE)])
    node = RoamingManager()
    try:
        yield node
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_parameter_file_is_applied_to_the_real_node(manager):
    """Every configured key must be declared and loaded by RoamingManager."""
    expected = yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8'))[
        'roaming_manager'
    ]['ros__parameters']
    for name, value in expected.items():
        assert manager.has_parameter(name), name
        assert manager.get_parameter(name).value == value


def test_public_ros_graph_uses_nav2_inputs_without_cmd_vel(manager):
    """The real node must expose its API without directly driving wheels."""
    services = dict(manager.get_service_names_and_types())
    assert {
        '/roaming/start',
        '/roaming/pause',
        '/roaming/resume',
        '/roaming/stop',
    } <= services.keys()

    expected_publishers = {
        '/roaming/status': 'std_msgs/msg/String',
        '/roaming/selected_goal': 'geometry_msgs/msg/PoseStamped',
    }
    for topic, message_type in expected_publishers.items():
        endpoints = manager.get_publishers_info_by_topic(topic)
        own = [endpoint for endpoint in endpoints if endpoint.node_name == manager.get_name()]
        assert [endpoint.topic_type for endpoint in own] == [message_type]

    expected_subscriptions = {
        '/map': 'nav_msgs/msg/OccupancyGrid',
        '/roaming/interest_target': 'geometry_msgs/msg/PoseStamped',
        '/roaming/goal': 'geometry_msgs/msg/PoseStamped',
    }
    for topic, message_type in expected_subscriptions.items():
        endpoints = manager.get_subscriptions_info_by_topic(topic)
        own = [endpoint for endpoint in endpoints if endpoint.node_name == manager.get_name()]
        assert [endpoint.topic_type for endpoint in own] == [message_type]

    own_cmd_vel_publishers = [
        endpoint
        for endpoint in manager.get_publishers_info_by_topic('/cmd_vel')
        if endpoint.node_name == manager.get_name()
    ]
    assert not own_cmd_vel_publishers


def test_map_callback_builds_only_safe_candidates(manager):
    """The ROS adapter must turn a received map into the conservative grid."""
    message = OccupancyGrid()
    message.header.frame_id = 'map'
    message.info.width = 5
    message.info.height = 5
    message.info.resolution = 1.0
    message.info.origin.orientation.w = 1.0
    message.data = [0] * 25
    manager._map_callback(message)

    assert manager._grid is not None
    assert manager._candidates
    assert all(
        0 < candidate.cell_x < 4 and 0 < candidate.cell_y < 4
        for candidate in manager._candidates
    )
