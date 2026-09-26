from setuptools import find_packages, setup

setup(
    name='malbut_resource_monitor', version='0.1.0',
    packages=find_packages(exclude=['test']),
    package_data={'malbut_resource_monitor': ['viewer.html', 'topics.json']},
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/malbut_resource_monitor']),
        ('share/malbut_resource_monitor', ['package.xml', 'README.md']),
    ],
    install_requires=['setuptools'], zip_safe=True,
    maintainer='Malbut Team', maintainer_email='sanggeunji0117@gmail.com',
    description='Passive resource recorder and offline log viewer', license='Apache-2.0',
    entry_points={'console_scripts': [
        'resource_recorder = malbut_resource_monitor.collector:main',
        'resource_viewer = malbut_resource_monitor.viewer:main',
    ]},
)
