import os

from setuptools import find_packages, setup


package_name = 'malbut_scenarios'


def collect_data_files(*directories):
    """Collect scenario launch, configuration, and map assets."""
    data_files = []
    for directory in directories:
        for root, dirnames, filenames in os.walk(directory):
            dirnames[:] = [
                name
                for name in dirnames
                if name != '__pycache__' and not name.startswith('.')
            ]
            sources = [
                os.path.join(root, name)
                for name in filenames
                if not name.startswith('.')
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
    ] + collect_data_files('config', 'launch', 'maps'),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SANGGEUN JI',
    maintainer_email='sanggeunji0117@gmail.com',
    description='Integrated autonomous-driving demonstrations for Malbut',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'autonomous_driving_manager = '
            'malbut_scenarios.autonomous_driving_manager:main',
            'prepare_named_navigation_fixture = '
            'malbut_scenarios.named_navigation_fixture:main',
            'wait_for_simulation = '
            'malbut_scenarios.wait_for_simulation:main',
        ],
    },
)
