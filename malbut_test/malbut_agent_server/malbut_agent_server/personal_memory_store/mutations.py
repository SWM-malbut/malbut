"""Transactional personal-memory mutations, target selection, and lineage stamping."""

import json
import logging
import re
import sqlite3
import time
from typing import Any

from malbut_agent_server.conversation_preferences import (
    commit_settings,
    response_setting_fact,
)
from malbut_agent_server.memory_contract import validate_memory_proposal
from malbut_agent_server.memory_source_review import source_review_matches
from malbut_agent_server.personal_memory_store.context import PersonalMemoryContext
from malbut_agent_server.personal_memory_store.intent import (
    _PET_ALIAS,
    _correct_name_fact,
    _missing_memory_followup,
    _pet_name_correction,
    _self_introduction,
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
    CONSENT_PROMPT,
    NO,
    YES,
    MemorySnapshot,
)
from malbut_agent_server.schemas import AgentDecision, ValidationError

# Preserve the historical logger name consumed by telemetry and quiet-save tests.
_LOGGER = logging.getLogger('malbut_agent_server.personal_memory')


class PersonalMemoryMutations:
    """Apply validated memory mutations inside the caller's SQLite transaction."""

    def __init__(
        self,
        memory_store: Any,
        conversation_store: Any,
        context_repo: PersonalMemoryContext,
    ) -> None:
        self.memory = memory_store
        self.conversations = conversation_store
        self._ctx = context_repo

    def commit(
        self,
        request: Any,
        token: Any,
        snapshot: MemorySnapshot,
        result: Any,
        conn: sqlite3.Connection,
    ) -> tuple[str, dict[str, Any]]:
        """Apply memory and final answer within the caller's transaction."""
        from malbut_agent_server.conversation import is_context_limit_response

        if (
            self.memory.policy_state(request.user_id, connection=conn)
            != snapshot.state
        ):
            raise ValidationError('memory_changed')
        context_limited = is_context_limit_response(result.to_persisted_dict())
        if not context_limited:
            commit_settings(
                conn, token, snapshot.source['response_settings'],
                snapshot.source['settings_edit'],
            )
        # Another runtime may have attached a delayed fact to an earlier turn
        # while this turn was in inference. Its text was already part of this
        # conversation, so merge newly attached lineage without changing the
        # frozen model inputs or reply. Destructive writes still fail the
        # epoch check above, within this same transaction.
        _, _, inherited_dependencies = self._ctx._context(
            request.user_id, token, snapshot.state, snapshot.history,
            snapshot.summary, conn,
        )
        if context_limited:
            # A rejected input neither answers nor replaces a pending question.
            conn.execute('''UPDATE memory_questions SET revision=?
                WHERE user_id=? AND conversation_id=? AND session_instance_id=?
                AND generation=? AND revision=? AND memory_revision=?''', (
                token.revision + 1, request.user_id, request.conversation_id,
                token.session_instance_id, token.generation, token.revision,
                snapshot.state['revision'],
            ))
        else:
            conn.execute(
                (
                    'DELETE FROM memory_questions WHERE user_id=? '
                    'AND conversation_id=?'
                ),
                (request.user_id, request.conversation_id),
            )
        response_setting = bool(snapshot.source.get('settings_edit'))
        weather_setting = (
            result.raw_decision.tool_name == 'set_weather_location'
            and result.decision.reason in {
                'weather_location_saved', 'weather_location_ambiguous',
                'weather_location_not_found', 'weather_location_unavailable',
            }
        )
        # Explicit robot configuration is not a personal-memory management request.
        initial_setting = (
            response_setting
            and snapshot.source['settings_edit']['scope'] in {'defaults', 'invalid'}
        )
        decision, added = (
            (None, [])
            if context_limited or weather_setting or initial_setting
            else self._apply(request, token, snapshot, result, conn)
        )
        claim = (
            r'(기억|저장|삭제|정정|수정|개인화).{0,16}'
            r'(했|하였|완료|해\s*뒀|해\s*두었|됐|할게|해\s*둘게|하겠|해둘께)'
        )
        # Weather setting replies are constructed from Manager results, not model claims.
        if (decision is None and not weather_setting and not initial_setting
                and re.search(claim, result.decision.message)):
            _LOGGER.info('memory_policy reason=unverified_completion')
            decision = _missing_memory_followup(request.utterance)
            if decision is None:
                # Automatic candidates do not request storage receipts.
                # Keep normal conversational sentences; never repeat a model's
                # uncommitted completion claim or future storage promise.
                parts = re.split(
                    r'(?<=[.!?。])\s*|\n+', result.decision.message,
                )
                message = ' '.join(
                    part for part in parts
                    if part.strip() and not re.search(claim, part)
                )
                decision = reply(message or '말씀해 주셔서 고마워요.')
        if decision is not None:
            result.decision = decision
            result.raw_decision = decision
            result.provider_result.decision = decision
            result.state_trusted = False
        state = self.memory.policy_state(request.user_id, connection=conn)
        result.memory_revision = state['revision']
        dependencies = (
            snapshot.dependencies | inherited_dependencies | set(added)
        )
        for record in (self.memory.list_for_user(request.user_id, connection=conn) if added else ()):
            if record.id not in added:
                continue
            origin = record.metadata.get('source', {}).get('request_id')
            row = conn.execute(
                '''SELECT dependencies_json FROM memory_turn_state
                WHERE user_id=? AND request_id=?''',
                (request.user_id, origin),
            ).fetchone()
            if row is not None:
                origin_deps = set(json.loads(row[0])) | {record.id}
                conn.execute(
                    '''UPDATE memory_turn_state SET dependencies_json=?
                    WHERE user_id=? AND request_id=?''',
                    (json.dumps(sorted(origin_deps)), request.user_id, origin),
                )
        conn.execute(
            (
                'INSERT INTO memory_turn_state VALUES (?, ?, ?, '
                '?, ?, ?, ?, ?, ?)'
            ),
            (
                request.user_id,
                request.request_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                token.turn_id,
                state['revision'],
                json.dumps(sorted(dependencies)),
                time.time(),
            ),
        )
        return result.decision.message, result.to_persisted_dict()

    def _apply(
        self,
        request: Any,
        token: Any,
        snapshot: MemorySnapshot,
        result: Any,
        conn: sqlite3.Connection,
        *,
        _automatic_insert: bool = False,
    ) -> tuple[AgentDecision | None, list[str]]:
        text = request.utterance
        pending = snapshot.pending
        answer = compact(text).rstrip('.!?。')
        explicit = (
            management_request(text) or requested_operation(text, 'remember')
            or (snapshot.recall_blocked and any(
                requested_operation(text, op) for op in ('forget', 'correct')
            ))
        )
        source = snapshot.source
        proposal = result.provider_result.memory_proposal
        control = local_intent(text)
        if not pending and answer == '기억사용에동의하기':
            control = 'enable'
        if type(_automatic_insert) is not bool:
            raise ValidationError('automatic insert mode must be boolean')
        if _automatic_insert and (
            pending is not None or control is not None
            or not isinstance(proposal, dict)
            or proposal.get('operation') != 'remember'
            or proposal.get('target_ids') or proposal.get('query')
            or explicit
        ):
            raise ValidationError('automatic insert cannot manage memories')
        if (
            isinstance(proposal, dict)
            and proposal.get('operation') == 'remember'
            and control is None
            and not negated_management(text)
        ):
            # Words such as "기억력이" are not instructions to save a fact.
            # Keep explicit correction/deletion requests and pending controls
            # visible even when a provider labels their proposal "remember".
            explicit = any(
                requested_operation(text, operation)
                for operation in ('remember', 'correct', 'forget')
            )
        confirmed_correction = False
        selected_target = False
        if pending and pending['kind'] == 'target' and control is None:
            if answer in CANCEL:
                return reply('기억 변경 요청을 취소했어요.'), []
            choices = pending['proposal']['target_ids']
            ordinal = selection(text)
            if ordinal is None or not 0 <= ordinal < len(choices):
                self._ctx._question(
                    conn,
                    token,
                    'target',
                    pending['proposal'],
                    pending['source'],
                )
                return (
                    reply(
                        '변경하거나 삭제할 기억의 번호를 말씀해 주세요.', True
                    ),
                    [],
                )
            proposal = dict(pending['proposal'], target_ids=[choices[ordinal]])
            source = pending['source']
            explicit = True
            selected_target = True
            pending = None
        if pending and answer in YES | NO:
            if (
                self.conversations._select_session_locked(
                    token.user_id, token.conversation_id
                )
                is None
            ):
                raise ValidationError('conversation_changed')
            robot_pending = conn.execute(
                (
                    'SELECT 1 FROM confirmation_intents\n'
                    '                WHERE user_id=? AND '
                    "conversation_id=? AND state='pending' LIMIT 1"
                ),
                (token.user_id, token.conversation_id),
            ).fetchone()
            if robot_pending:
                return (
                    reply(
                        '어느 질문에 대한 답인지 불명확해 둘 다 승인하지 않았어요. '
                        '기억 관리 또는 로봇 실행 중 원하는 요청을 다시 말씀해 주세요.',
                        True,
                    ),
                    [],
                )
            if answer in NO:
                return reply('기억 관리 요청을 반영하지 않았어요.'), []
            source = pending['source']
            proposal = pending['proposal']
            explicit = True
            if pending['kind'] == 'consent':
                self.memory.set_personalization(
                    token.user_id, True, snapshot.source, connection=conn
                )
                if not proposal or not proposal['facts']:
                    return (
                        reply(
                            '개인화를 시작했어요. 직접 말한 일상 정보를 기억하고 대화에 활용할게요.'
                        ),
                        [],
                    )
            elif pending['kind'] == 'conflict':
                proposal = dict(proposal, operation='correct')
                confirmed_correction = True
        if control == 'disable':
            return self._disable_personalization(conn, token.user_id, source), []
        if control == 'enable':
            proposal = {
                'operation': 'enable',
                'facts': [],
                'target_ids': [],
                'query': '',
                'evidence': text,
            }
        if proposal is None:
            followup = _missing_memory_followup(text)
            if explicit or followup is not None:
                _LOGGER.info(
                    'memory_policy reason=proposal_missing response=%s',
                    'followup' if followup is not None else 'conversation',
                )
            return followup, []
        try:
            proposal = validate_memory_proposal(proposal)
        except ValidationError:
            _LOGGER.info('memory_policy reason=invalid_proposal')
            if not explicit and result.decision.type != 'tool_call':
                return None, []
            return (
                _missing_memory_followup(text)
                or reply('어떤 내용을 말씀하시는지 조금 더 알려줄래요?', True),
                [],
            )
        proposal = dict(proposal, facts=[
            fact for fact in proposal['facts'] if not response_setting_fact(fact)
        ])
        operation = proposal['operation']
        if result.decision.type == 'tool_call':
            return (
                reply(
                    '로봇 실행과 기억 관리 중 먼저 처리할 요청을 말씀해 주세요.',
                    True,
                ),
                [],
            )
        if not direct_source(source['text']) or (
            proposal['evidence'] and proposal['evidence'] not in source['text']
        ):
            return (
                reply('직접 요청한 기억 관리 내용인지 확인해 주세요.', True)
                if explicit
                else None
            ), []
        if negated_management(source['text']):
            return reply('기억을 변경하지 않았어요.'), []
        correction = self_name_correction(source['text']) if not pending else None
        pet_edit = _pet_name_correction(source['text']) if not pending else None
        if pet_edit:
            subjects = (pet_edit['pet'], _PET_ALIAS.get(pet_edit['pet']))
            facts = proposal['facts']
            if (not snapshot.state['enabled'] or not result.provider_result.memory_supported
                    or result.decision.type not in {'message', 'clarification'} or operation != 'correct'
                    or len(facts) != 1 or facts[0]['kind'] != 'pet' or facts[0]['attribute'] != 'name'
                    or facts[0]['subject'] not in subjects or facts[0]['value'] != pet_edit['new']
                    or facts[0]['evidence'] not in source['text']
                    or not all(pet_edit[k] in facts[0]['evidence'] for k in ('old', 'new'))):
                return reply('정정할 반려동물 이름을 확인하지 못했어요. 다시 말씀해 주세요.', True), []
        if correction:
            facts = proposal['facts']
            if (not snapshot.state['enabled'] or not result.provider_result.memory_supported
                    or result.decision.type not in {'message', 'clarification'}
                    or operation not in {'remember', 'correct'} or len(facts) != 1
                    or not _correct_name_fact(facts[0], correction, source['text'])):
                return reply('정정할 본명을 확인하지 못했어요. 다시 말씀해 주세요.', True), []
            facts[0]['value'] = correction['new']
            operation = 'correct'
            explicit = True
        if operation in {'forget', 'correct'} and not (
            confirmed_correction
            or requested_operation(source['text'], operation)
        ):
            return (
                reply(
                    '기억을 변경하지 않았어요. 원하는 조회·정정·삭제 요청을 구체적으로 말씀해 주세요.',
                    True,
                ),
                [],
            )
        if operation == 'disable':
            # Only an unambiguous current utterance may revoke consent.
            return (
                reply(
                    '개인화를 중단하려는 요청인지 명확하게 말씀해 주세요.',
                    True,
                ),
                [],
            )
        if operation == 'enable' or (
            operation == 'remember'
            and not self.memory.policy_state(token.user_id, connection=conn)[
                'enabled'
            ]
        ):
            if explicit:
                self._ctx._question(conn, token, 'consent', proposal, source)
                return reply(CONSENT_PROMPT, True), []
            return None, []
        if operation in {'recall', 'forget', 'correct'} and not explicit:
            if operation != 'recall' or not snapshot.state['enabled']:
                return None, []
        if (
            operation == 'remember'
            and not explicit
            and re.search(
                r'[?？]|뭐|누구|어떤|어떻게|일까|인가요',
                source['text'],
            )
        ):
            return None, []
        for fact in proposal['facts']:
            if not (
                correction or pet_edit or fact_matches_source(fact, source['text'])
                or source_review_matches(fact, source)
            ):
                if operation == 'remember' and not explicit:
                    return None, []
                _LOGGER.info('memory_policy reason=source_unverified')
                return (
                    reply(
                        '누구에 대한 어떤 내용인지 한 가지만 더 알려줄래요?',
                        True,
                    ),
                    [],
                )
        records = list(
            self.memory.list_for_user(token.user_id, connection=conn)
        )
        spelling = source.get('pet_name_spelling')
        if spelling and operation == 'remember':
            subjects = (spelling['subject'], _PET_ALIAS.get(spelling['subject']))
            known_pets = [r.metadata.get('fact') or {} for r in records if r.kind == 'pet']
            for fact in proposal['facts']:
                if fact['kind'] != 'pet' or fact['attribute'] != 'name' or fact['subject'] not in subjects:
                    continue
                name = spelling['name_before_copula']
                if spelling['copula'] in {'이야', '이에요'} and any(
                    known.get('subject') in subjects and known.get('attribute') == 'name'
                    and known.get('value') == name + '이'
                    for known in known_pets
                ):
                    name += '이'
                if fact['value'] != name:
                    if not explicit:
                        return None, []
                    return reply(
                        f"이름 표기를 확인할게요. ‘우리 {spelling['subject']} 이름은 ○○입니다’처럼 다시 말씀해 주세요.",
                        True,
                    ), []
        by_id = {record.id: record for record in records}
        targets = proposal['target_ids']
        supplied = {r['id'] for r in snapshot.context['memories']}
        if snapshot.pending:
            supplied.update(snapshot.pending['proposal'].get('target_ids', []))
        if correction:
            own_names = self._ctx._active_identity(token.user_id, 'name', conn)
            original = own_names[0] if len(own_names) == 1 else None
            original_fact = original.metadata.get('fact', {}) if original else {}
            if (original is None or (correction['old'] is not None
                    and (original_fact.get('value') or _self_introduction(original.content)) != correction['old'])
                    or (targets and targets != [original.id])):
                return reply('정정할 기존 본명을 확인하지 못했어요. 다시 말씀해 주세요.', True), []
            # The full command identifies this single slot independently of the
            # model. Preserve its subject spelling so it remains the same slot.
            proposal['facts'][0]['subject'] = original_fact.get('subject', 'user')
            targets = [original.id]
            supplied.add(original.id)
        if pet_edit:
            # Resolve authority from the user's full edit and current slot. The
            # model can mistype a UUID; it cannot select a different record.
            current, _ = self._ctx._pet_names(token.user_id, pet_edit['pet'], conn)
            original = current[0] if len(current) == 1 else None
            original_fact = original.metadata.get('fact', {}) if original else {}
            if (not original or original.id not in supplied or original_fact.get('value') != pet_edit['old']
                    or len(targets) > 1 or (targets and targets[0] != original.id and conn.execute(
                        'SELECT 1 FROM memories WHERE id=?', (targets[0],)).fetchone())):
                return reply('정정할 기존 반려동물 이름을 확인하지 못했어요. 다시 말씀해 주세요.', True), []
            proposal['facts'][0]['subject'] = original_fact['subject']
            targets = [original.id]
        if any(key not in by_id or key not in supplied for key in targets):
            if operation == 'remember' and not explicit:
                return None, []
            return (
                reply(
                    '관리할 기억을 확인할 수 없어요. 대상을 다시 말씀해 주세요.',
                    True,
                ),
                [],
            )
        if (
            operation in {'forget', 'correct'}
            and targets
            and not (selected_target or confirmed_correction or correction or pet_edit)
        ):
            identified = self._identify_targets(records, source['text'])
            if set(targets) != set(identified) or len(identified) != 1:
                targets = []
        if operation == 'recall':
            return self._execute_recall(
                conn, token.user_id, snapshot, proposal, records, by_id, targets, explicit,
            )
        if operation in {'forget', 'correct'} and not targets:
            resolved_targets, prompt = self._disambiguate_targets(
                conn, token, snapshot, text, operation, proposal, source, records, by_id,
            )
            if prompt is not None:
                return prompt, []
            targets = resolved_targets
        if operation == 'forget':
            self._mark_prior_sources(
                conn, token.user_id, [by_id[key] for key in targets]
            )
            outcome = self.memory.remove_facts(
                token.user_id, targets, connection=conn
            )
            return (
                reply(
                    '요청한 기억을 삭제했어요.'
                    if outcome['invalidated_ids']
                    else '삭제할 기억이 없어요.'
                ),
                [],
            )
        if operation == 'correct' and (
            len(targets) != 1 or len(proposal['facts']) != 1
        ):
            return (
                reply(
                    '한 번에 정정할 기억과 새 내용을 하나씩 말씀해 주세요.',
                    True,
                ),
                [],
            )
        if not proposal['facts']:
            if operation == 'remember' and not explicit:
                return None, []
            return reply('기억할 내용을 구체적으로 말씀해 주세요.', True), []
        return self._apply_upsert_facts(
            conn, token, operation, proposal, source, by_id, targets,
            explicit=explicit, automatic_insert=_automatic_insert,
        )

    def _disable_personalization(
        self, conn: sqlite3.Connection, user_id: str, source: dict[str, Any],
    ) -> AgentDecision:
        self.memory.set_personalization(
            user_id, False, source, connection=conn,
        )
        message = (
            '개인화를 중단했어요. 개인 사실 기억의 자동 저장과 활용을 멈추고, '
            '기존 기억은 유지할게요.'
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='story_runtime_policy'"
        ).fetchone():
            story_policy = conn.execute(
                'SELECT enabled FROM story_runtime_policy WHERE user_id=?',
                (user_id,),
            ).fetchone()
            if story_policy and story_policy['enabled']:
                message += (
                    ' 별도로 동의한 이야기 기억 설정은 계속 켜져 있어요. '
                    '이야기 기억도 멈추려면 이야기 기억 설정에서 중단해 주세요.'
                )
        return reply(message)

    def _execute_recall(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        snapshot: MemorySnapshot,
        proposal: dict[str, Any],
        records: list[Any],
        by_id: dict[str, Any],
        targets: list[str],
        explicit: bool,
    ) -> tuple[AgentDecision, list[str]]:
        if not explicit:
            found = [
                item for item in snapshot.memories
                if not targets or item.id in targets
            ]
        else:
            found = [by_id[key] for key in targets] if targets else records
        if explicit and proposal['query'] and not targets:
            found = list(
                self.memory.search(
                    user_id,
                    proposal['query'],
                    limit=10,
                    connection=conn,
                )
            )
        if not found:
            return reply('해당하는 장기기억이 없어요.'), []
        message = '기억하고 있는 내용이에요. ' + '; '.join(
            r.content for r in found[:10]
        )
        if len(found) > 10:
            message += f' (전체 {len(found)}개 중 10개예요. 원하는 내용을 말씀해 주세요.)'
        return reply(message[:3900]), [r.id for r in found[:10]]

    def _disambiguate_targets(
        self,
        conn: sqlite3.Connection,
        token: Any,
        snapshot: MemorySnapshot,
        text: str,
        operation: str,
        proposal: dict[str, Any],
        source: dict[str, Any],
        records: list[Any],
        by_id: dict[str, Any],
    ) -> tuple[list[str], AgentDecision | None]:
        if (
            re.search(
                r'(기억|정보).*(전부|모두|전체)|(전부|모두|전체).*(기억|정보)',
                text,
            )
            and operation == 'forget'
        ):
            return list(by_id), None
        identified = self._identify_targets(records, source['text'])
        matches = [by_id[key] for key in identified]
        if not matches and snapshot.recall_blocked and re.search(r'저장(?:된|한)\s*이름', text):
            matches = [record for record in records if record.kind == 'name']
        if not matches:
            matches = list(
                self.memory.search(
                    token.user_id,
                    source['text'],
                    limit=10,
                    connection=conn,
                )
            )
            if len(matches) == 1 and len(records) > 1:
                matches = records[:10]
        if len(matches) != 1:
            if matches and snapshot.recall_blocked:
                return [], reply('어떤 저장 항목인지 종류를 더 구체적으로 알려 주세요.', True)
            if matches:
                proposal['target_ids'] = [item.id for item in matches]
                self._ctx._question(conn, token, 'target', proposal, source)
                choices = '; '.join(
                    f'{i + 1}. {item.content}'
                    for i, item in enumerate(matches)
                )
                return [], reply(
                    '어떤 기억을 말씀하셨나요? ' + choices[:3000],
                    True,
                )
            return [], reply(
                '대상 기억을 찾지 못했어요. 내용을 다시 말씀해 주세요.',
                True,
            )
        return [matches[0].id], None

    def _apply_upsert_facts(
        self,
        conn: sqlite3.Connection,
        token: Any,
        operation: str,
        proposal: dict[str, Any],
        source: dict[str, Any],
        by_id: dict[str, Any],
        targets: list[str],
        *,
        explicit: bool,
        automatic_insert: bool,
    ) -> tuple[AgentDecision | None, list[str]]:
        # A savepoint prevents partial automatic saves before a conflict.
        conn.execute('SAVEPOINT memory_effects')
        if operation == 'correct':
            self._mark_prior_sources(
                conn, token.user_id, [by_id[key] for key in targets]
            )
        added: list[str] = []
        for fact in proposal['facts']:
            outcome = self.memory.upsert_fact(
                token.user_id,
                fact,
                source,
                correct_ids=targets if operation == 'correct' else (),
                connection=conn,
                _automatic_insert=automatic_insert,
            )
            if outcome['status'] == 'conflict':
                conn.execute('ROLLBACK TO memory_effects')
                conn.execute('RELEASE memory_effects')
                if operation == 'remember' and not explicit:
                    return None, []
                conflicting = [r.id for r in outcome['records']]
                proposal['target_ids'] = conflicting
                self._ctx._question(conn, token, 'conflict', proposal, source)
                return (
                    reply(
                        '기존 기억과 다른 내용이에요. 기존 정보를 지금 말한 내용으로 바꿀까요?',
                        True,
                    ),
                    [],
                )
            added.extend(record.id for record in outcome['records'])
        conn.execute('RELEASE memory_effects')
        if operation == 'correct':
            return reply('요청한 내용으로 기억을 정정했어요.'), added
        if explicit:
            return reply('말씀한 정보를 기억했어요.'), added
        return None, added

    @staticmethod
    def _identify_targets(records: list[Any], text: str) -> list[str]:
        value = compact(text)
        scores: dict[str, int] = {}
        for record in records:
            fact = record.metadata.get('fact', {})
            subject = compact(fact.get('subject', ''))
            item = compact(fact.get('value', ''))
            score = 4 if item and item in value else 0
            if (
                subject
                and subject not in {'user', 'self'}
                and subject in value
            ):
                score += 2
            if fact.get('kind') == 'name' and re.search(
                r'(내|제|사용자).*이름', text
            ):
                score += 2
            if fact.get('kind') == 'nickname' and re.search(
                r'호칭|별명', text
            ):
                score += 2
            if score:
                scores[record.id] = score
        if not scores:
            return [records[0].id] if len(records) == 1 else []
        best = max(scores.values())
        return [key for key, score in scores.items() if score == best]

    def _mark_prior_sources(
        self, conn: sqlite3.Connection, user_id: str, records: list[Any],
    ) -> None:
        """Exclude earlier duplicate mentions and their dependent dialogue."""
        rows = conn.execute(
            '''SELECT * FROM conversation_turns
            WHERE user_id=? AND status='completed' ORDER BY ordinal''',
            (user_id,),
        ).fetchall()
        revision = self.memory.policy_state(user_id, connection=conn)[
            'revision'
        ]
        for record in records:
            fact_value = compact(
                record.metadata.get('fact', {}).get('value', '')
            )
            if not fact_value:
                continue  # Legacy deletion installs a conservative cutoff.
            earliest: dict[tuple[Any, Any, Any], int] = {}
            for row in rows:
                if fact_value in compact(
                    row['user_content']
                    + ' '
                    + (row['assistant_content'] or '')
                ):
                    key = (
                        row['conversation_id'],
                        row['session_instance_id'],
                        row['generation'],
                    )
                    earliest[key] = min(
                        earliest.get(key, row['ordinal']), row['ordinal']
                    )
            for row in rows:
                key = (
                    row['conversation_id'],
                    row['session_instance_id'],
                    row['generation'],
                )
                if key not in earliest or row['ordinal'] < earliest[key]:
                    continue
                stamp = conn.execute(
                    '''SELECT dependencies_json FROM memory_turn_state
                    WHERE user_id=? AND request_id=?''',
                    (user_id, row['request_id']),
                ).fetchone()
                deps = (set(json.loads(stamp[0])) if stamp else set()) | {
                    record.id
                }
                conn.execute(
                    (
                        'INSERT INTO memory_turn_state VALUES (?, ?, ?, '
                        '?, ?, ?, ?, ?, ?)\n'
                        '                    ON CONFLICT(user_id, '
                        'request_id) DO UPDATE SET '
                        'dependencies_json=excluded.dependencies_json'
                    ),
                    (
                        user_id,
                        row['request_id'],
                        *key,
                        row['turn_id'],
                        revision,
                        json.dumps(sorted(deps)),
                        row['created_at'],
                    ),
                )
