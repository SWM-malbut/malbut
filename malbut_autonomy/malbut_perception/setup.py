"""Setuptools metadata for the malbut_perception ROS package."""

import os
from glob import glob

from setuptools import find_packages, setup


package_name = 'malbut_perception'


setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        (
            'share/ament_index/resource_index/packages',
            ['resource/' + package_name],
        ),
        ('share/' + package_name, ['package.xml', 'README.md']),
        (
            os.path.join('share', package_name, 'config'),
            glob('config/*.yaml'),
        ),
        (
            os.path.join('share', package_name, 'launch'),
            glob('launch/*.launch.py'),
        ),
        (
            os.path.join('share', package_name, 'scripts'),
            glob('scripts/*'),
        ),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SANGGEUN JI',
    maintainer_email='sanggeunji0117@gmail.com',
    description=(
        'Sensor-only RGB-D person detection and localization for Malbut'
    ),
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'person_localizer = '
            'malbut_perception.target_localizer_node:main',
        ],
    },
)
