import os
from setuptools import setup

package_name = 'malbut_description'


def collect_data_files(*directories):
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
    version='0.0.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, [
            'package.xml',
            'LICENSE',
            'THIRD_PARTY_NOTICES.md',
        ]),
    ] + collect_data_files('config', 'launch', 'meshes', 'urdf', 'rviz'),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SANGGEUN JI',
    maintainer_email='sanggeunji0117@gmail.com',
    description='Hiwonder-mesh-based ROSOrin Ultimate Mecanum description',
    license='Apache-2.0 AND LicenseRef-Hiwonder-ROSOrin',
    tests_require=['pytest'],
)
