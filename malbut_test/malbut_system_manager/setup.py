"""Setuptools metadata for the Malbut system manager."""

from glob import glob
import os

from setuptools import find_packages, setup


package_name = 'malbut_system_manager'


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
            os.path.join('share', package_name, 'launch'),
            glob('launch/*.launch.py'),
        ),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Malbut Team',
    maintainer_email='sanggeunji0117@gmail.com',
    description='Manifest-driven mission and runtime state manager for Malbut',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'system_manager = '
            'malbut_system_manager.system_manager_node:main',
        ],
    },
)
