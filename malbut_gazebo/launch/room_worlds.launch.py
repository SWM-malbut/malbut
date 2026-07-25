import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription,LaunchService
from launch.actions import DeclareLaunchArgument,OpaqueFunction
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration



def launch_setup(context):
    use_sim_time = LaunchConfiguration('use_sim_time', default='true').perform(context)
    world_name = LaunchConfiguration('world_name', default='robocup_home').perform(context)
    nav = LaunchConfiguration('nav', default='false').perform(context)

    nav_arg = DeclareLaunchArgument('nav',default_value=nav)
    use_sim_time_arg = DeclareLaunchArgument('use_sim_time',default_value=use_sim_time)
    world_name_arg = DeclareLaunchArgument('world_name',default_value=world_name)


    malbut_gazebo_path = get_package_share_directory('malbut_gazebo')


    world = os.path.join(malbut_gazebo_path, "worlds", world_name + ".sdf")
    gz_sim_launch = IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                [os.path.join(get_package_share_directory('ros_gz_sim'),
                'launch', 'gz_sim.launch.py')]),
                launch_arguments=[('gz_args', [' -r ' + world])])
    

    ros_gz_bridge_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(malbut_gazebo_path, 'launch/ros_gz_bridge.launch.py')
            ),
        launch_arguments={
            'use_sim_time': use_sim_time,
            'nav': nav,
        }.items(),
    )

    spawn_model_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(malbut_gazebo_path, 'launch/spawn_model.launch.py')
            ),
        launch_arguments={
            'world_name': world_name,
            'use_sim_time': use_sim_time,
        }.items(),
    )

    # spawn_objects                                                                
    spawn_objects_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(malbut_gazebo_path, 'launch/spawn_objects.launch.py')
            ),
        launch_arguments={
            'world_name': world_name,
            'use_sim_time': use_sim_time,
        }.items(),
    )
    return ([
        use_sim_time_arg,
        world_name_arg,
        nav_arg,
        gz_sim_launch,
        spawn_objects_launch,
        spawn_model_launch,
        ros_gz_bridge_launch,
    ])
    


def generate_launch_description():
    return LaunchDescription([
        OpaqueFunction(function = launch_setup)
    ])



if __name__ == '__main__':
    ld = generate_launch_description()

    ls = LaunchService()
    ls.include_launch_description(ld)
    ls.run()
