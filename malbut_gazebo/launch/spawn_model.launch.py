import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription, LaunchService
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import Command, LaunchConfiguration
from launch_ros.actions import Node


def launch_setup(context):
    use_sim_time = LaunchConfiguration(
        'use_sim_time', default='true').perform(context)
    world_name = LaunchConfiguration(
        'world_name', default='robocup_home').perform(context)
    moveit_unite = LaunchConfiguration(
        'moveit_unite', default='false').perform(context)

    sim_ign = 'false' if moveit_unite == 'true' else 'true'

    world_name_arg = DeclareLaunchArgument(
        'world_name', default_value=world_name)
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time', default_value=use_sim_time)

    use_sim_time = use_sim_time == 'true'

    malbut_gazebo_path = get_package_share_directory('malbut_gazebo')
    xacro_file = os.path.join(
        malbut_gazebo_path, 'urdf', 'robot.gazebo.xacro')
    robot_description_content = Command([
        'xacro ', xacro_file,
        ' sim_ign:=', sim_ign,
    ])

    robot_state_publisher_node = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        output='screen',
        parameters=[{
            'robot_description': robot_description_content,
            'use_sim_time': use_sim_time,
        }],
    )

    spawn_entity = Node(
        package='ros_gz_sim',
        executable='create',
        output='screen',
        arguments=[
            '-topic', 'robot_description',
            '-name', 'robot',
            '-allow_renaming', 'true',
            '-x', '0',
            '-y', '0',
            '-z', '0.0',
        ],
        parameters=[{'use_sim_time': True}],
    )

    return [
        use_sim_time_arg,
        world_name_arg,
        robot_state_publisher_node,
        spawn_entity,
    ]


def generate_launch_description():
    return LaunchDescription([
        OpaqueFunction(function=launch_setup),
    ])


if __name__ == '__main__':
    ld = generate_launch_description()
    ls = LaunchService()
    ls.include_launch_description(ld)
    ls.run()
