from setuptools import find_packages, setup


package_name = 'malbut_agent_server'


setup(
    name=package_name,
    version='0.3.0',
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
        (
            'share/' + package_name + '/docs/jira',
            ['docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md'],
        ),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='SWM Malbut contributors',
    maintainer_email='maintainers@example.com',
    url='https://github.com/SWM-malbut/malbut',
    description=(
        'Provider-neutral agent boundary and safety contract for Malbut'
    ),
    license='Apache-2.0',
    tests_require=['pytest'],
)
