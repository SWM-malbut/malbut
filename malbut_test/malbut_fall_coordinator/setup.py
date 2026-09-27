"""Package the always-on fall coordinator separately from the mission manager."""

from setuptools import find_packages, setup


package_name = 'malbut_fall_coordinator'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml', 'README.md']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Malbut Team',
    maintainer_email='sanggeunji0117@gmail.com',
    description='Fall settings and incident confirmation coordination',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={'console_scripts': [
        'fall_coordinator = malbut_fall_coordinator.node:main',
    ]},
)
