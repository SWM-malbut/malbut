"""Package the Malbut local microphone transcript source."""

from setuptools import find_packages, setup


package_name = 'malbut_voice'


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
            ['package.xml', 'README.md', 'requirements-stt.lock'],
        ),
        (
            'share/' + package_name + '/config',
            ['config/microphone-stt.example.json'],
        ),
        (
            'share/' + package_name + '/config/models',
            [
                'config/models/'
                'faster-whisper-small-536b0662.manifest.json'
            ],
        ),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SWM Malbut contributors',
    maintainer_email='maintainers@example.com',
    url='https://github.com/SWM-malbut/malbut',
    description=(
        'Explicit one-shot hardware microphone to local transcript source'
    ),
    license='Apache-2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            (
                'malbut-microphone-stt = '
                'malbut_voice.cli:microphone_stt_main'
            ),
        ],
    },
)
