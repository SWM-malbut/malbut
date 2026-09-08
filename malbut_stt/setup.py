"""Package metadata for the Malbut STT ROS node."""

from setuptools import find_packages, setup


package_name = 'malbut_stt'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml', 'README.md', 'requirements.txt']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SWM Malbut contributors',
    maintainer_email='maintainers@example.com',
    description='Wake-triggered speech transcription and ROS topic publication',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={'console_scripts': ['stt = malbut_stt.node:main']},
)
