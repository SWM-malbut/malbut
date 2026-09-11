"""Consent and provenance policy coordinated with the conversation commit."""

import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, replace

from malbut_agent_server.memory_contract import validate_memory_proposal
from malbut_agent_server.memory_source_review import (
    attach_source_review, source_review_matches,
)
from malbut_agent_server.schemas import AgentDecision, ValidationError
from malbut_agent_server.summarization import (
    ExtractiveConversationSummarizer,
    SummarySourceTurn,
)

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
    'yes',
    'ㅇㅇ',
}
NO = {'아니', '아니요', '싫어', '동의안해', '거절', 'no'}
CANCEL = NO | {'취소', '취소할게', '취소해줘', '그만'}
_LOGGER = logging.getLogger(__name__)


def _missing_memory_followup(text):
    """Ask about a clear command, never infer one from memory vocabulary.

    This only selects user-facing wording when no valid proposal exists;
    retrieval, consent, source validation and mutation authority are unchanged.
    """
    if not direct_source(text) or negated_management(text):
        return None
    # This is a receipt/follow-up check, not a new intent classifier. Bare
    # "기억해" may describe recollection; generic editing is not memory work.
    text = re.sub(r'(기억|저장)\s*해(?:요)?\s*[?？]', '', text)
    ending = r'(?:해\s*(?:줘요?|주세요|둬|두자|줄래|주라)|하자)'
    memory_target = bool(re.search(r'기억|개인화', text))
    commands = (
        (r'삭제\s*' + ending + r'|지워\s*(?:줘요?|주세요)|잊어\s*줘',
         '어떤 내용을 지울지 다시 알려줄래요?', memory_target),
        (r'(?:정정|수정|변경)\s*' + ending + r'|바꿔\s*(?:줘요?|주세요)',
         '어떤 내용을 어떻게 바꿀지 다시 알려줄래요?', memory_target),
        (r'(?:기억|저장)\s*' + ending,
         '기억해 둘 내용을 한 가지만 다시 알려줄래요?', True),
    )
    for pattern, message, eligible in commands:
        if eligible and re.search(
            r'(?:' + pattern + r')(?=\s*(?:$|[.!?。？]))', text,
        ):
            return reply(message, True)
    return None


def compact(text):
    """Normalize comparison text without modifying the stored source."""
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()


def selection(text):
    """Resolve only an exact answer to a numbered memory question."""
    answer = compact(text).rstrip('.!?。')
    ordinal = {
        '첫번째': 0,
        '첫째': 0,
        '두번째': 1,
        '둘째': 1,
        '세번째': 2,
        '셋째': 2,
    }.get(answer)
    if re.fullmatch(r'\d+번?', answer):
        ordinal = int(answer.rstrip('번')) - 1
    return ordinal


def direct_source(text):
    """Conservatively exclude quotation, reported speech and hypotheticals."""
    return not (
        any(
            char in text
            for char in ('"', "'", '`', '“', '”', '‘', '’', '「', '」')
        )
        or re.search(
            r'예시|예를\s*들|만약|가정|라고\s*(했|말|적|써)|친구가|친구는',
            text,
        )
    )


def management_request(text):
    """Identify explicit memory management, not ordinary personal facts."""
    return direct_source(text) and bool(
        re.search(
            r'기억|개인화|잊어|잊으|삭제|정정|수정|바꿔|아니라|정확히는',
            text,
        )
    )


def local_intent(text):
    """Recognize clear consent controls without relying on model claims."""
    if not direct_source(text) or negated_management(text):
        return None
    value = compact(text).rstrip('.!?。')
    if re.fullmatch(
        r'(개인화|자동저장)(를|는|을)?(중단|꺼|끄|그만|멈춰)'
        r'(해줘|해주세요|해|줘|주세요|할래|하자|해요)?',
        value,
    ):
        return 'disable'
    if re.fullmatch(
        r'(개인화|기억)(를|는|을)?(시작|켜|활성화)'
        r'(해줘|해주세요|해|줘|주세요|할래|하자|해요)?',
        value,
    ):
        return 'enable'
    return None


def negated_management(text):
    """Do not turn a restriction or negated request into a mutation."""
    return bool(
        re.search(
            r'(기억|저장|삭제|수정|정정|변경|중단|지우|지워|바꾸|바꿔|잊).{0,8}'
            r'(하지|말아|말고|말라|않|금지)|안\s*(지워|바꿔|기억|저장|삭제)',
            text,
        )
    )


