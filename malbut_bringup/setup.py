"""Install the real-robot launch and one-shot readiness check."""

from glob import glob

from setuptools import find_packages, setup


setup(
    name='malbut_bringup',
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
         ['resource/malbut_bringup']),
        ('share/malbut_bringup', ['package.xml', 'README.md']),
        ('share/malbut_bringup/launch', glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Malbut Team',
    maintainer_email='sanggeunji0117@gmail.com',
    description='ROSOrin hardware and Malbut application startup',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={'console_scripts': [
        'wait_for_robot = malbut_bringup.readiness:main',
    ]},
)
