"""Architectural invariants for the modular conversation_store package."""

import ast
from pathlib import Path

import pytest

from malbut_agent_server import conversation as conversation_module
from malbut_agent_server.conversation import (
    ConversationNotFoundError,
    ConversationStateError,
    SQLiteConversationStore,
)
from malbut_agent_server.conversation_store import (
    CompactionRepository,
    ConversationConfig,
    ConversationDatabase,
    SessionRepository,
)


class _SimulatedAbort(BaseException):
    """Non-Exception BaseException used to test rollback safety."""


def test_conversation_database_transaction_commits_and_rolls_back(
    tmp_path,
) -> None:
    """ConversationDatabase commits normally and rolls back any failure."""
    db = ConversationDatabase(str(tmp_path / 'uow.sqlite3'))
    try:
        with db.transaction() as conn:
            conn.execute(
                '''
                INSERT INTO conversation_sessions (
                    user_id, conversation_id, session_instance_id,
                    status, generation, revision,
                    created_at, updated_at, expires_at
                ) VALUES (
                    'user-a', 'conv-1', 'inst-1',
                    'active', 1, 0, 100.0, 100.0, 200.0
                )
                '''
            )
        assert db.connection.in_transaction is False
        row = db.connection.execute(
            "SELECT conversation_id FROM conversation_sessions WHERE conversation_id = 'conv-1'"
        ).fetchone()
        assert row is not None

        with pytest.raises(RuntimeError, match='boom'):
            with db.transaction() as conn:
                conn.execute(
                    '''
                    INSERT INTO conversation_sessions (
                        user_id, conversation_id, session_instance_id,
                        status, generation, revision,
                        created_at, updated_at, expires_at
                    ) VALUES (
                        'user-a', 'conv-err', 'inst-err',
                        'active', 1, 0, 100.0, 100.0, 200.0
                    )
                    '''
                )
                raise RuntimeError('boom')
        assert db.connection.in_transaction is False
        assert (
            db.connection.execute(
                "SELECT 1 FROM conversation_sessions WHERE conversation_id = 'conv-err'"
            ).fetchone()
            is None
        )

        with pytest.raises(_SimulatedAbort):
            with db.transaction() as conn:
                conn.execute(
                    '''
                    INSERT INTO conversation_sessions (
                        user_id, conversation_id, session_instance_id,
                        status, generation, revision,
                        created_at, updated_at, expires_at
                    ) VALUES (
                        'user-a', 'conv-abort', 'inst-abort',
                        'active', 1, 0, 100.0, 100.0, 200.0
                    )
                    '''
                )
                raise _SimulatedAbort('cancelled')
        assert db.connection.in_transaction is False
        assert (
            db.connection.execute(
                "SELECT 1 FROM conversation_sessions WHERE conversation_id = 'conv-abort'"
            ).fetchone()
            is None
        )
    finally:
        db.close()


def test_conversation_database_reentrant_transaction_preserves_outer_atomicity(
    tmp_path,
) -> None:
    """Nested transaction() joins the outer transaction without early commit."""
    db = ConversationDatabase(str(tmp_path / 'reentrant.sqlite3'))
    try:
        with pytest.raises(RuntimeError, match='outer failed'):
            with db.transaction() as outer_conn:
                outer_conn.execute(
                    '''
                    INSERT INTO conversation_sessions (
                        user_id, conversation_id, session_instance_id,
                        status, generation, revision,
                        created_at, updated_at, expires_at
                    ) VALUES (
                        'user-a', 'conv-outer', 'inst-outer',
                        'active', 1, 0, 100.0, 100.0, 200.0
                    )
                    '''
                )
                with db.transaction() as inner_conn:
                    assert inner_conn is outer_conn
                    inner_conn.execute(
                        '''
                        INSERT INTO conversation_sessions (
                            user_id, conversation_id, session_instance_id,
                            status, generation, revision,
                            created_at, updated_at, expires_at
                        ) VALUES (
                            'user-a', 'conv-inner', 'inst-inner',
                            'active', 1, 0, 100.0, 100.0, 200.0
                        )
                        '''
                    )
                # Inner exit must not have committed the outer transaction.
                assert db.connection.in_transaction is True
                raise RuntimeError('outer failed')

        assert db.connection.in_transaction is False
        rows = db.connection.execute(
            'SELECT conversation_id FROM conversation_sessions'
        ).fetchall()
        assert rows == []
    finally:
        db.close()


