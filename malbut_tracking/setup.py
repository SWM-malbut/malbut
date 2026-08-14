"""Setuptools metadata for the Malbut target-tracking package."""

import os
from glob import glob

from setuptools import find_packages, setup


package_name = 'malbut_tracking'


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
    ],
    install_requires=['setuptools', 'numpy'],
    zip_safe=True,
    maintainer='SANGGEUN JI',
    maintainer_email='sanggeunji0117@gmail.com',
    description='Sensor-driven person following through standard Nav2 actions',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'person_follower = '
            'malbut_tracking.person_follower_node:main',
        ],
    },
)
