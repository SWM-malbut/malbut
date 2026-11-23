"""Architectural invariants for the decomposed monolith packages."""

import ast
from pathlib import Path

import pytest

from malbut_agent_server import (
    orchestrator as orchestrator_module,
    personal_memory as personal_memory_module,
    story_runtime_store as story_runtime_store_module,
)
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.personal_memory import PersonalMemory
from malbut_agent_server.story_runtime_store import StoryRuntimeStore

_PACKAGE_ROOT = Path(orchestrator_module.__file__).resolve().parent


def _assert_acyclic_package(
    subpackage: str,
    forbidden_parent: str,
    expected_min_modules: int,
) -> None:
    pkg_dir = _PACKAGE_ROOT / subpackage
    dotted_subpackage = subpackage.replace('/', '.')
    graph: dict[str, list[str]] = {}
    for path in sorted(pkg_dir.glob('*.py')):
        module_name = path.stem
        if module_name == '__init__':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == forbidden_parent:
                    raise AssertionError(
                        f'{subpackage}/{module_name}.py must not import {forbidden_parent}'
                    )
                prefix = f'malbut_agent_server.{dotted_subpackage}.'
                if node.module.startswith(prefix):
                    deps.add(node.module[len(prefix):].split('.')[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == forbidden_parent:
                        raise AssertionError(
                            f'{subpackage}/{module_name}.py must not import {forbidden_parent}'
                        )
        graph[module_name] = sorted(deps)

    assert len(graph) >= expected_min_modules
    visit_state: dict[str, int] = {}

    def _visit(node: str, stack: list[str]) -> None:
        visit_state[node] = 1
        for dep in graph.get(node, ()):
            if visit_state.get(dep) == 1:
                cycle = stack[stack.index(dep):] + [dep]
                raise AssertionError(
                    f'Circular dependency in {subpackage}: {cycle}'
                )
            if visit_state.get(dep, 0) == 0:
                _visit(dep, stack + [dep])
        visit_state[node] = 2

    for module_name in graph:
        if visit_state.get(module_name, 0) == 0:
            _visit(module_name, [module_name])


@pytest.mark.parametrize(
    ('subpackage', 'forbidden_parent', 'min_modules'),
    [
        ('orchestration', 'malbut_agent_server.orchestrator', 3),
        ('personal_memory_store', 'malbut_agent_server.personal_memory', 4),
        ('story_store', 'malbut_agent_server.story_runtime_store', 5),
        ('providers/openai', 'malbut_agent_server.providers.openai_responses', 4),
        ('communication', 'malbut_agent_server.ros_communication', 6),
        ('application/cloud_fall', 'malbut_agent_server.application.cloud_fall_monitor', 7),
        ('application/fall_sensor', 'malbut_agent_server.application.fall_flow_sensor', 3),
        ('dialogue', 'malbut_agent_server.speech_dialogue', 3),
    ],
)
def test_subpackages_form_strict_acyclic_dags(
    subpackage: str,
    forbidden_parent: str,
    min_modules: int,
) -> None:
    """Every extracted subpackage is acyclic and never imports its facade."""
    _assert_acyclic_package(subpackage, forbidden_parent, min_modules)


def test_store_facades_contain_no_direct_sql_and_reexport_public_api() -> None:
    """Thin facades delegate SQL to repositories and preserve public exports."""
    for module in (personal_memory_module, story_runtime_store_module):
        facade_path = Path(module.__file__).resolve()
        tree = ast.parse(
            facade_path.read_text(encoding='utf-8'),
            filename=str(facade_path),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != 'execute', (
                    f'{facade_path.name} facade must not run direct SQL queries'
                )

    expected_exports = {
        orchestrator_module: {
            'AgentOrchestrator',
            'ExpiredDecisionError',
            'MemoryChangedError',
            'OrchestrationResult',
            'ServerClarification',
            'StoryReplyBinding',
            'accepts_weather_context',
        },
        personal_memory_module: {
            'CONSENT_PROMPT',
            'STALE_MESSAGE',
            'YES',
            'NO',
            'CANCEL',
            'MemorySnapshot',
            'PersonalMemory',
            'compact',
            'selection',
            'direct_source',
            'management_request',
            'explicit_memory_management',
            'local_intent',
            'negated_management',
            'requested_operation',
            'self_name_correction',
            'fact_matches_source',
            'pet_owner_is_unambiguous',
            'preference_matches_source',
            'reply',
        },
        story_runtime_store_module: {
            'LEASE_SECONDS',
            'MAX_ATTEMPTS',
            'MAX_SOURCE_CHARS',
            'MAX_UPDATES',
            'MAX_PENDING',
            'FAILURE_CODES',
            'StoryRuntimeStore',
            '_ValidationSnapshot',
            '_normalized',
            '_refs_json',
            '_refs',
            '_digest',
            '_identity',
            '_message_key',
            '_source_object',
        },
    }
    for module, names in expected_exports.items():
        missing = {name for name in names if not hasattr(module, name)}
        assert not missing, f'{module.__name__} missing exports: {sorted(missing)}'


def test_refactored_modules_never_access_private_conversation_store_handles() -> None:
    """Refactored modules must use conversation_store.database/locked/transaction."""
    paths = [
        _PACKAGE_ROOT / 'automatic_memory_worker.py',
        _PACKAGE_ROOT / 'orchestrator.py',
        _PACKAGE_ROOT / 'personal_memory.py',
        _PACKAGE_ROOT / 'story_runtime_store.py',
        *sorted((_PACKAGE_ROOT / 'orchestration').glob('*.py')),
        *sorted((_PACKAGE_ROOT / 'personal_memory_store').glob('*.py')),
        *sorted((_PACKAGE_ROOT / 'story_store').glob('*.py')),
    ]
    forbidden_attrs = {'_lock', '_connection'}
    conversation_owners = {'conversation_store', 'conversations'}

    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr in forbidden_attrs):
                continue
            target = node.value
            owner_name = (
                target.id if isinstance(target, ast.Name)
                else target.attr if isinstance(target, ast.Attribute)
                else None
            )
            assert owner_name not in conversation_owners, (
                f'{path.relative_to(_PACKAGE_ROOT)} accesses '
                f'{owner_name}.{node.attr} directly instead of public database API'
            )


def test_conversation_store_public_database_and_read_write_transactions(
    tmp_path: Path,
) -> None:
    """SQLiteConversationStore exposes database, locked(), and read/write UoW."""
    store = SQLiteConversationStore(str(tmp_path / 'shared.sqlite3'))
    try:
        db = store.database
        assert db.connection is store._connection
        assert db.lock is store._lock

        with store.transaction(write=False) as read_conn:
            assert read_conn is db.connection
            assert read_conn.in_transaction is True
            row = read_conn.execute('SELECT 1').fetchone()
            assert row is not None and row[0] == 1
        assert db.connection.in_transaction is False

        with store.transaction(write=True) as write_conn:
            assert write_conn is db.connection
            assert write_conn.in_transaction is True
        assert db.connection.in_transaction is False

        with store.locked():
            assert db.connection.execute('SELECT 2').fetchone()[0] == 2
    finally:
        store.close()


def test_story_runtime_store_dynamic_clock_and_shared_memory_connection() -> None:
    """Dynamic _clock mutations propagate and :memory: stores bind cleanly."""
    now = [1000.0]
    conversations = SQLiteConversationStore(':memory:', clock=lambda: now[0])
    memory = SQLiteMemoryStore(':memory:')
    personal = PersonalMemory(memory, conversations)
    story = StoryRuntimeStore(conversations, memory, clock=lambda: now[0])
    try:
        assert memory._connection is conversations.database.connection
        assert personal.conversations is conversations
        assert story._now() == 1000.0

        later = [2500.0]
        story._clock = lambda: later[0]
        assert story._now() == 2500.0
        assert story._db.now() == 2500.0
    finally:
        conversations.close()


def _node_loc(path: Path, name: str, *, is_class: bool = False) -> int:
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    target_type = ast.ClassDef if is_class else (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, target_type) and node.name == name:
            assert node.end_lineno is not None
            return node.end_lineno - node.lineno + 1
    raise AssertionError(f'{name} not found in {path}')


def test_decomposed_runtime_facades_preserve_exports_and_stay_bounded() -> None:
    """Decomposed runtime facades keep public/test seams and prevent monolith regrowth."""
    from malbut_agent_server import ros_communication, speech_dialogue
    from malbut_agent_server.application import cloud_fall_monitor, fall_flow_sensor
    from malbut_agent_server.providers import openai_responses

    expected_exports = {
        openai_responses: {
            'MAX_PROVIDER_RESPONSE_BYTES',
            'OpenAIResponsesProvider',
            'PooledOpenAITransport',
            'REASONING_EFFORTS',
            'SPEECH_END_INSTRUCTIONS',
            'TEXT_DECISION_SCHEMA',
            'Transport',
            '_DEFAULT_URLLIB_TRANSPORT',
            '_NoRedirectHandler',
            '_TWO_PASS_FOLLOWUP_DIRECTIVE',
            '_TwoPassPrefix',
            '_insufficient_quota',
            '_is_fast_speech_call1',
        },
        ros_communication: {
            'ADDRESSEE_SERVICE',
            'CommandLines',
            'DEFAULT_CONVERSATION_DB',
            'DEFAULT_DB_PATH',
            'DEFAULT_SPEECH_USER',
            'END_DIALOGUE_TOPIC',
            'INPUT_STATUS_TOPIC',
            'MAX_COMMAND_BYTES',
            'MAX_PENDING_ADDRESSEE_REQUESTS',
            'RESPONSE_TOPIC',
            'TRANSCRIPT_TOPIC',
            'apply_command',
            'create_communication_node',
            'dialogue_settings_from_args',
            'main',
            'read_commands',
            'run_with_fall_rules',
            'spin_fall_rules',
        },
        cloud_fall_monitor: {
            'APPROACH_OUTCOMES',
            'CLEARED_PLACE_MAX_S',
            'CloudAnalysisStatus',
            'CloudFallMonitor',
            'GUARD_FAILURES',
            'PersonCheck',
            'RUNTIME_CLOUD_BLOCKS',
            '_inside',
            'associate_timed_finding',
        },
        fall_flow_sensor: {
            'AnalysisStatus',
            'FallFlowSensor',
            'FlowIncident',
            'GUARD_REASONS',
            'Look',
            'Photo',
            'RUNTIME_CLOUD_BLOCKS',
        },
        speech_dialogue: {
            'DialogueWorker',
            'ERROR_RESPONSE',
            'MAX_INTERRUPTION_IDS',
            'MAX_SPEECH_ID_LENGTH',
            'MEMORY_CHANGED_RESPONSE',
            'NAVIGATION_CHANGED_RESPONSE',
            'SESSION_ERROR_RESPONSE',
            'SpeechInputTooLongError',
            '_DialogueReply',
            'starts_new_conversation',
            'validate_dialogue_input',
            'validate_interruption_input',
        },
    }
    for module, names in expected_exports.items():
        missing = {name for name in names if not hasattr(module, name)}
        assert not missing, f'{module.__name__} missing exports: {sorted(missing)}'

    assert _node_loc(
        _PACKAGE_ROOT / 'application/cloud_fall_monitor.py', 'CloudFallMonitor', is_class=True,
    ) <= 700
    assert _node_loc(
        _PACKAGE_ROOT / 'providers/openai_responses.py', 'OpenAIResponsesProvider', is_class=True,
    ) <= 550
    assert _node_loc(
        _PACKAGE_ROOT / 'providers/openai_responses.py', '_prepare_payload',
    ) <= 80
    assert _node_loc(
        _PACKAGE_ROOT / 'ros_communication.py', 'create_communication_node',
    ) <= 150
    assert _node_loc(
        _PACKAGE_ROOT / 'application/fall_flow_sensor.py', 'FallFlowSensor', is_class=True,
    ) <= 600
    assert _node_loc(
        _PACKAGE_ROOT / 'speech_dialogue.py', 'DialogueWorker', is_class=True,
    ) <= 650
    assert _node_loc(
        _PACKAGE_ROOT / 'vlm_eval_metrics.py', '_summarize_configuration',
    ) <= 150