def requested_operation(text, operation):
    """Require an operation-specific current instruction, not a model label."""
    if not direct_source(text) or negated_management(text):
        return False
    patterns = {
        'forget': r'삭제해|삭제하|잊어|잊으|지워|지우자|없애',
        'correct': r'정정해|정정하|수정해|수정하|변경해|바꿔|바꾸자|아니라|정확히는',
        'remember': r'기억해|기억하|기억해\s*둬|저장해|저장하',
    }
    return bool(re.search(patterns[operation], text))


def fact_matches_source(fact, source):
    """Check the declared subject and attribute against one direct phrase."""
    evidence = fact['evidence']
    value = compact(fact['value'])
    raw = compact(evidence)
    subject = compact(fact['subject'])
    attribute = compact(fact['attribute'])
    kind = fact['kind']
    if evidence not in source or not value or value not in raw:
        return False
    self_subject = subject in {'user', 'self', '사용자', '나', '저'}
    self_name = bool(re.search(r'(내|제|나의|저의)이름', raw))
    self_nickname = bool(re.search(
        r'(내|제|나의|저의)(호칭|별명)|(나를|저를).+불러', raw,
    ))
    if kind == 'name':
        return (self_subject and attribute == 'name' and self_name
                and _label_value('이름', value, raw))
    if kind == 'nickname':
        named = _label_value('(호칭|별명)', value, raw) or bool(re.search(
            r'(나를|저를)' + re.escape(value) + r'(이?라고)불러', raw,
        ))
        return (self_subject and attribute == 'nickname'
                and self_nickname and named)
    if kind == 'preference':
        if not self_subject or not re.match(
            r'^(나는|저는|난|전|내가|제가|내취향|제취향|내선호|제선호)', raw,
        ):
            return False
        return preference_matches_source(attribute, value, raw)
    if kind != 'pet' or self_subject:
        return False
    if not re.search(
        r'반려동물|강아지|반려견|고양이|반려묘|토끼|햄스터|앵무새|거북이', raw,
    ):
        return False
    aliases = {
        '강아지': ('강아지', '반려견'), '반려견': ('강아지', '반려견'),
        '고양이': ('고양이', '반려묘'), '반려묘': ('고양이', '반려묘'),
    }
    if not any(name in raw for name in aliases.get(subject, (subject,))):
        return False
    if any(word in raw for word in ('강아지', '반려견')) and any(
        word in raw for word in ('고양이', '반려묘')
    ):
        return False
    labels = {
        'name': '이름', 'species': '(종류|종)',
        'breed': '(품종|견종|묘종)', 'age': '나이',
        'birthday': '(생일|태어난날짜|태어난날)',
        'color': '(털색|색깔|색)',
    }
    if attribute in labels:
        return _label_value(labels[attribute], value, raw)
    return preference_matches_source(attribute, value, raw)


def _label_value(label, value, text):
    correction = r'(?:[가-힣A-Za-z0-9]+(?:가|이)아니라)?'
    matched = re.search(
        label + r'(?:은|는|이|가|을|를|:)?' + correction
        + re.escape(value), text,
    )
    # A value mentioned before "아니야/아니라" is not an affirmed fact.
    return matched is not None and not re.match(
        r'(?:이|가)?아니', text[matched.end():],
    )


def preference_matches_source(attribute, value, evidence):
    """Preserve preference polarity and reject ambiguous combined clauses."""
    if attribute not in {'likes', 'dislikes', 'preference'}:
        return False
    text = evidence.rstrip('.!?。')
    if re.search(
        r'그리고|하지만|반면|그런데|아니|[,.!?;\n]|안싫|싫어하지않|할지도|일수',
        text,
    ):
        return False
    negative = bool(re.search(r'싫어|안좋아|안선호|좋아하지않|선호하지않', text))
    positive = bool(re.search(r'좋아|선호', text)) and not negative
    # Multiple predicates can associate the right sentiment with a wrong noun.
    if len(re.findall(r'좋아|싫어|선호', text)) != 1:
        return False
    if attribute == 'likes':
        return positive and _preference_object(value, text)
    if attribute == 'dislikes':
        return negative and _preference_object(value, text)
    value_negative = bool(re.search(
        r'싫어|안좋아|안선호|좋아하지않|선호하지않', value,
    ))
    value_positive = bool(re.search(r'좋아|선호', value)) and not value_negative
    return (positive and value_positive) or (negative and value_negative)


