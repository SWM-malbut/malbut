"""Snapshot reads, provenance filtering, source review, and freshness checks."""

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import replace
from typing import Any, Callable

from malbut_agent_server.conversation_preferences import (
    SETTINGS_PROMPT,
    initial_settings as read_initial_settings,
    response_setting_fact,
    response_settings,
    save_initial_settings,
)
from malbut_agent_server.conversation_recall import (
    is_self_name_question,
    self_name_lookup,
    speech_recall_focus,
    speech_recall_prohibited,
)
from malbut_agent_server.memory_source_review import attach_source_review
from malbut_agent_server.personal_memory_store.intent import (
    _PET_ALIAS,
    _PET_ORDINALS,
    _pet_name_correction,
    _pet_name_declaration,
    _pet_name_question,
    _self_introduction,
    _stored_identity_question,
    compact,
    direct_source,
    fact_matches_source,
    local_intent,
    management_request,
    negated_management,
    reply,
    requested_operation,
    selection,
    self_name_correction,
)
from malbut_agent_server.personal_memory_store.models import (
    CANCEL,
    NO,
    YES,
    MemorySnapshot,
)
from malbut_agent_server.schemas import AgentDecision, ValidationError
from malbut_agent_server.summarization import (
    ExtractiveConversationSummarizer,
    SummarySourceTurn,
)


