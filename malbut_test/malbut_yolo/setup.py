"""Install Malbut's shared upstream detector integration."""

from glob import glob
from setuptools import setup


setup(
    name='malbut_yolo',
    version='0.1.0',
    packages=['malbut_yolo'],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/malbut_yolo']),
        ('share/malbut_yolo', ['package.xml', 'README.md']),
        ('share/malbut_yolo/launch', glob('launch/*.launch.py')),
        ('share/malbut_yolo/config', glob('config/*.yaml')),
        ('share/malbut_yolo/scripts', glob('scripts/*.sh')),
    ],
    install_requires=['setuptools'],
    tests_require=['pytest'],
    zip_safe=True,
    entry_points={'console_scripts': [
        'yolo_node = malbut_yolo.yolo_node:main',
    ]},
)
