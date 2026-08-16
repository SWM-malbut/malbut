from setuptools import find_packages, setup


package_name = 'malbut_vision'


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
    maintainer='ubuntu',
    maintainer_email='ubuntu@todo.todo',
    description='ROS 2 OpenCV learning tools for the Malbut simulated camera.',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'color_detect = malbut_vision.color_detect:main',
            'lab_threshold = malbut_vision.lab_threshold:main',
        ],
    },
)