class PersonalMemoryContext:
    """Read-side snapshots, lineage redaction, pending questions, and freshness."""

    def __init__(self, memory_store: Any, conversation_store: Any) -> None:
        self.memory = memory_store
        self.conversations = conversation_store

    def initial_settings(self, user_id: str) -> dict[str, Any]:
        """Read the four conversation defaults without enabling memory."""
        with self.conversations.locked():
            return read_initial_settings(
                self.conversations.database.connection, user_id,
            )

    def set_initial_settings(
        self, user_id: str, settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist explicitly selected defaults independently of consent."""
        with self.conversations.transaction(write=True) as conn:
            return save_initial_settings(conn, user_id, settings)

    def snapshot(
        self,
        request: Any,
        token: Any,
        history: Any,
        summary: Any,
        memory_limit: int = 5,
        *,
        defer_facts: bool = False,
    ) -> MemorySnapshot:
        """Read eligible history and memories under one read transaction."""
        source: dict[str, Any] = {
            'user_id': request.user_id,
            'conversation_id': token.conversation_id,
            'session_instance_id': token.session_instance_id,
            'generation': token.generation,
            'turn_id': token.turn_id,
            'request_id': token.request_id,
            'text': request.utterance,
        }
        pet_cutoff: float = 0.0
        with self.conversations.transaction(write=False) as conn:
            state = self.memory.policy_state(
                request.user_id, connection=conn,
            )
            effective_settings, retained_settings, settings_edit = response_settings(
                conn, token, request.utterance,
            )
            source['response_settings'] = retained_settings
            source['settings_edit'] = settings_edit
            pending_row = conn.execute(
                (
                    'SELECT * FROM memory_questions\n'
                    '                    WHERE user_id=? AND '
                    'conversation_id=?\n'
                    '                    AND session_instance_id=? '
                    'AND generation=? AND revision=?'
                ),
                (
                    request.user_id,
                    token.conversation_id,
                    token.session_instance_id,
                    token.generation,
                    token.revision,
                ),
            ).fetchone()
            pending: dict[str, Any] | None = dict(pending_row) if pending_row else None
            if pending:
                pending['proposal'] = json.loads(
                    pending.pop('proposal_json')
                )
                pending['source'] = json.loads(pending.pop('source_json'))
                if pending['memory_revision'] != state['revision'] or (
                    pending['kind'] == 'target'
                    and selection(request.utterance) is None
                    and compact(request.utterance).rstrip('.!?。')
                    not in CANCEL
                ):
                    pending = None
            # A complete read question may contain the same 기억/저장 stems
            # as management commands (e.g. "우리 강아지 이름 기억나?").
            read_question = (
                defer_facts
                and ('기억' in request.utterance or '저장' in request.utterance)
                and speech_recall_focus(request.utterance, (), None) is not None
            )
            blocked = bool(defer_facts and speech_recall_prohibited(request.utterance))
            manage = not (read_question or blocked) and management_request(request.utterance)
            deferred = bool(
                defer_facts and state['enabled'] and not blocked and not manage
                and not pending and not settings_edit
                and local_intent(request.utterance) is None
                and (read_question or not any(
                    requested_operation(request.utterance, operation)
                    for operation in ('remember', 'correct', 'forget')
                ))
            )
            correction: re.Match[str] | None = None
            stored_identity: tuple[str, ...] | None = None
            if state['enabled'] and not blocked and not pending:
                correction = self_name_correction(request.utterance)
                stored_identity = _stored_identity_question(request.utterance)
            name_question = (
                state['enabled'] and not blocked and not pending
                and (is_self_name_question(request.utterance) or (
                    self_name_lookup(request.utterance)
                    and speech_recall_focus(request.utterance, (), None) == 'name'
                ))
            )
            pet = (
                _pet_name_question(request.utterance)
                if state['enabled'] and not blocked and not pending else None
            )
            pet_edit = (
                _pet_name_correction(request.utterance)
                if state['enabled'] and not blocked and not pending else None
            )
            if pet or pet_edit:
                deferred = manage = False
                pet_subject = pet if pet is not None else pet_edit['pet']  # type: ignore[index]
                memories, pet_cutoff = self._pet_names(request.user_id, pet_subject, conn)
            elif correction or stored_identity or name_question:
                deferred = False
                memories = [
                    record
                    for kind in stored_identity or ('name',)
                    for record in self._active_identity(request.user_id, kind, conn)
                ]
            elif state['enabled'] and not (blocked or deferred):
                memories = list(self.memory.search(
                    request.user_id, request.utterance, limit=memory_limit, connection=conn,
                ))
            else:
                memories = []
            candidates = memories
            if manage and not (correction or stored_identity or name_question):
                candidates = list(
                    self.memory.search(
                        request.user_id,
                        request.utterance,
                        limit=10,
                        connection=conn,
                    )
                )
            filtered, clean_summary, deps = self._context(
                request.user_id,
                token,
                state,
                list(history),
                summary,
                conn,
            )
            name_evidence_only = False
            if name_question and not stored_identity and len(memories) > 1:
                stored_identity = ('name',)
            if name_question and not stored_identity:
                filtered = self._name_history(request.user_id, conn, memories, filtered)
                clean_summary = None
                # Indirect questions (e.g. whether the speaker was remembered)
                # keep a conversational answer even when there is no evidence.
                name_evidence_only = (
                    bool(memories or filtered)
                    or not is_self_name_question(request.utterance)
                )
                if not name_evidence_only:
                    stored_identity = ('name',)
            if pet:
                # Current slots are authoritative. On a miss keep only a direct
                # statement of this pet's name since the last invalidation.
                subjects = (pet, _PET_ALIAS.get(pet))
                filtered = [
                    replace(turn, assistant_content='', response={})
                    for turn in filtered
                    if not memories and '저장' not in request.utterance
                    and turn.created_at > max(pet_cutoff, state['legacy_cutoff'])
                    and (said := _pet_name_declaration(turn.user_content))
                    and said['pet'] in subjects
                ]
                clean_summary = None
        if pet:
            source['pet_name_question'] = pet
        spelling = None
        declaration = _pet_name_declaration(request.utterance)
        if declaration:
            stem, ending = declaration['stem'], declaration['ending']
            # A consonant-final noun takes 이야. The same sounds can also be
            # a name ending in 이 followed by 야. An established spelling wins.
            if not ending.startswith('이') or (ord(stem[-1]) - ord('가')) % 28:
                spelling = {
                    'subject': declaration['pet'],
                    'name_before_copula': stem,
                    'copula': ending,
                }
                source['pet_name_spelling'] = spelling
        if stored_identity:
            source['stored_identity'] = stored_identity
        deps.update(record.id for record in memories)
        context: dict[str, Any] = {
            'enabled': state['enabled'],
            'response_settings': effective_settings,
            'pending_question': (
                {'kind': pending['kind'], 'proposal': pending['proposal']}
                if pending
                else None
            ),
            'memories': self._model_candidates(candidates),
            'allowed_kinds': ['name', 'nickname', 'pet', 'preference'],
        }
        if spelling:
            context['pet_name_spelling'] = spelling
        if pet:
            context['pet_name_evidence_only'] = True
        if name_evidence_only:
            context['self_name_evidence_only'] = True
        if correction:
            context['self_name_correction'] = {'new_name_verbatim': correction['new']}
        return MemorySnapshot(
            state,
            memories,
            filtered,
            clean_summary,
            deps,
            context,
            pending,
            source,
            deferred,
            blocked,
        )

    def _pet_names(
        self, user_id: str, pet: str, conn: sqlite3.Connection,
    ) -> tuple[list[Any], float]:
        subjects = (pet, _PET_ALIAS[pet]) if pet in _PET_ALIAS else (pet,)
        slots = [json.dumps(['pet', subject, 'name'], ensure_ascii=False) for subject in subjects]
        slots.extend(
            json.dumps(['pet', ordinal + space + subject, 'name'], ensure_ascii=False)
            for subject in subjects for ordinal in _PET_ORDINALS for space in ('', ' ')
        )
        placeholders = ','.join('?' for _ in slots)
        now = time.time()
        rows = conn.execute(
            'SELECT m.* FROM memory_fact_slots s JOIN memories m ON m.id=s.memory_id AND m.user_id=s.user_id '
            f'WHERE s.user_id=? AND s.slot_key IN ({placeholders}) AND (m.expires_at IS NULL OR m.expires_at>?)',
            (user_id, *slots, now),
        ).fetchall()
        if rows:
            return [self.memory._record(row) for row in rows], 0
        cutoff = float(conn.execute(
            "SELECT COALESCE(MAX(cutoff),0) FROM memory_pet_name_cutoffs "
            f"WHERE user_id=? AND slot_hash IN ('*',{placeholders})",
            (user_id, *(hashlib.sha256(slot.encode()).hexdigest() for slot in slots)),
        ).fetchone()[0])
        legacy = (
            r'\s*(?:(?:우리|저희|내|제)\s*(?:집\s*)?)?(?:' + '|'.join(subjects)
            + r')\s*(?:의\s*)?이름(?:은|는|:)\s*[가-힣A-Za-z0-9]+[.!。]?\s*'
        )
        legacy_rows = conn.execute(
            "SELECT m.* FROM memories m WHERE m.user_id=? AND m.kind IN ('pet','fact') AND m.created_at>? "
            "AND (m.expires_at IS NULL OR m.expires_at>?) AND NOT EXISTS "
            "(SELECT 1 FROM memory_fact_slots s WHERE s.memory_id=m.id)",
            (user_id, cutoff, now),
        )
        return [
            self.memory._record(row)
            for row in legacy_rows
            if row['content'].count('이름') == 1
            and not json.loads(row['metadata_json']).get('fact')
            and re.fullmatch(legacy, row['content'])
        ], cutoff

    def _active_identity(
        self, user_id: str, kind: str, conn: sqlite3.Connection,
    ) -> list[Any]:
        rows = conn.execute(
            'SELECT m.* FROM memory_fact_slots s JOIN memories m ON m.id=s.memory_id '
            'WHERE s.user_id=? AND m.user_id=s.user_id AND m.kind=? '
            'AND (m.expires_at IS NULL OR m.expires_at>?)',
            (user_id, kind, time.time()),
        )
        records = (self.memory._record(row) for row in rows)
        owned = [
            record for record in records
            if record.metadata['fact']['attribute'] == kind
            and record.metadata['fact']['subject'] in {'user', 'self', '사용자', '나', '저'}
        ]
        if kind == 'name':
            legacy_rows = conn.execute(
                "SELECT m.* FROM memories m JOIN memory_policy_state p ON p.user_id=m.user_id "
                "WHERE m.user_id=? AND m.kind='name' AND m.created_at>MAX(p.self_name_cutoff,p.legacy_cutoff) "
                'AND (m.expires_at IS NULL OR m.expires_at>?) AND NOT EXISTS '
                '(SELECT 1 FROM memory_fact_slots s WHERE s.memory_id=m.id)',
                (user_id, time.time()),
            )
            for row in legacy_rows:
                record = self.memory._record(row)
                if not record.metadata.get('fact') and _self_introduction(record.content):
                    owned.append(record)
        return owned

    @staticmethod
    def _name_history(
        user_id: str,
        conn: sqlite3.Connection,
        records: list[Any],
        history: list[Any],
    ) -> list[Any]:
        # An active structured fact establishes the current name. Otherwise
        # only direct introductions made after invalidation can supply evidence.
        if any(record.metadata.get('fact') for record in records):
            return []
        cutoff = conn.execute(
            'SELECT MAX(self_name_cutoff,legacy_cutoff) FROM memory_policy_state WHERE user_id=?',
            (user_id,),
        ).fetchone()[0]
        return [
            replace(turn, assistant_content='', response={})
            for turn in history
            if turn.created_at > cutoff and _self_introduction(turn.user_content)
        ]

    @staticmethod
    def _model_candidates(records: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                'id': record.id,
                'kind': record.kind,
                'content': record.content,
                'fact': {
                    key: value
                    for key, value in record.metadata.get('fact', {}).items()
                    if key != 'evidence'
                },
            }
            for record in records
        ]

    def _check_deferred_turn(
        self, conn: sqlite3.Connection, token: Any, snapshot: MemorySnapshot,
    ) -> None:
        if (not snapshot.facts_deferred or not snapshot.state['enabled']
                or self.memory.policy_state(token.user_id, connection=conn) != snapshot.state):
            raise ValidationError('memory_changed')
        pending = conn.execute(
            "SELECT 1 FROM conversation_turns t JOIN conversation_sessions s "
            "ON s.user_id=t.user_id AND s.conversation_id=t.conversation_id "
            "AND s.session_instance_id=t.session_instance_id AND s.generation=t.generation "
            "WHERE t.user_id=? AND t.request_id=? AND t.conversation_id=? AND t.session_instance_id=? "
            "AND t.generation=? AND t.turn_id=? AND t.status='pending' AND s.status='active' "
            "AND s.revision=? AND s.expires_at>?",
            (
                token.user_id, token.request_id, token.conversation_id,
                token.session_instance_id, token.generation, token.turn_id,
                token.revision, self.conversations._now(),
            ),
        ).fetchone()
        if pending is None:
            raise ValidationError('memory_changed')
        if any(record.expires_at is not None and record.expires_at <= time.time()
               for record in snapshot.memories):
            raise ValidationError('memory_changed')

    def recall_deferred_facts(
        self,
        token: Any,
        snapshot: MemorySnapshot,
        focus: str,
        limit: int,
        *,
        previous_name_id: str | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]], Callable[[], None]] | None:
        """Acquire this turn's fact readset in one consent-checked snapshot."""
        with self.memory._transaction() as conn:
            self._check_deferred_turn(conn, token, snapshot)
            if focus == 'name' and self_name_lookup(snapshot.source['text']):
                records = self._active_identity(token.user_id, 'name', conn)
            elif previous_name_id is None:
                records, _ = self.memory.search_with_revision(
                    token.user_id, snapshot.source['text'], limit=limit, connection=conn,
                    fact_kind=None if focus == 'related' else focus,
                )
            else:
                # Only a current, structured own-name slot can corroborate the
                # old receipt. A miss leaves the snapshot untouched for routing.
                row = conn.execute(
                    'SELECT m.* FROM memory_fact_slots s JOIN memories m ON m.id=s.memory_id '
                    'WHERE s.user_id=? AND m.user_id=s.user_id AND s.memory_id=? AND s.slot_key=? '
                    'AND (m.expires_at IS NULL OR m.expires_at>?)',
                    (token.user_id, previous_name_id, json.dumps(['name', 'user', 'name']), time.time()),
                ).fetchone()
                if row is None:
                    return None
                records = [self.memory._record(row)]
        snapshot.memories = records
        snapshot.dependencies.update(record.id for record in records)
        snapshot.context['memories'] = self._model_candidates(records)

        def guard() -> None:
            with self.memory._transaction() as conn:
                self._check_deferred_turn(conn, token, snapshot)

        return records, snapshot.context['memories'], guard

    @staticmethod
    def prepare_source_review(
        request: Any,
        snapshot: MemorySnapshot,
        result: Any,
        reviewer: Any,
    ) -> Any:
        """Review unresolved meanings before opening the commit transaction."""
        proposal = result.provider_result.memory_proposal
        if (
            reviewer is None or snapshot.pending
            or result.decision.type not in {'message', 'clarification'}
            or proposal is None
            or proposal['operation'] not in {'remember', 'correct'}
            or not proposal['facts']
            or not direct_source(request.utterance)
            or negated_management(request.utterance)
        ):
            return None
        explicit_save = requested_operation(request.utterance, 'remember')
        explicit_correct = requested_operation(request.utterance, 'correct')
        if not snapshot.state['enabled'] and not explicit_save:
            return None
        if self_name_correction(request.utterance) or _pet_name_correction(request.utterance):
            # An exact name edit is checked against the primary fact and live
            # owned slot at commit. A reviewer cannot widen its authority.
            return None
        if proposal['operation'] == 'correct' and not explicit_correct:
            return None
        if not explicit_save and not explicit_correct and re.search(
            r'[?？]|뭐|누구|어떤|어떻게|일까|인가요', request.utterance,
        ):
            return None
        if (
            proposal['evidence']
            and proposal['evidence'] not in request.utterance
        ):
            return None
        attributes = {
            'name': {'name'}, 'nickname': {'nickname'},
            'preference': {'likes', 'dislikes', 'preference'},
            'pet': {'name', 'species', 'breed', 'age', 'birthday', 'color',
                    'likes', 'dislikes', 'preference'},
        }
        unresolved = []
        for fact in proposal['facts']:
            if response_setting_fact(fact):
                continue
            if (
                fact['evidence'] not in request.utterance
                or not compact(fact['value'])
                or compact(fact['value']) not in compact(fact['evidence'])
                or fact['attribute'] not in attributes[fact['kind']]
                or (fact['kind'] != 'pet' and compact(fact['subject']) not in {
                    'user', 'self', '사용자', '나', '저',
                })
                or (fact['kind'] == 'pet' and compact(fact['subject']) in {
                    'user', 'self', '사용자', '나', '저',
                })
            ):
                return None
            if not fact_matches_source(fact, request.utterance):
                unresolved.append(fact)
        if not unresolved:
            return None
        outcome = reviewer.review(request, unresolved)
        attach_source_review(snapshot.source, outcome.facts)
        provider = result.provider_result
        provider.latency_ms += outcome.elapsed_ms
        # Account for review tokens without changing the public metadata shape.
        # Unknown usage must stay unknown, rather than under-reporting a call.
        usage: dict[str, int | None] = {}
        for key in ('input_tokens', 'output_tokens', 'total_tokens'):
            old = getattr(provider.usage, key)
            extra = (
                getattr(outcome.response.usage, key)
                if outcome.response is not None else None
            )
            usage[key] = (
                old + extra
                if old is not None and extra is not None else None
            )
        provider.usage = replace(provider.usage, **usage)
        return outcome

    def _context(
        self,
        user_id: str,
        token: Any,
        state: dict[str, Any],
        history: list[Any],
        summary: Any,
        conn: sqlite3.Connection,
    ) -> tuple[list[Any], Any, set[str]]:
        from malbut_agent_server.conversation import is_context_limit_response

        def usable_turn(turn: Any) -> Any:
            # Keep the receipt and ordinal for compaction, but never re-admit a
            # rejected input. The stored original and valid summary stay intact.
            return (
                replace(turn, user_content='')
                if is_context_limit_response(turn.response) else turn
            )

        history = [usable_turn(turn) for turn in history]
        # ponytail: privacy checks scan this generation's raw lineage even after
        # compaction; batch/cache dependencies if long-session latency warrants.
        invalid = self.memory.invalidated_ids(user_id, connection=conn)
        params = (
            user_id,
            token.conversation_id,
            token.session_instance_id,
            token.generation,
            token.ordinal,
        )
        rows = conn.execute(
            '''SELECT t.request_id, t.created_at, s.dependencies_json,
                CASE WHEN s.request_id IS NULL THEN t.response_json ELSE NULL END
                    AS legacy_response_json
            FROM conversation_turns t
            LEFT JOIN memory_turn_state s
                ON s.user_id=t.user_id AND s.request_id=t.request_id
            WHERE t.user_id=? AND t.conversation_id=? AND t.session_instance_id=?
            AND t.generation=? AND t.status='completed' AND t.ordinal<?
            ORDER BY t.ordinal''',
            params,
        ).fetchall()
        deps: set[str] = set()
        redacted = False
        blocked_requests: set[str] = set()
        legacy_dependencies: set[str] = set()
        for row in rows:
            if row['dependencies_json'] is not None:
                item_deps = set(json.loads(row['dependencies_json']))
            else:
                legacy_raw = row['legacy_response_json']
                legacy_response = json.loads(legacy_raw) if legacy_raw else {}
                legacy_dependencies.update(
                    self._legacy_dependencies(legacy_response)
                )
                item_deps = set(legacy_dependencies)
            blocked = (
                row['created_at'] <= state['legacy_cutoff']
                or bool(item_deps & invalid)
                or (not state['enabled'] and bool(item_deps))
            )
            if blocked:
                redacted = True
                blocked_requests.add(row['request_id'])
            else:
                deps.update(item_deps)
        if not redacted:
            return history, summary, deps
        full_rows = conn.execute(
            '''SELECT t.* FROM conversation_turns t
            WHERE user_id=? AND conversation_id=? AND session_instance_id=?
            AND generation=? AND status='completed' AND ordinal<?
            ORDER BY ordinal''',
            params,
        ).fetchall()
        eligible = []
        for row in full_rows:
            item = self.conversations._turn_from_row(row)
            if item.request_id in blocked_requests:
                item = replace(
                    item, user_content='', assistant_content='', response={}
                )
            eligible.append(usable_turn(item))
        if self.conversations.semantic_context:
            if summary is not None:
                row = conn.execute(
                    '''SELECT state_json FROM conversation_summaries
                    WHERE user_id=? AND conversation_id=?
                    AND session_instance_id=? AND generation=?
                    AND summary_id=? AND summary_revision=?''',
                    (
                        user_id, token.conversation_id,
                        token.session_instance_id, token.generation,
                        summary.summary_id, summary.summary_revision,
                    ),
                ).fetchone()
                try:
                    compacted_state = json.loads(row['state_json']) if row else {}
                    if compacted_state.get('memory_policy_state') == state:
                        return (
                            [item for item in eligible
                             if item.ordinal > summary.source_end_ordinal],
                            summary, deps,
                        )
                except (TypeError, ValueError, AttributeError):
                    pass
            return eligible, None, deps
        window = self.conversations.history_limit
        recent = eligible[-window:]
        prefix = eligible[:-window]
        clean_summary = None
        if prefix and summary is not None:
            generated = ExtractiveConversationSummarizer().update(
                '',
                [
                    SummarySourceTurn(
                        t.ordinal,
                        t.turn_id,
                        t.user_content,
                        t.assistant_content,
                    )
                    for t in prefix
                ],
                prefix[0].ordinal,
                prefix[-1].ordinal,
                len(prefix),
                self.conversations.summary_max_chars,
            )
            clean_summary = replace(
                summary,
                content=generated.content,
                source_digest=hashlib.sha256(
                    generated.content.encode()
                ).hexdigest(),
                summarizer=generated.algorithm,
            )
        return recent, clean_summary, deps

    @staticmethod
    def local_decision(
        request: Any, snapshot: MemorySnapshot,
    ) -> AgentDecision | None:
        """Keep clear controls and replies to consent questions model-free."""
        edit = snapshot.source.get('settings_edit')
        if edit and edit['scope'] in {'defaults', 'invalid'}:
            if edit['scope'] == 'invalid' or not edit['patch']:
                return reply(SETTINGS_PROMPT, True)
            return reply('선택한 대화 취향을 초기 설정에 저장했어요. 기억 사용 동의는 바꾸지 않았어요.')
        if pet := snapshot.source.get('pet_name_question'):
            if len(snapshot.memories) > 1:
                return reply(f'저장된 이름이 여러 개예요. 어느 {pet}의 이름이 궁금하세요?', True)
            if not (snapshot.memories or snapshot.history):
                return reply(f'저장된 {pet} 이름을 확인할 수 없어요.')
        if identities := snapshot.source.get('stored_identity'):
            # A question about what is *stored* is answered by the active slot.
            # A previous correction may still be mentioned in story or dialogue.
            parts = []
            clarify = False
            for kind in identities:
                label = '본명' if kind == 'name' else '호칭'
                records = [record for record in snapshot.memories if record.kind == kind]
                if len(records) == 1:
                    record = records[0]
                    value = (
                        record.metadata['fact']['value']
                        if record.metadata.get('fact')
                        else _self_introduction(record.content)
                    )
                    parts.append(f'저장된 {label}은 {value}입니다.')
                elif records:
                    parts.append(f'저장된 {label} 후보가 여러 개예요. 어떤 {label}을 사용해야 할까요?')
                    clarify = True
                else:
                    parts.append(f'현재 저장된 {label}을 확인할 수 없어요.')
            return reply(' '.join(parts), clarify)
        if local_intent(request.utterance) is not None:
            return reply('기억 설정 요청을 확인했어요.')
        answer = compact(request.utterance).rstrip('.!?。')
        if snapshot.pending and answer in YES | NO:
            return reply('기억 관리 질문에 대한 답을 확인했어요.')
        if answer == '기억사용에동의하기':
            return reply('기억 설정 요청을 확인했어요.')
        if snapshot.pending and snapshot.pending['kind'] == 'target':
            return reply('관리할 기억의 선택을 확인했어요.')
        return None

    def pending_question(
        self,
        user_id: str,
        conversation_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Return a question still bound to the user's live conversation."""
        if connection is not None:
            return self._pending_question(connection, user_id, conversation_id)
        with self.conversations.transaction(write=False) as conn:
            return self._pending_question(conn, user_id, conversation_id)

    def _pending_question(
        self, conn: sqlite3.Connection, user_id: str, conversation_id: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            (
                'SELECT q.* FROM memory_questions q\n'
                '                    JOIN conversation_sessions '
                's\n'
                '                    ON q.user_id=s.user_id AND '
                'q.conversation_id=s.conversation_id\n'
                '                    AND '
                'q.session_instance_id=s.session_instance_id\n'
                '                    AND '
                'q.generation=s.generation AND '
                'q.revision=s.revision\n'
                '                    WHERE q.user_id=? AND '
                'q.conversation_id=?\n'
                "                    AND s.status='active' AND "
                's.expires_at>?'
            ),
            (user_id, conversation_id, self.conversations._now()),
        ).fetchone()
        state = self.memory.policy_state(user_id, connection=conn)
        valid = row and row['memory_revision'] == state['revision']
        return dict(row) if valid else None

    def assert_fresh(self, user_id: str, request_id: str) -> None:
        """Reject stale cached or queued answers without executing again."""
        with self.conversations.locked():
            conn = self.conversations.database.connection
            stamp = conn.execute(
                '''SELECT revision FROM memory_turn_state
                WHERE user_id=? AND request_id=?''',
                (user_id, request_id),
            ).fetchone()
            state = self.memory.policy_state(user_id, connection=conn)
            if stamp is None:
                old = conn.execute(
                    (
                        'SELECT response_json FROM conversation_turns\n'
                        '                    WHERE user_id=? AND '
                        "request_id=? AND status='completed' "
                    ),
                    (user_id, request_id),
                ).fetchone()
                if old and self._legacy_dependencies(json.loads(old[0])):
                    raise ValidationError('memory_changed')
            if (stamp is None and state['revision'] != 0) or (
                stamp is not None and stamp['revision'] != state['revision']
            ):
                raise ValidationError('memory_changed')

    @staticmethod
    def _legacy_dependencies(response: dict[str, Any]) -> set[str]:
        """Read pre-migration personalized provenance without replaying it."""
        public = response.get('public', response)
        memory = public.get('memory', {})
        return {key for key in memory.get('ids', []) if isinstance(key, str)}

    def _question(
        self,
        conn: sqlite3.Connection,
        token: Any,
        kind: str,
        proposal: dict[str, Any],
        source: dict[str, Any],
    ) -> None:
        conn.execute(
            '''INSERT OR REPLACE INTO memory_questions
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                token.revision + 1,
                kind,
                json.dumps(proposal, ensure_ascii=False),
                json.dumps(source, ensure_ascii=False),
                self.memory.policy_state(token.user_id, connection=conn)[
                    'revision'
                ],
            ),
        )
