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
        ('share/' + package_name, ['package.xml']),
    ] + collect_data_files('launch', 'urdf', 'rviz', 'meshes'),
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='ubuntu',
    maintainer_email='1270161395@qq.com',
    description='TODO: Package description',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
        ],
    },
)
