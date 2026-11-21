"""Personal memory policy constants, snapshot DTO, and schema initialization."""

from dataclasses import dataclass
from typing import Any

from malbut_agent_server.conversation_preferences import (
    initialize_schema as initialize_preferences_schema,
)
from malbut_agent_server.conversation_store.database import ConversationDatabase

CONSENT_PROMPT = (
    '이름·호칭·반려동물 정보·취향처럼 직접 말한 일상 정보를 앞으로 '
    '자동으로 저장하고 다음 대화에 활용해도 될까요? 언제든 조회·정정·삭제하거나 '
    '개인화를 중단할 수 있어요.'
)
STALE_MESSAGE = (
    '기억 정보가 변경되어 이전 답변을 전달하지 않았어요. 다시 말씀해 주세요.'
)
YES = {
    '응',
    '네',
    '예',
    '좋아',
    '동의해',
    '동의합니다',
    '동의해요',
    '기억사용에동의하기',
    'yes',
    'ㅇㅇ',
}
NO = {'아니', '아니요', '싫어', '동의안해', '거절', 'no', '사용하지않기'}
CANCEL = NO | {'취소', '취소할게', '취소해줘', '그만'}


@dataclass
class MemorySnapshot:
    """One user-scoped input snapshot held across inference."""

    state: dict[str, Any]
    memories: list[Any]
    history: list[Any]
    summary: Any
    dependencies: set[str]
    context: dict[str, Any]
    pending: dict[str, Any] | None
    source: dict[str, Any]
    facts_deferred: bool = False
    recall_blocked: bool = False


def initialize_personal_memory_schema(db: ConversationDatabase) -> None:
    """Create memory turn-state and pending-question tables on the shared DB."""
    from malbut_agent_server.memory import initialize_memory_schema

    with db.locked():
        conn = db.connection
        initialize_memory_schema(conn)
        initialize_preferences_schema(conn)
        conn.execute(
            (
                'CREATE TABLE IF NOT EXISTS memory_turn_state (\n'
                '                user_id TEXT NOT NULL, '
                'request_id TEXT NOT NULL,\n'
                '                conversation_id TEXT NOT NULL, '
                'session_instance_id TEXT NOT NULL,\n'
                '                generation INTEGER NOT NULL, '
                'turn_id TEXT NOT NULL,\n'
                '                revision INTEGER NOT NULL, '
                'dependencies_json TEXT NOT NULL,\n'
                '                created_at REAL NOT NULL,\n'
                '                PRIMARY KEY (user_id, '
                'request_id))'
            )
        )
        conn.execute('''CREATE TABLE IF NOT EXISTS memory_questions (
            user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            session_instance_id TEXT NOT NULL, generation INTEGER NOT NULL,
            revision INTEGER NOT NULL, kind TEXT NOT NULL,
            proposal_json TEXT NOT NULL, source_json TEXT NOT NULL,
            PRIMARY KEY (user_id, conversation_id))''')
        if 'memory_revision' not in {
            row[1]
            for row in conn.execute('PRAGMA table_info(memory_questions)')
        }:
            conn.execute(
                (
                    'ALTER TABLE memory_questions ADD COLUMN '
                    'memory_revision INTEGER NOT NULL DEFAULT 0'
                )
            )
        conn.commit()
