"""Setuptools metadata for the reusable person re-identification package."""

from glob import glob

from setuptools import find_packages, setup


package_name = 'malbut_reid'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
         ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml', 'README.md']),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
        ('share/' + package_name + '/scripts', glob('scripts/*.sh')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SANGGEUN JI',
    maintainer_email='sanggeunji0117@gmail.com',
    description='Shared image-only person re-identification from YOLO topics',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={'console_scripts': [
        'person_reidentifier = malbut_reid.person_reidentifier_node:main',
    ]},
)
