import os
from ament_index_python.packages import get_package_share_directory

from launch_ros.actions import Node
from nav2_common.launch import RewrittenYaml
from launch.actions import DeclareLaunchArgument, OpaqueFunction, ExecuteProcess
from launch import LaunchDescription, LaunchService
from launch.substitutions import LaunchConfiguration

def launch_setup(context):
    enable_save = LaunchConfiguration('enable_save', default='true').perform(context)
    use_sim_time = LaunchConfiguration('use_sim_time', default='true')
    map_frame = LaunchConfiguration('map_frame', default='map')
    odom_frame = LaunchConfiguration('odom_frame', default='odom')
    base_frame = LaunchConfiguration('base_frame', default='base_footprint')
    scan_topic = LaunchConfiguration('scan_topic', default='scan')

    enable_save_arg = DeclareLaunchArgument('enable_save', default_value=enable_save)
    use_sim_time_arg = DeclareLaunchArgument('use_sim_time', default_value=use_sim_time)
    map_frame_arg = DeclareLaunchArgument('map_frame', default_value=map_frame)
    odom_frame_arg = DeclareLaunchArgument('odom_frame', default_value=odom_frame)
    base_frame_arg = DeclareLaunchArgument('base_frame', default_value=base_frame)
    scan_topic_arg = DeclareLaunchArgument('scan_topic', default_value=scan_topic)

    malbut_gazebo_package_path = get_package_share_directory('malbut_gazebo')

    slam_params = RewrittenYaml(
        source_file=os.path.join(malbut_gazebo_package_path, 'config/slam.yaml'),
        param_rewrites={
            'use_sim_time': use_sim_time,
            'map_frame': map_frame,
            'odom_frame': odom_frame,
            'base_frame': base_frame,
            },
        convert_types=True
    )

    remappings=[
        ('/tf', 'tf'),
        ('/tf_static', 'tf_static'),
        ('/map', 'map'),
        ('/map_metadata', 'map_metadata'),
    ]
    if enable_save == 'false':
        remappings.append(('/slam_toolbox/save_map', '/save_map'))

    sync_node = Node(package='slam_toolbox',
         executable='sync_slam_toolbox_node',
         name='slam_toolbox',
         output='screen',
         parameters=[
           slam_params,
         ],
         remappings=remappings
    )
    rviz_node = ExecuteProcess(
            cmd=['rviz2', '-d', os.path.join(malbut_gazebo_package_path, 'rviz/slam.rviz')],
            output='screen'
        )

    return [
        enable_save_arg,
        use_sim_time_arg,
        map_frame_arg,
        odom_frame_arg,
        base_frame_arg,
        scan_topic_arg,
        sync_node,
        rviz_node
    ]

def generate_launch_description():
    return LaunchDescription([
        OpaqueFunction(function = launch_setup)
    ])

if __name__ == '__main__':
    # 创建一个LaunchDescription对象(create a LaunchDescription object)
    ld = generate_launch_description()

    ls = LaunchService()
    ls.include_launch_description(ld)
    ls.run()
