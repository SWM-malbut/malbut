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
        ('share/' + package_name, ['package.xml']),
    ] + collect_data_files(
        'launch',
        'urdf',
        'config',
        'worlds',
        'rviz',
        'maps',
        'meshes',
        'models',
    ),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='ubuntu',
    maintainer_email='ubuntu@todo.todo',
    description='Gazebo Fortress integration for the Malbut ROSOrin model',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'spawn_when_ready = malbut_gazebo.spawn_when_ready:main',
            'teleop_key_control = malbut_gazebo.teleop_key_control:main',
        ],
    },
)
