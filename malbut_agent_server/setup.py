from setuptools import find_packages, setup


package_name = 'malbut_agent_server'


setup(
    name=package_name,
    version='0.5.0',
    packages=find_packages(exclude=['test']),
    package_data={package_name: ['data/*.json', 'data/*.jsonl']},
    data_files=[
        (
            'share/ament_index/resource_index/packages',
            ['resource/' + package_name],
        ),
        (
            'share/' + package_name,
            ['package.xml', 'README.md', '.env.example', 'requirements-openai.txt'],
        ),
        (
            'share/' + package_name + '/docs',
            ['docs/SEMANTIC_MEMORY.md'],
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
                'docs/jira/SWM25-128_CLEAN_BASELINE.md',
                'docs/jira/SWM25-131_TEXT_CONFIRMATION_RAI.md',
                'docs/jira/SWM25-132_APPROVED_NAV2_EXECUTION.md',
                'docs/jira/'
                'SWM25-152_ROLE_MODEL_CONFIGURATION.md',
                'docs/jira/SWM25-171_LONG_TERM_MEMORY.md',
            ],
        ),
        (
            'share/' + package_name + '/docs/fall',
            ['docs/fall/fall_detection.md', 'docs/fall/fall_storage_api.md',
             'docs/fall/fall_runtime.md', 'docs/fall/fall_decision_policy.md',
             'docs/fall/fall_subject_observation.md', 'docs/fall/fall_robot_preparation.md'],
        ),
        (
            'share/' + package_name + '/config',
            ['config/fall_runtime.example.json'],
        ),
        (
            'share/' + package_name + '/docs/evaluations',
            [
                'docs/evaluations/'
                'SWM25-72_OPENAI_EVALUATION_2026-08-05.md',
                'docs/evaluations/'
                'SWM25-72_OPENAI_POSTFIX_PARITY_EVALUATION_2026-08-05.md',
                'docs/evaluations/FALL_DETECTION_VLM_REQUIREMENTS.md',
                'docs/evaluations/VLM_EVALUATION_HARNESS.md',
            ],
        ),
    ],
    install_requires=['setuptools', 'tiktoken>=0.7,<1'],
    extras_require={
        'vlm-nova': ['boto3>=1.35.0,<2'],
        'fall-cloud': ['aiohttp>=3.9,<4', 'Pillow>=9'],
    },
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
                'weather = '
                'malbut_agent_server.weather_action:main'
            ),
            (
                'agent_communication = '
                'malbut_agent_server.ros_communication:main'
            ),
            (
                'speech_receiver = '
                'malbut_agent_server.speech_receiver:main'
            ),
            (
                'malbut-agent-server = '
                'malbut_agent_server.cli:server_main'
            ),
            (
                'malbut-agent-eval = '
                'malbut_agent_server.eval_runner:main'
            ),
            (
                'malbut-front-route-inspect = '
                'malbut_agent_server.front_route_inspector:main'
            ),
            (
                'malbut-vlm-eval = '
                'malbut_agent_server.vlm_eval_runner:main'
            ),
            (
                'malbut-vlm-infer = '
                'malbut_agent_server.vlm_inference_runner:main'
            ),
            (
                'malbut-fall-upload = '
                'malbut_agent_server.fall_upload_worker:main'
            ),
            (
                'malbut-fall-monitor = '
                'malbut_agent_server.ros_fall_monitor:main'
            ),
        ],
    },
)
