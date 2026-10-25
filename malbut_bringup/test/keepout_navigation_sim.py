"""
Exercise the hardware Nav2 configuration on a Fortress robot, without hardware.

Run after building/sourcing malbut_description, malbut_gazebo and
malbut_gazebo_plugins. The generated room is synthetic; no household map is used.
"""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid
from xml.etree import ElementTree as ET

import cv2
import numpy as np
import yaml


def make_fixture(folder, gazebo_share):
    """Give the simulator and the map server identical physical walls."""
    from malbut_bringup.zones import build_mask

    origin, resolution, shape = (-2., -2.), .05, (90, 100)
    boxes = [(-2., -2., -1.9, 2.5), (2.9, -2., 3., 2.5),
             (-2., -2., 3., -1.9), (-2., 2.4, 3., 2.5),
             (1.7, .9, 2., 1.6), (.3, 1.7, .6, 2.)]
    tree = ET.parse(gazebo_share / 'worlds/empty.sdf')
    world = tree.getroot().find('world')
    image = np.full(shape, 254, dtype=np.uint8)
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        model = ET.SubElement(world, 'model', name=f'fixture_wall_{index}')
        ET.SubElement(model, 'static').text = 'true'
        ET.SubElement(model, 'pose').text = f'{(x0+x1)/2} {(y0+y1)/2} .5 0 0 0'
        link = ET.SubElement(model, 'link', name='body')
        for kind in ('collision', 'visual'):
            element = ET.SubElement(link, kind, name=kind)
            geometry = ET.SubElement(element, 'geometry')
            ET.SubElement(ET.SubElement(geometry, 'box'), 'size').text = f'{x1-x0} {y1-y0} 1'
        col0, col1 = [round((x-origin[0])/resolution) for x in (x0, x1)]
        row0, row1 = [round((y-origin[1])/resolution) for y in (y0, y1)]
        # The last box is deliberately absent from the static map: only the
        # simulator's lidar can add it to Nav2's obstacle layer.
        if index != len(boxes)-1:
            image[shape[0]-row1:shape[0]-row0, col0:col1] = 0
    tree.write(folder / 'world.sdf')
    cv2.imwrite(str(folder / 'map.pgm'), image)
    geometry = {'image': 'map.pgm', 'resolution': resolution, 'origin': [*origin, 0.],
                'negate': 0, 'occupied_thresh': .65, 'free_thresh': .196}
    (folder / 'map.yaml').write_text(yaml.safe_dump(geometry))
    ring = [[.95, -.75], [1.9, -.75], [1.9, .75], [.95, .75], [.95, -.75]]
    features = [{'type': 'Feature', 'properties': {
        'role': 'semantic_zone', 'behavior': 'restricted'},
        'geometry': {'type': 'Polygon', 'coordinates': [ring]}}]
    mask, _ = build_mask(folder / 'map.yaml', features)
    return image, mask, geometry, ring, boxes