def test_dynamic_clock_and_config_mutations_propagate_to_repositories(
    tmp_path,
) -> None:
    """Mutating _clock or runtime limits on the facade updates all repos."""
    now = [1000.0]
    store = SQLiteConversationStore(
        str(tmp_path / 'dynamic.sqlite3'),
        ttl_seconds=60,
        clock=lambda: now[0],
    )
    try:
        session = store.create('user-a', 'conv-1')
        assert session.expires_at == 1060.0

        # Replace _clock after construction (used by existing tests).
        later = [1065.0]
        store._clock = lambda: later[0]
        expired = store.get('user-a', 'conv-1')
        assert expired.status == 'expired'

        # Mutate config attributes after construction and verify propagation.
        store.semantic_context = True
        store.max_turns_per_session = 10
        assert store._sessions._config.semantic_context is True
        assert store._compaction._config.semantic_context is True
        assert store._turns._config.max_turns_per_session == 10
    finally:
        store.close()


def test_repositories_operate_independently_on_shared_database(
    tmp_path,
) -> None:
    """SessionRepository and CompactionRepository work cleanly standalone."""
    now = [500.0]
    db = ConversationDatabase(
        str(tmp_path / 'standalone.sqlite3'),
        clock=lambda: now[0],
    )
    config = ConversationConfig(ttl_seconds=60)
    sessions = SessionRepository(db, config)
    compaction = CompactionRepository(db, sessions, config)
    try:
        created = sessions.create('user-a', 'conv-1')
        assert created.status == 'active'
        assert created.generation == 1
        assert compaction.get_summary('user-a', 'conv-1') is None

        reset = sessions.reset('user-a', 'conv-1')
        assert reset.generation == 2
        assert reset.revision == 1

        now[0] = 600.0
        assert sessions.purge_expired() == 1
        with pytest.raises(ConversationStateError):
            sessions.close_session('user-a', 'conv-1')

        assert sessions.delete('user-a', 'conv-1') is True
        with pytest.raises(ConversationNotFoundError):
            sessions.get('user-a', 'conv-1')
    finally:
        db.close()


def test_conversation_store_internal_imports_form_strict_acyclic_dag() -> None:
    """Internal conversation_store modules have zero cycles or facade back-refs."""
    pkg_dir = (
        Path(conversation_module.__file__).resolve().parent
        / 'conversation_store'
    )
    graph = {}
    for path in sorted(pkg_dir.glob('*.py')):
        module_name = path.stem
        if module_name == '__init__':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == 'malbut_agent_server.conversation':
                    raise AssertionError(
                        f'{module_name}.py must not import conversation.py'
                    )
                prefix = 'malbut_agent_server.conversation_store.'
                if node.module.startswith(prefix):
                    deps.add(node.module[len(prefix):].split('.')[0])
        graph[module_name] = sorted(deps)

    assert len(graph) >= 10
    visit_state = {}

    def _visit(node: str, stack: list) -> None:
        visit_state[node] = 1
        for dep in graph.get(node, ()):
            if visit_state.get(dep) == 1:
                cycle = stack[stack.index(dep):] + [dep]
                raise AssertionError(f'Circular dependency detected: {cycle}')
            if visit_state.get(dep, 0) == 0:
                _visit(dep, stack + [dep])
        visit_state[node] = 2

    for module_name in graph:
        if visit_state.get(module_name, 0) == 0:
            _visit(module_name, [module_name])


def test_conversation_facade_has_no_direct_sql_and_reexports_public_api() -> None:
    """conversation.py stays a thin facade with all public exports intact."""
    facade_path = Path(conversation_module.__file__).resolve()
    source = facade_path.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(facade_path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != 'execute', (
                'conversation.py facade must not run direct SQL queries'
            )

    expected_exports = {
        'SQLiteConversationStore',
        'ConversationNotFoundError',
        'ConversationStateError',
        'ConversationConflictError',
        'ConversationChangedError',
        'ConfirmationSchemaError',
        'ConfirmationIntentNotFoundError',
        'ConfirmationIntentConflictError',
        'ConfirmationIntentAlreadyTerminalError',
        'ConversationSession',
        'ConversationTurn',
        'ConversationSummary',
        'BeginTurnToken',
        'BeginTurnResult',
        'ConversationSnapshot',
        'TextTurnRequestClaim',
        'is_context_limit_response',
        'CONFIRMATION_SCHEMA_METADATA_SQL',
        'CONFIRMATION_INTENTS_SQL',
        'CONFIRMATION_RESPONSE_OWNER_INDEX_SQL',
        'CONFIRMATION_ONE_PENDING_INDEX_SQL',
        'TEXT_TURN_REQUEST_CLAIMS_SQL',
    }
    missing = {
        name for name in expected_exports
        if not hasattr(conversation_module, name)
    }
    assert not missing, f'Missing facade exports: {sorted(missing)}'
