import os
from setuptools import find_packages, setup

package_name = 'malbut_gazebo'


def collect_data_files(*directories):
    data_files = []
    for directory in directories:
        for root, dirnames, filenames in os.walk(directory):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname != '__pycache__' and not dirname.startswith('.')
            ]
            sources = [
                os.path.join(root, filename)
                for filename in filenames
                if not filename.startswith('.')
            ]
            if sources:
                data_files.append((
                    os.path.join('share', package_name, root),
                    sources,
                ))
    return data_files


setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, [
            'package.xml',
            'LICENSE',
            'README.md',
            'THIRD_PARTY_NOTICES.md',
        ]),
    ] + collect_data_files(
        'launch',
        'urdf',
        'config',
        'worlds',
        'rviz',
        'maps',
        'models',
        'web',
    ),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SANGGEUN JI',
    maintainer_email='sanggeunji0117@gmail.com',
    description=(
        'Gazebo Fortress robot integration and household environments for '
        'Malbut'
    ),
    license=(
        'Apache-2.0 AND MIT AND LicenseRef-Hiwonder-ROSOrin AND '
        'LicenseRef-Gazebo-Fuel-Actor'
    ),
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'spawn_when_ready = malbut_gazebo.spawn_when_ready:main',
            'teleop_key_control = malbut_gazebo.teleop_key_control:main',
            'build_user_map = malbut_gazebo.user_map_builder:main',
            'build_zone_filter_mask = '
            'malbut_gazebo.zone_filter_mask:main',
            'user_map_editor = malbut_gazebo.user_map_editor:main',
            'robot_web_server = malbut_gazebo.robot_web_server:main',
            'map_onboarding_server = '
            'malbut_gazebo.map_onboarding_server:main',
            'cloud_robot_sync = malbut_gazebo.cloud_robot_sync:main',
            'demo_actor_manager = '
            'malbut_gazebo.demo_actor_manager:main',
            'inscribed_escape = '
            'malbut_gazebo.inscribed_escape:main',
            'record_localization_state = '
            'malbut_gazebo.localization_handoff:record_main',
            'restore_localization_state = '
            'malbut_gazebo.localization_handoff:restore_main',
            'pose_checkpoint = malbut_gazebo.pose_checkpoint:main',
            'nav2_startup_gate = malbut_gazebo.nav2_startup_gate:main',
        ],
    },
)
