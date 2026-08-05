from setuptools import find_packages, setup


package_name = 'malbut_agent_server'


setup(
    name=package_name,
    version='0.4.0',
    packages=find_packages(exclude=['test']),
    package_data={package_name: ['data/*.jsonl']},
    data_files=[
        (
            'share/ament_index/resource_index/packages',
            ['resource/' + package_name],
        ),
        (
            'share/' + package_name,
            ['package.xml', 'README.md', '.env.example'],
        ),
        (
            'share/' + package_name + '/docs/jira',
            [
                'docs/jira/SWM25-69_CONVERSATION_AGENT_CONTRACT.md',
                'docs/jira/SWM25-69_INTERFACE_APPROVAL_GUIDE.md',
                'docs/jira/SWM25-70_MULTITURN_CONVERSATION_SESSION.md',
                'docs/jira/SWM25-71_USER_CONTEXT_INTEGRATION.md',
                'docs/jira/SWM25-72_LLM_PROVIDER_INTEGRATION.md',
            ],
        ),
        (
            'share/' + package_name + '/docs/evaluations',
            [
                'docs/evaluations/'
                'SWM25-72_OPENAI_EVALUATION_2026-08-05.md',
                'docs/evaluations/'
                'SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md',
            ],
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
    entry_points={
        'console_scripts': [
            (
                'malbut-agent-server = '
                'malbut_agent_server.cli:server_main'
            ),
            (
                'malbut-agent-eval = '
                'malbut_agent_server.eval_runner:main'
            ),
        ],
    },
)
