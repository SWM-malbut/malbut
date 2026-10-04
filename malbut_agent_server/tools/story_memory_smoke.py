#!/usr/bin/env python3
"""Offline story-memory prototype using only fictional, temporary records.

Run from the repository root:
    PYTHONPATH=malbut_agent_server python3.12 malbut_agent_server/tools/story_memory_smoke.py

The summaries and story grouping below are hand-written fixtures, not model
outputs. No network, API keys, robot actions or production databases are used.
The existing conversation module requires Python 3.10+; tested with 3.12.
"""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.story_memory import SQLiteStoryMemoryStore, StoryEntry
from malbut_agent_server.story_memory_sources import make_source_ref


def run_demo():
    """Persist a story, reopen storage and inspect it from a later session."""
    with tempfile.TemporaryDirectory(prefix='malbut-story-demo-') as temporary:
        path = str(Path(temporary) / 'synthetic.sqlite3')
        conversation = SQLiteConversationStore(path, semantic_context=True)
        memory = SQLiteStoryMemoryStore(path)
        source_connection = sqlite3.connect(path)
        owner = 'fictional-user'

        def complete(session_id, turn_id, user_text, assistant_text):
            conversation.create(owner, session_id)
            begun = conversation.begin_turn(
                user_id=owner, conversation_id=session_id, turn_id=turn_id,
                request_id=turn_id, request_fingerprint=turn_id,
                user_content=user_text,
            )
            _, turn = conversation.complete_turn(
                begun.token, assistant_text,
                {'decision': {'type': 'message', 'message': assistant_text}},
            )
            return tuple(make_source_ref(
                source_connection, owner, turn.conversation_id,
                turn.session_instance_id, turn.generation, turn.turn_id, role,
            ) for role in ('user', 'assistant'))

        try:
            first, _ = complete(
                'visit-one', 'exhibition-1',
                '오늘 전시를 봤어. 작품은 좋았는데 사람이 많아서 좀 지쳤어.',
                '작품은 좋았지만 붐벼서 지쳤구나.',
            )
            memory.set_consent(owner, True, [first])
            first_state = (StoryEntry(
                '첫 방문에는 작품이 좋았지만 사람이 많아 지쳤다고 말했다.',
                'experience', 'user', 'stated', (first.key,),
            ),)
            memory.begin_checkpoint(owner, 'exhibition', 'checkpoint-1', [first])
            memory.commit_checkpoint(
                owner, 'checkpoint-1', '같은 전시의 방문 이야기', ['전시'],
                first_state, first_state,
            )

            # Neither a reset nor a process restart should expire story memory.
            conversation.reset(owner, 'visit-one')
            memory.close()
            memory = SQLiteStoryMemoryStore(path)
            second, _ = complete(
                'visit-two', 'exhibition-2',
                '그 전시 다시 가봤어. 오늘은 한산해서 편안하게 봤어.',
                '이번에는 천천히 볼 수 있었구나.',
            )
            resumed = memory.search(owner, '전시')
            if len(resumed) != 1 or len(memory.get_evidence(owner, 'exhibition')) != 1:
                raise RuntimeError('cross-session source retrieval failed')
            memory.set_consent(owner, True, [first, second])
            second_state = (StoryEntry(
                '재방문에는 한산해서 편안하게 관람했다고 말했다.',
                'experience', 'user', 'stated', (second.key,),
            ),)
            memory.begin_checkpoint(owner, 'exhibition', 'checkpoint-2', [second])
            exhibition = memory.commit_checkpoint(
                owner, 'checkpoint-2', '같은 전시의 방문 이야기', ['전시'],
                second_state, second_state,
            )

            user_ref, assistant_ref = complete(
                'manual-discussion', 'manual-1',
                '보드게임 설명서는 고치기 쉽게 한 장 요약부터 만들어 보자.',
                '그림 중심으로 구성하는 방법도 있어.',
            )
            memory.set_consent(
                owner, True, [first, second, user_ref, assistant_ref],
            )
            plan = (
                StoryEntry(
                    '수정하기 쉬워서 한 장 요약부터 만들기로 했다.',
                    'decision', 'user', 'confirmed', (user_ref.key,),
                ),
                StoryEntry(
                    '그림 중심 구성은 말벗의 제안이며 아직 선택하지 않았다.',
                    'decision', 'assistant', 'proposed', (assistant_ref.key,),
                ),
            )
            memory.begin_checkpoint(
                owner, 'manual', 'checkpoint-3', [user_ref, assistant_ref],
            )
            manual = memory.commit_checkpoint(
                owner, 'checkpoint-3', '보드게임 설명서', ['설명서', '한 장 요약'],
                plan, plan,
            )
            if memory.search('another-user', '전시'):
                raise RuntimeError('user isolation failed')
            memory.set_consent(owner, False)
            if memory.search(owner, '전시'):
                raise RuntimeError('disabled memory was returned')
            memory.set_consent(owner, True)
            if not memory.search(owner, '전시'):
                raise RuntimeError('disabled memory was not retained')
            return {
                'mode': 'offline_fixture_prototype',
                'automatic_extraction': False,
                'production_integration': False,
                'checks': {
                    'new_session_after_restart': True,
                    'two_visits_in_one_story': len(exhibition.episodes) == 2,
                    'proposal_not_user_decision': manual.current[1].status == 'proposed',
                    'other_user_isolated': True,
                    'off_stops_reuse_but_retains_memory': True,
                },
                'exhibition': asdict(exhibition),
                'manual': asdict(manual),
                'limits': [
                    '요약과 이야기 묶음은 사람이 작성한 가상 입력이다.',
                    '운영 연결·자동 추출·원문 일부 삭제는 아직 구현하지 않았다.',
                    '출처별 동의 범위만 검증하며 실제 동의 화면은 없다.',
                ],
            }
        finally:
            source_connection.close()
            memory.close()
            conversation.close()


if __name__ == '__main__':
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))
