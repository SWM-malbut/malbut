"""Install the automatic mapping Action server and launch file."""

from glob import glob

from setuptools import find_packages, setup


setup(
    name='malbut_autoslam', version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/malbut_autoslam']),
        ('share/malbut_autoslam', ['package.xml', 'README.md']),
        ('share/malbut_autoslam/launch', glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools'], zip_safe=True,
    maintainer='Malbut Team', maintainer_email='sanggeunji0117@gmail.com',
    description='Cancellable frontier exploration and navigation map saving',
    license='Apache-2.0', tests_require=['pytest'],
    entry_points={'console_scripts': [
        'autoslam = malbut_autoslam.autoslam_node:main',
    ]},
)
