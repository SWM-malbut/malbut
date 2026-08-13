"""Package and install the Malbut agent server and its evidence docs."""

from setuptools import find_packages, setup


package_name = 'malbut_agent_server'


setup(
    name=package_name,
    version='0.5.0',
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
                'docs/jira/SWM25-73_AGENT_TOOL_GATEWAY.md',
                'docs/jira/SWM25-75_LONG_TERM_MEMORY_INTEGRATION.md',
                'docs/jira/SWM25-76_VOICE_CONVERSATION_PIPELINE.md',
                'docs/jira/SWM25-77_EMOTION_EXPRESSION_INTEGRATION.md',
            ],
        ),
        (
            'share/' + package_name + '/docs',
            ['docs/LLM_AGENT_IMPLEMENTATION_ACCEPTANCE_CRITERIA.md'],
        ),
        (
            'share/' + package_name + '/docs/evaluations',
            [
                'docs/evaluations/'
                'SWM25-72_OPENAI_EVALUATION_2026-08-05.md',
                'docs/evaluations/'
                'SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md',
                'docs/evaluations/'
                'SWM25-69_74_REVALIDATION_2026-08-12.md',
                'docs/evaluations/'
                'SWM25-69_74_REVALIDATION_2026-08-12.html',
                'docs/evaluations/'
                'SYNTHETIC_CONVERSATION_TRACE_2026-08-13.md',
                'docs/evaluations/'
                'SWM25-75_77_300X_OFFLINE_2026-08-13.md',
                'docs/evaluations/'
                'SWM25-75_77_HARDENING_2026-08-13.md',
            ],
        ),
        (
            'share/' + package_name + '/docs/evaluations/artifacts',
            [
                'docs/evaluations/artifacts/'
                'SWM25-69_74_300X_OFFLINE_2026-08-12.json',
                'docs/evaluations/artifacts/'
                'SWM25-69_74_REVALIDATION_2026-08-12.artifact.json',
                'docs/evaluations/artifacts/'
                'SWM25-69_74_REVALIDATION_2026-08-12.delivery.json',
                'docs/evaluations/artifacts/'
                'SWM25-69_74_REVALIDATION_2026-08-12.sql',
                'docs/evaluations/artifacts/'
                'SYNTHETIC_CONVERSATION_TRACE_2026-08-13.json',
                'docs/evaluations/artifacts/'
                'SWM25-75_77_300X_OFFLINE_2026-08-13.json',
            ],
        ),
        (
            'share/' + package_name + '/docs/worklogs',
            ['docs/worklogs/OVERNIGHT_2026-08-13.md'],
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
