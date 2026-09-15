"""Setuptools metadata for Malbut streaming TTS."""

from setuptools import find_packages, setup


package_name = 'malbut_tts'


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
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SWM Malbut contributors',
    maintainer_email='maintainers@example.com',
    description='Queue and stream local speech with ROS playback controls.',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'tts_receiver = malbut_tts.receiver:main',
            'tts_node = malbut_tts.node:main',
            'tts_smoke = malbut_tts.smoke:main',
        ],
    },
)