def _preference_object(value, text):
    return bool(re.search(
        re.escape(value) + r'(?:을|를|은|는|이|가)?'
        r'(?:정말|아주|많이|너무|별로|안)?(?:좋아|싫어|선호)', text,
    ))


def reply(message, clarification=False):
    """Build a server-owned non-actuating outcome."""
    return AgentDecision(
        type='clarification' if clarification else 'message',
        message=message,
        reason='personal_memory',
        confidence=1.0,
    )


@dataclass
class MemorySnapshot:
    """One user-scoped input snapshot held across inference."""

    state: dict
    memories: list
    history: list
    summary: object
    dependencies: set
    context: dict
    pending: dict | None
    source: dict


class PersonalMemory:
    """Validate proposals and record their effects on one SQLite connection."""

    def __init__(self, memory_store, conversation_store):
        """Use the existing DB file and keep independent public interfaces."""
        from malbut_agent_server.memory import initialize_memory_schema

        self.memory = memory_store
        self.conversations = conversation_store
        if (
            memory_store.database_path
            == conversation_store.database_path
            == ':memory:'
        ):
            memory_store.bind_connection(
                conversation_store._connection,
                conversation_store._lock,
            )
        elif memory_store.database_path != conversation_store.database_path:
            raise ValueError(
                'conversation and memory must use the same SQLite database'
            )
        with conversation_store._lock:
            conn = conversation_store._connection
            initialize_memory_schema(conn)
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

    def snapshot(self, request, token, history, summary, memory_limit=5):
        """Read eligible history and memories under one read transaction."""
        source = {
            'user_id': request.user_id,
            'conversation_id': token.conversation_id,
            'session_instance_id': token.session_instance_id,
            'generation': token.generation,
            'turn_id': token.turn_id,
            'request_id': token.request_id,
            'text': request.utterance,
        }
        with self.conversations._lock:
            conn = self.conversations._connection
            conn.execute('BEGIN')
            try:
                state = self.memory.policy_state(
                    request.user_id, connection=conn
                )
                memories = (
                    list(
                        self.memory.search(
                            request.user_id,
                            request.utterance,
                            limit=memory_limit,
                            connection=conn,
                        )
                    )
                    if state['enabled']
                    else []
                )
                candidates = memories
                if management_request(request.utterance):
                    candidates = list(
                        self.memory.search(
                            request.user_id,
                            request.utterance,
                            limit=10,
                            connection=conn,
                        )
                    )
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
                pending = dict(pending_row) if pending_row else None
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
                filtered, clean_summary, deps = self._context(
                    request.user_id,
                    token,
                    state,
                    list(history),
                    summary,
                    conn,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        deps.update(record.id for record in memories)
        return MemorySnapshot(
            state,
            memories,
            filtered,
            clean_summary,
            deps,
            {
                'enabled': state['enabled'],
                'pending_question': (
                    {'kind': pending['kind'], 'proposal': pending['proposal']}
                    if pending
                    else None
                ),
                'memories': [
                    {
                        'id': record.id,
                        'kind': record.kind,
                        'content': record.content,
                        'fact': {
                            key: value
                            for key, value in record.metadata.get(
                                'fact', {}
                            ).items()
                            if key != 'evidence'
                        },
                    }
                    for record in candidates
                ],
                'allowed_kinds': ['name', 'nickname', 'pet', 'preference'],
            },
            pending,
            source,
        )

    def prepare_source_review(self, request, snapshot, result, reviewer):
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
            return
        explicit_save = requested_operation(request.utterance, 'remember')
        explicit_correct = requested_operation(request.utterance, 'correct')
        if not snapshot.state['enabled'] and not explicit_save:
            return
        if proposal['operation'] == 'correct' and not explicit_correct:
            return
        if not explicit_save and not explicit_correct and re.search(
            r'[?？]|뭐|누구|어떤|어떻게|일까|인가요', request.utterance,
        ):
            return
        if (
            proposal['evidence']
            and proposal['evidence'] not in request.utterance
        ):
            return
        attributes = {
            'name': {'name'}, 'nickname': {'nickname'},
            'preference': {'likes', 'dislikes', 'preference'},
            'pet': {'name', 'species', 'breed', 'age', 'birthday', 'color',
                    'likes', 'dislikes', 'preference'},
        }
        unresolved = []
        for fact in proposal['facts']:
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
                return
            if not fact_matches_source(fact, request.utterance):
                unresolved.append(fact)
        if not unresolved:
            return
        outcome = reviewer.review(request, unresolved)
        attach_source_review(snapshot.source, outcome.facts)
        provider = result.provider_result
        provider.latency_ms += outcome.elapsed_ms
        # Account for review tokens without changing the public metadata shape.
        # Unknown usage must stay unknown, rather than under-reporting a call.
        usage = {}
        for key in ('input_tokens', 'output_tokens', 'total_tokens'):
            old = getattr(provider.usage, key)
            extra = (getattr(outcome.response.usage, key)
                     if outcome.response is not None else None)
            usage[key] = (old + extra
                          if old is not None and extra is not None else None)
        provider.usage = replace(provider.usage, **usage)
        return outcome

    def _context(self, user_id, token, state, history, summary, conn):
        invalid = self.memory.invalidated_ids(user_id, connection=conn)
        rows = conn.execute(
            '''SELECT t.* FROM conversation_turns t
            WHERE user_id=? AND conversation_id=? AND session_instance_id=?
            AND generation=? AND status='completed' AND ordinal<?
            ORDER BY ordinal''',
            (
                user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                token.ordinal,
            ),
        ).fetchall()
        deps = set()
        redacted = False
        eligible = []
        legacy_dependencies = set()
        for row in rows:
            item = self.conversations._turn_from_row(row)
            stamp = conn.execute(
                '''SELECT dependencies_json FROM memory_turn_state
                WHERE user_id=? AND request_id=?''',
                (user_id, item.request_id),
            ).fetchone()
            if stamp:
                item_deps = set(json.loads(stamp[0]))
            else:
                legacy_dependencies.update(
                    self._legacy_dependencies(item.response)
                )
                item_deps = set(legacy_dependencies)
            blocked = (
                item.created_at <= state['legacy_cutoff']
                or bool(item_deps & invalid)
                or (not state['enabled'] and bool(item_deps))
            )
            if blocked:
                redacted = True
                item = replace(
                    item, user_content='', assistant_content='', response={}
                )
            else:
                deps.update(item_deps)
            eligible.append(item)
        if not redacted:
            return history, summary, deps
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

    def local_decision(self, request, snapshot):
        """Keep clear controls and replies to consent questions model-free."""
        if local_intent(request.utterance) is not None:
            return reply('기억 설정 요청을 확인했어요.')
        answer = compact(request.utterance).rstrip('.!?。')
        if snapshot.pending and answer in YES | NO:
            return reply('기억 관리 질문에 대한 답을 확인했어요.')
        if snapshot.pending and snapshot.pending['kind'] == 'target':
            return reply('관리할 기억의 선택을 확인했어요.')
        return None

    def pending_question(self, user_id, conversation_id, connection=None):
        """Return a question still bound to the user's live conversation."""
        if connection is not None:
            return self._pending_question(connection, user_id, conversation_id)
        with self.conversations._lock:
            conn = self.conversations._connection
            conn.execute('BEGIN')
            try:
                pending = self._pending_question(
                    conn, user_id, conversation_id
                )
                conn.commit()
                return pending
            except Exception:
                conn.rollback()
                raise

    def _pending_question(self, conn, user_id, conversation_id):
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

    def assert_fresh(self, user_id, request_id):
        """Reject stale cached or queued answers without executing again."""
        with self.conversations._lock:
            conn = self.conversations._connection
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
    def _legacy_dependencies(response):
        """Read pre-migration personalized provenance without replaying it."""
        public = response.get('public', response)
        memory = public.get('memory', {})
        return {key for key in memory.get('ids', []) if isinstance(key, str)}

    def _question(self, conn, token, kind, proposal, source):
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

    def commit(self, request, token, snapshot, result, conn):
        """Apply memory and final answer within the caller's transaction."""
        if (
            self.memory.policy_state(request.user_id, connection=conn)
            != snapshot.state
        ):
            raise ValidationError('memory_changed')
        # Another runtime may have attached a delayed fact to an earlier turn
        # while this turn was in inference. Its text was already part of this
        # conversation, so merge newly attached lineage without changing the
        # frozen model inputs or reply. Destructive writes still fail the
        # epoch check above, within this same transaction.
        _, _, inherited_dependencies = self._context(
            request.user_id, token, snapshot.state, snapshot.history,
            snapshot.summary, conn,
        )
        conn.execute(
            (
                'DELETE FROM memory_questions WHERE user_id=? '
                'AND conversation_id=?'
            ),
            (request.user_id, request.conversation_id),
        )
        decision, added = self._apply(request, token, snapshot, result, conn)
        claim = (
            r'(기억|저장|삭제|정정|수정|개인화).{0,16}'
            r'(했|하였|완료|해\s*뒀|해\s*두었|됐|할게|해\s*둘게|하겠|해둘께)'
        )
        if decision is None and re.search(claim, result.decision.message):
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
        for record in self.memory.list_for_user(
            request.user_id, connection=conn
        ):
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

    def _apply(self, request, token, snapshot, result, conn,
               *, _automatic_insert=False):
        text = request.utterance
        pending = snapshot.pending
        answer = compact(text).rstrip('.!?。')
        explicit = management_request(text) or requested_operation(
            text, 'remember'
        )
        source = snapshot.source
        proposal = result.provider_result.memory_proposal
        control = local_intent(text)
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
                self._question(
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
            self.memory.set_personalization(
                token.user_id, False, source, connection=conn
            )
            return (
                reply(
                    '개인화를 중단했어요. 자동 저장과 활용을 멈추고, 기존 기억은 유지할게요.'
                ),
                [],
            )
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
                self._question(conn, token, 'consent', proposal, source)
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
                fact_matches_source(fact, source['text'])
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
        by_id = {record.id: record for record in records}
        targets = proposal['target_ids']
        supplied = {r['id'] for r in snapshot.context['memories']}
        if snapshot.pending:
            supplied.update(snapshot.pending['proposal'].get('target_ids', []))
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
            and not (selected_target or confirmed_correction)
        ):
            identified = self._identify_targets(records, source['text'])
            if set(targets) != set(identified) or len(identified) != 1:
                targets = []
        if operation == 'recall':
            if not explicit:
                found = [item for item in snapshot.memories
                         if not targets or item.id in targets]
            else:
                found = [by_id[key] for key in targets] if targets else records
            if explicit and proposal['query'] and not targets:
                found = list(
                    self.memory.search(
                        token.user_id,
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
        if operation in {'forget', 'correct'} and not targets:
            if (
                re.search(
                    r'(기억|정보).*(전부|모두|전체)|(전부|모두|전체).*(기억|정보)',
                    text,
                )
                and operation == 'forget'
            ):
                targets = list(by_id)
            else:
                identified = self._identify_targets(records, source['text'])
                matches = [by_id[key] for key in identified]
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
                    if matches:
                        proposal['target_ids'] = [item.id for item in matches]
                        self._question(conn, token, 'target', proposal, source)
                        choices = '; '.join(
                            f'{i+1}. {item.content}'
                            for i, item in enumerate(matches)
                        )
                        return (
                            reply(
                                '어떤 기억을 말씀하셨나요? ' + choices[:3000],
                                True,
                            ),
                            [],
                        )
                    return (
                        reply(
                            '대상 기억을 찾지 못했어요. 내용을 다시 말씀해 주세요.',
                            True,
                        ),
                        [],
                    )
                targets = [matches[0].id]
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
        # A savepoint prevents partial automatic saves before a conflict.
        conn.execute('SAVEPOINT memory_effects')
        if operation == 'correct':
            self._mark_prior_sources(
                conn, token.user_id, [by_id[key] for key in targets]
            )
        added = []
        for fact in proposal['facts']:
            outcome = self.memory.upsert_fact(
                token.user_id,
                fact,
                source,
                correct_ids=targets if operation == 'correct' else (),
                connection=conn,
                _automatic_insert=_automatic_insert,
            )
            if outcome['status'] == 'conflict':
                conn.execute('ROLLBACK TO memory_effects')
                conn.execute('RELEASE memory_effects')
                if operation == 'remember' and not explicit:
                    return None, []
                conflicting = [r.id for r in outcome['records']]
                proposal['target_ids'] = conflicting
                self._question(conn, token, 'conflict', proposal, source)
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
    def _identify_targets(records, text):
        value = compact(text)
        scores = {}
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

    def _mark_prior_sources(self, conn, user_id, records):
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
            earliest = {}
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
