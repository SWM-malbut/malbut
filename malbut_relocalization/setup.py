"""Install the relocalization Action server."""

from setuptools import find_packages, setup


setup(
    name='malbut_relocalization', version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/malbut_relocalization']),
        ('share/malbut_relocalization', ['package.xml', 'README.md']),
    ],
    install_requires=['setuptools'], zip_safe=True,
    maintainer='Malbut Team', maintainer_email='sanggeunji0117@gmail.com',
    description='Find the robot pose on the selected saved map and remember its last pose',
    license='Apache-2.0', tests_require=['pytest'],
    entry_points={'console_scripts': [
        'relocalization = malbut_relocalization.relocalization_node:main',
    ]},
)
