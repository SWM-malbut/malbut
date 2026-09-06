"""Setuptools metadata for the Malbut target-tracking package."""

import os
from glob import glob

from setuptools import find_packages, setup


package_name = 'malbut_tracking'


def benchmark_data_files():
    """Install benchmark assets under the tracking package share tree."""
    data_files = []
    root = os.path.join(package_name, 'benchmark')
    for directory in ('config', 'launch', 'actors'):
        source_root = os.path.join(root, directory)
        for current, dirnames, filenames in os.walk(source_root):
            dirnames[:] = [
                name for name in dirnames if name != '__pycache__'
            ]
            sources = [
                os.path.join(current, name)
                for name in filenames
                if not name.startswith('.')
            ]
            if sources:
                relative = os.path.relpath(current, root)
                data_files.append(
                    (
                        os.path.join(
                            'share', package_name, 'benchmark', relative
                        ),
                        sources,
                    )
                )
    return data_files


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
    ] + benchmark_data_files(),
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
            'person_tracking_benchmark = '
            'malbut_tracking.benchmark.evaluator:main',
        ],
    },
)
