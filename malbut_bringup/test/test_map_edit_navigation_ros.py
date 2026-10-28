"""Opt-in Nav2 planning check in an isolated DDS domain; never starts a base driver."""

import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

import pytest


@pytest.mark.skipif(os.environ.get('MALBUT_MAP_EDIT_NAV2_TEST') != '1',
                    reason='set MALBUT_MAP_EDIT_NAV2_TEST=1 for isolated Nav2 integration')
def test_cleaned_static_map_keeps_sensor_and_keepout_obstacles(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    import yaml
    import rclpy
    from geometry_msgs.msg import TransformStamped
    from lifecycle_msgs.srv import ChangeState
    from nav2_msgs.action import ComputePathToPose
    from nav2_msgs.msg import CostmapFilterInfo
    from nav2_msgs.srv import LoadMap
    from nav_msgs.msg import OccupancyGrid
    from rclpy.action import ActionClient
    from rclpy.qos import DurabilityPolicy, QoSProfile
    from sensor_msgs.msg import LaserScan
    from tf2_ros import StaticTransformBroadcaster
    from malbut_bringup.map_edit import MapEdits, apply_ticket

    monkeypatch.setenv('ROS_DOMAIN_ID', '188')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    pixels = np.zeros((50, 80), np.uint8)
    pixels[10:40, 1:79] = 254
    pixels[10:40, 35:45] = 0
    path = tmp_path / 'corridor.yaml'
    cv2.imwrite(str(path.with_suffix('.pgm')), pixels)
    path.write_text(yaml.safe_dump({'image': 'corridor.pgm', 'resolution': .1, 'origin': [0.,0.,0.],
                    'negate': 0, 'mode': 'trinary', 'occupied_thresh': .65, 'free_thresh': .196}))
    params = tmp_path / 'nav2.yaml'
    params.write_text(yaml.safe_dump({
        'map_server': {'ros__parameters': {'yaml_filename': str(path)}},
        'planner_server': {'ros__parameters': {
            'expected_planner_frequency': 1., 'planner_plugins': ['GridBased'],
            'GridBased': {'plugin': 'nav2_navfn_planner/NavfnPlanner', 'tolerance': .1, 'allow_unknown': False}}},
        'global_costmap': {'global_costmap': {'ros__parameters': {
            'global_frame': 'map', 'robot_base_frame': 'base_link', 'robot_radius': .15,
            'update_frequency': 10., 'publish_frequency': 10., 'resolution': .1,
            'track_unknown_space': True, 'always_send_full_costmap': True,
            'plugins': ['static_layer', 'obstacle_layer', 'inflation_layer'], 'filters': ['keepout_filter'],
            'static_layer': {'plugin': 'nav2_costmap_2d::StaticLayer', 'map_subscribe_transient_local': True},
            'obstacle_layer': {'plugin': 'nav2_costmap_2d::ObstacleLayer', 'observation_sources': 'scan',
                'scan': {'topic': '/scan', 'data_type': 'LaserScan', 'clearing': True, 'marking': True,
                         'inf_is_valid': True, 'obstacle_max_range': 9., 'raytrace_max_range': 10.}},
            'inflation_layer': {'plugin': 'nav2_costmap_2d::InflationLayer', 'inflation_radius': .2},
            'keepout_filter': {'plugin': 'nav2_costmap_2d::KeepoutFilter', 'filter_info_topic': '/keepout_info'},
        }}},
    }))
    logs, processes = [], []
    rclpy.init()
    node = rclpy.create_node('map_edit_integration')
    transient = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
    mask_pub = node.create_publisher(OccupancyGrid, '/keepout_mask', transient)
    info_pub = node.create_publisher(CostmapFilterInfo, '/keepout_info', transient)
    scan_pub = node.create_publisher(LaserScan, '/scan', 10)
    tf = StaticTransformBroadcaster(node)
    transform = TransformStamped()
    transform.header.frame_id, transform.child_frame_id = 'map', 'base_link'
    transform.transform.translation.x, transform.transform.translation.y = 1., 2.5
    transform.transform.rotation.w = 1.
    tf.sendTransform(transform)
    obstacle = [False]

    def scan():
        value = LaserScan()
        value.header.frame_id = 'base_link'
        value.header.stamp = node.get_clock().now().to_msg()
        value.angle_min, value.angle_max, value.angle_increment = -math.pi, math.pi, math.pi/360
        value.range_min, value.range_max = .05, 10.
        value.ranges = [3./math.cos(angle) if obstacle[0] and abs(angle) < .55 else float('inf')
                        for angle in (value.angle_min+i*value.angle_increment for i in range(721))]
        scan_pub.publish(value)
    timer = node.create_timer(.05, scan)

    def wait(future, timeout=15):
        end = time.monotonic() + timeout
        while not future.done() and time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=.03)
        assert future.done(), 'Nav2 operation timed out'
        return future.result()

    def spin(seconds=1.):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=.03)

    def rpc(kind, topic, request):
        client = node.create_client(kind, topic)
        assert client.wait_for_service(timeout_sec=15), topic
        result = wait(client.call_async(request))
        node.destroy_client(client)
        return result

    def lifecycle(name, transition):
        request = ChangeState.Request()
        request.transition.id = transition
        assert rpc(ChangeState, f'/{name}/change_state', request).success

    def publish_mask(blocked):
        mask = OccupancyGrid()
        mask.header.frame_id = 'map'
        mask.header.stamp = node.get_clock().now().to_msg()
        mask.info.width, mask.info.height, mask.info.resolution = 80, 50, .1
        mask.info.origin.orientation.w = 1.
        data = np.zeros((50,80), dtype=np.int8)
        if blocked:
            data[:,39:42] = 100
        mask.data = data.flatten().tolist()
        mask_pub.publish(mask)
        info = CostmapFilterInfo()
        info.type, info.filter_mask_topic, info.base, info.multiplier = 0, '/keepout_mask', 0., 1.
        info_pub.publish(info)

    try:
        for package, executable in [('nav2_map_server','map_server'), ('nav2_planner','planner_server')]:
            logfile = (tmp_path / f'{executable}.log').open('w')
            logs.append(logfile)
            processes.append(subprocess.Popen([f'/opt/ros/humble/lib/{package}/{executable}',
                '--ros-args', '--params-file', str(params)], stdout=logfile, stderr=subprocess.STDOUT,
                env=os.environ.copy(), start_new_session=True))
        lifecycle('map_server', 1)
        lifecycle('map_server', 3)
        publish_mask(False)
        lifecycle('planner_server', 1)
        lifecycle('planner_server', 3)
        action = ActionClient(node, ComputePathToPose, '/compute_path_to_pose')
        assert action.wait_for_server(timeout_sec=10)
        spin(2.)

        def route():
            goal = ComputePathToPose.Goal()
            goal.goal.header.frame_id = 'map'
            goal.goal.header.stamp = node.get_clock().now().to_msg()
            goal.goal.pose.position.x, goal.goal.pose.position.y = 6., 2.5
            goal.goal.pose.orientation.w = 1.
            goal.planner_id = 'GridBased'
            handle = wait(action.send_goal_async(goal))
            assert handle.accepted
            result = wait(handle.get_result_async())
            return result.status == 4 and len(result.result.path.poses) > 1

        assert not route(), 'the mapped temporary wall should block the corridor'
        edits = MapEdits(path)
        original = edits.preview()
        changed = edits.save(original['revision'], [[[3.4,1.],[4.6,1.],[4.6,4.],[3.4,4.]]], str(uuid.uuid4()))
        assert not route(), 'saving alone must not change planning'
        def activate(map_path):
            assert rpc(LoadMap, '/map_server/load_map', LoadMap.Request(map_url=map_path)).result == 0
        apply_ticket(edits.ticket(changed['revision'], changed['revision'], str(uuid.uuid4())), activate)
        spin(2.)
        assert route(), 'the cleaned corridor must allow planning'
        obstacle[0] = True
        spin(2.)
        assert not route(), 'a real laser obstacle must still block the route'
        obstacle[0] = False
        spin(2.)
        assert route(), 'clearing sensor observations should reopen the corridor'
        publish_mask(True)
        spin(2.)
        assert not route(), 'keepout zones must remain impassable after cleanup'
    except Exception:
        for logfile in logs:
            logfile.flush()
            print(Path(logfile.name).read_text()[-7000:])
        raise
    finally:
        node.destroy_timer(timer)
        for process in processes:
            os.killpg(process.pid, signal.SIGINT)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for logfile in logs:
            logfile.close()
        node.destroy_node()
        rclpy.shutdown()
