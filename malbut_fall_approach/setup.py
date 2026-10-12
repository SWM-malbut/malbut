"""Package the fall check drive that the Manager runs as `fall_approach`."""

from setuptools import find_packages, setup


package_name = 'malbut_fall_approach'

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
    description='Fall check drive near a suspicion, to a whole-body view, and back',
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={'console_scripts': [
        'fall_approach = malbut_fall_approach.fall_approach_node:main',
    ]},
)