def run(args):
    """Use actual Gazebo odometry, lidar, planner and controller in a private domain."""
    # Set isolation before importing/initializing ROS or launching any process.
    os.environ['ROS_DOMAIN_ID'] = str(args.domain)
    os.environ['ROS_LOCALHOST_ONLY'] = '1'
    os.environ['IGN_PARTITION'] = 'malbut-keepout-' + uuid.uuid4().hex
    os.environ.pop('CYCLONEDDS_URI', None)
    from ament_index_python.packages import get_package_share_directory
    import rclpy
    from geometry_msgs.msg import TransformStamped, Twist
    from lifecycle_msgs.srv import ChangeState, GetState
    from nav2_msgs.action import ComputePathToPose, NavigateToPose
    from nav2_msgs.msg import CostmapFilterInfo
    from nav2_msgs.srv import GetCostmap
    from nav_msgs.msg import OccupancyGrid, Odometry
    from rcl_interfaces.msg import Log
    from rclpy.action import ActionClient
    from rclpy.parameter import Parameter
    from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
    from sensor_msgs.msg import LaserScan
    from tf2_ros import StaticTransformBroadcaster

    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    share = Path(get_package_share_directory('malbut_gazebo'))
    pixels, mask, geometry, ring, boxes = make_fixture(folder, share)
    params = yaml.safe_load((root / 'malbut_bringup/config/nav2_params.yaml').read_text())
    for scope in ('local_costmap', 'global_costmap'):
        costmap = params[scope][scope]['ros__parameters']
        costmap['use_sim_time'] = True
        costmap['obstacle_layer']['scan']['topic'] = '/scan'
        if args.baseline:
            costmap['filters'] = ['keepout_filter']
            costmap.pop('keepout_inflation_layer', None)
    for name in ('controller_server', 'planner_server', 'behavior_server',
                 'velocity_smoother', 'bt_navigator',
                 'bt_navigator_navigate_to_pose_rclcpp_node',
                 'bt_navigator_navigate_through_poses_rclcpp_node'):
        params[name]['ros__parameters']['use_sim_time'] = True
        params[name]['ros__parameters']['bond_heartbeat_period'] = 0.
    params['bt_navigator']['ros__parameters']['default_nav_to_pose_bt_xml'] = str(
        root / 'malbut_bringup/config/navigate_to_pose.xml')
    params['bt_navigator']['ros__parameters']['default_nav_through_poses_bt_xml'] = str(
        root / 'malbut_bringup/config/navigate_through_poses.xml')
    (folder / 'nav2.yaml').write_text(yaml.safe_dump(params))
    processes, streams = [], []
    result = {'baseline': args.baseline, 'domain': args.domain, 'legs': [], 'errors': [],
              'warnings': [], 'pose_samples': [], 'commands': [], 'zone': ring,
              'localization': 'Gazebo odometry with identity map-to-odom; AMCL excluded',
              'config': str(root / 'malbut_bringup/config/nav2_params.yaml')}

    def start(label, command):
        stream = (folder / f'{label}.log').open('w')
        streams.append(stream)
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        processes.append(process)
        return process

    rclpy.init()
    node = rclpy.create_node('keepout_sim_verifier', parameter_overrides=[
        Parameter('use_sim_time', value=True)])
    latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
    latest = {}

    def odometry(message):
        latest['odom'] = message
        p = message.pose.pose
        result['pose_samples'].append([node.get_clock().now().nanoseconds / 1e9,
                                       p.position.x, p.position.y,
                                       2*math.atan2(p.orientation.z, p.orientation.w)])

    def laser(message):
        latest['scan'] = message
        latest['scan_received'] = time.monotonic()

    def log(message):
        if message.level >= 30:
            result['errors' if message.level >= 40 else 'warnings'].append({
                'name': message.name, 'message': message.msg})

    def velocity(message):
        result['commands'].append([message.linear.x, message.linear.y, message.angular.z])

    subs = [node.create_subscription(Odometry, '/odom', odometry, qos_profile_sensor_data),
            node.create_subscription(LaserScan, '/scan', laser, qos_profile_sensor_data),
            node.create_subscription(Log, '/rosout', log, 100),
            node.create_subscription(Twist, '/cmd_vel', velocity, 100)]
    publishers = []
    stop_pub = node.create_publisher(Twist, '/cmd_vel', 10)
    tf = StaticTransformBroadcaster(node)
    transform = TransformStamped()
    transform.header.frame_id, transform.child_frame_id = 'map', 'odom'
    transform.transform.rotation.w = 1.
    tf.sendTransform(transform)
    active_handle = None

    def spin_until(predicate, timeout=30.):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError('Simulation operation timed out')
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError('Simulation child exited: ' + str(process.args))
            rclpy.spin_once(node, timeout_sec=.03)

    def pump(seconds):
        deadline = time.monotonic() + seconds
        spin_until(lambda: time.monotonic() >= deadline, seconds + 1)

    def wait(future, timeout=30.):
        spin_until(future.done, timeout)
        return future.result()

    def rpc(kind, name, request):
        client = node.create_client(kind, name)
        try:
            assert client.wait_for_service(timeout_sec=20), name
            # Fast DDS can discover the request reader before the response
            # writer is matched. Let discovery settle before the first call.
            pump(.2)
            try:
                return wait(client.call_async(request))
            except TimeoutError:
                if kind is not ChangeState:
                    raise
                # Never repeat a possibly completed transition. Confirm its
                # resulting state through the read-only lifecycle service.
                state = rpc(GetState, name.replace('/change_state', '/get_state'),
                            GetState.Request()).current_state.id
                expected = {1: 2, 3: 3}[request.transition.id]
                if state != expected:
                    raise RuntimeError(f'{name}: expected state {expected}, got {state}')
                result.setdefault('lost_transition_acknowledgements', []).append(name)
                return ChangeState.Response(success=True)
        finally:
            node.destroy_client(client)

    def grid(topic, values):
        pub = node.create_publisher(OccupancyGrid, topic, latched)
        publishers.append(pub)
        message = OccupancyGrid()
        message.header.frame_id = 'map'
        message.header.stamp = node.get_clock().now().to_msg()
        message.info.width, message.info.height = values.shape[1], values.shape[0]
        message.info.resolution = geometry['resolution']
        message.info.origin.position.x, message.info.origin.position.y = geometry['origin'][:2]
        message.info.origin.orientation.w = 1.
        message.data = np.flipud(values).astype(np.int8).flatten().tolist()
        pub.publish(message)

    def read_costs():
        response = rpc(GetCostmap, '/global_costmap/get_costmap', GetCostmap.Request())
        m = response.map
        data = np.asarray(m.data).reshape(m.metadata.size_y, m.metadata.size_x)
        np.savez_compressed(folder / 'costmap.npz', data=data)

        def at(x, y):
            row = int((y-m.metadata.origin.position.y)/m.metadata.resolution)
            col = int((x-m.metadata.origin.position.x)/m.metadata.resolution)
            return int(data[row, col])

        sensor_cost = max(at(x, y) for x in np.arange(.25, .66, .025)
                          for y in np.arange(1.65, 1.81, .025))
        return {'zone_interior': at(1.3, .3), 'outside_zone_10cm': at(1.3, .85),
                'physical_wall': at(1.8, 1.2), 'free_space': at(0., 0.),
                'unmapped_lidar_obstacle': sensor_cost}

    try:
        start('gazebo', ['ros2', 'launch', 'malbut_gazebo', 'simulation.launch.py',
                         f'world:={folder / "world.sdf"}', 'gui:=false', 'headless:=true',
                         'rviz:=false', 'depth_camera_enabled:=false', 'imu_enabled:=false',
                         'x:=0', 'y:=0', 'yaw:=0', 'z:=0.002'])
        spin_until(lambda: 'odom' in latest and 'scan' in latest, 60)
        assert node.get_clock().now().nanoseconds > 0
        assert len(latest['scan'].ranges) > 100
        result['lidar_samples'] = len(latest['scan'].ranges)
        grid('/map', np.where(pixels < 65, 100, 0))
        grid('/keepout_filter_mask', mask)
        info = node.create_publisher(CostmapFilterInfo, '/keepout_costmap_filter_info', latched)
        publishers.append(info)
        info.publish(CostmapFilterInfo(type=0, filter_mask_topic='/keepout_filter_mask',
                                       base=0., multiplier=1.))
        for package, executable, remaps in [
                ('nav2_planner', 'planner_server', []),
                ('nav2_controller', 'controller_server', ['-r', 'cmd_vel:=cmd_vel_nav']),
                ('nav2_behaviors', 'behavior_server', []),
                ('nav2_velocity_smoother', 'velocity_smoother',
                 ['-r', 'cmd_vel:=cmd_vel_nav', '-r', 'cmd_vel_smoothed:=cmd_vel']),
                ('nav2_bt_navigator', 'bt_navigator', [])]:
            start(executable, ['ros2', 'run', package, executable, '--ros-args',
                               '--params-file', str(folder / 'nav2.yaml'), *remaps])
            for transition in (1, 3):
                request = ChangeState.Request()
                request.transition.id = transition
                assert rpc(ChangeState, f'/{executable}/change_state', request).success
        pump(3)
        result['costs'] = read_costs()
        assert result['costs']['zone_interior'] == 254
        assert result['costs']['physical_wall'] == 254
        assert result['costs']['unmapped_lidar_obstacle'] == 254
        if not args.baseline:
            assert result['costs']['outside_zone_10cm'] >= 253, result['costs']
        planner = ActionClient(node, ComputePathToPose, '/compute_path_to_pose')
        navigator = ActionClient(node, NavigateToPose, '/navigate_to_pose')
        assert planner.wait_for_server(timeout_sec=10)
        assert navigator.wait_for_server(timeout_sec=10)
        for label, target in [('viewpoint', (1.13, 1.13)), ('return', (0., 0.))]:
            goal = ComputePathToPose.Goal()
            goal.goal.header.frame_id = 'map'
            goal.goal.header.stamp = node.get_clock().now().to_msg()
            goal.goal.pose.position.x, goal.goal.pose.position.y = target
            goal.goal.pose.orientation.w = 1.
            goal.planner_id = 'GridBased'
            handle = wait(planner.send_goal_async(goal))
            assert handle.accepted
            plan = wait(handle.get_result_async())
            leg = {'name': label, 'target': target, 'planning_status': plan.status,
                   'path': [[p.pose.position.x, p.pose.position.y]
                            for p in plan.result.path.poses]}
            result['legs'].append(leg)
            if plan.status != 4:
                break
            command = NavigateToPose.Goal(pose=goal.goal)
            active_handle = wait(navigator.send_goal_async(command))
            assert active_handle.accepted
            future = active_handle.get_result_async()
            deadline = time.monotonic() + 90
            while not future.done():
                rclpy.spin_once(node, timeout_sec=.03)
                if time.monotonic() > deadline or time.monotonic()-latest['scan_received'] > 2:
                    wait(active_handle.cancel_goal_async())
                    wait(future, 10)
                    raise TimeoutError('Navigation timed out or lidar became stale')
            leg['navigation_status'] = future.result().status
            active_handle = None
            pump(.3)
            p = latest['odom'].pose.pose.position
            leg['final_xy'] = [p.x, p.y]
            leg['distance_m'] = math.hypot(p.x-target[0], p.y-target[1])
            print(json.dumps({k: v for k, v in leg.items() if k != 'path'}), flush=True)
            if leg['navigation_status'] != 4:
                break
        result['success'] = (len(result['legs']) == 2 and all(
            leg.get('navigation_status') == 4 and leg['distance_m'] < .2
            for leg in result['legs']))
        outline = np.array([[.1485, .116], [.1485, -.116],
                            [-.1485, -.116], [-.1485, .116]])
        hazards = [np.asarray(ring[:-1], dtype=np.float32)]
        hazards.extend(np.asarray([[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                                  dtype=np.float32) for x0, y0, x1, y1 in boxes)
        intersections = [0] * len(hazards)
        for _, x, y, yaw in result['pose_samples']:
            c, s = math.cos(yaw), math.sin(yaw)
            body = (outline @ np.array([[c, s], [-s, c]]) + [x, y]).astype(np.float32)
            for index, hazard in enumerate(hazards):
                if cv2.intersectConvexConvex(body, hazard)[0] > 1e-7:
                    intersections[index] += 1
        result['footprint_intersections'] = {
            'restricted_zone': intersections[0], 'physical_geometry': sum(intersections[1:])}
        if not args.baseline:
            assert result['success'], 'The fixed configuration must complete both legs'
            assert not any(intersections), result['footprint_intersections']
            assert not result['errors'], result['errors']
    finally:
        if active_handle is not None:
            try:
                wait(active_handle.cancel_goal_async(), 5)
                wait(active_handle.get_result_async(), 5)
            except Exception as error:
                result['cancel_error'] = str(error)
        stop_pub.publish(Twist())
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
        for process in reversed(processes):
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
        for stream in streams:
            stream.close()
        (folder / 'results.json').write_text(json.dumps(result, indent=2))
        del subs
        node.destroy_node()
        rclpy.shutdown()


def main():
    """Create a new evidence directory; never overwrite an existing run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--domain', type=int, default=74, choices=range(70, 101))
    run(parser.parse_args())


if __name__ == '__main__':
    main()
