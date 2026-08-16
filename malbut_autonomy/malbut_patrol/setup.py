import os

from setuptools import find_packages, setup


package_name = 'malbut_patrol'


def collect_data_files(*directories):
    """Collect package data while preserving its source directory layout."""
    data_files = []
    for directory in directories:
        for root, dirnames, filenames in os.walk(directory):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname != '__pycache__' and not dirname.startswith('.')
            ]
            sources = [
                os.path.join(root, filename)
                for filename in filenames
                if not filename.startswith('.')
            ]
            if sources:
                data_files.append((
                    os.path.join('share', package_name, root),
                    sources,
                ))
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
        (
            'share/' + package_name,
            ['package.xml', 'README.md'],
        ),
    ] + collect_data_files('config', 'launch'),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Malbut Team',
    maintainer_email='ubuntu@todo.todo',
    description='Scheduled waypoint patrol orchestration for Malbut',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'patrol_manager = malbut_patrol.patrol_manager:main',
        ],
    },
)
