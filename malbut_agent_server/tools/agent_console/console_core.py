"""Terminal access to the current Agent, with isolated storage and mock tools."""

import re
import time
import uuid
from dataclasses import replace

from malbut_agent_server.conversation_preferences import response_settings
from malbut_agent_server.factory import build_orchestrator
from malbut_agent_server.gateway import ToolGateway, ToolQuery
from malbut_agent_server.schemas import SpeechAgentRequest, ValidationError
from malbut_agent_server.speech_dialogue import starts_new_conversation
from malbut_agent_server.tools import TOOL_SPECS
from malbut_agent_server.story_extractor import OpenAIStoryExtractor
from malbut_agent_server.story_memory_provider import StoryMemoryProvider
from malbut_agent_server.story_memory_service import StoryMemoryService, StoryServiceError


_DEFAULT_EXTRACTOR = object()
STORY_CONSENT = (
    '장기 이야기 기억을 켤까요? 일상의 경험·감정, 목표·결정·이유를 같은 이야기로 '
    '묶어 자동 저장하고 다음 대화에 관련 기억을 사용합니다. 삭제할 때까지 보관합니다. '
    '요약과 답변을 위해 관련 대화 원문·기존 요약을 OpenAI API로 보냅니다. '
    '켜기 전 대화는 포함하지 않습니다. /stories off로 새 저장·재사용을 멈추고, '
    '/stories delete로 원문과 관련 기억을 지울 수 있습니다. 동의하면 “네”, 아니면 “아니요”.'
)


class ConsoleCore:
    """Keep the terminal on the production dialogue and memory boundaries."""

    def __init__(self, settings, *, story_extractor=_DEFAULT_EXTRACTOR):
        if settings.tool_mode != 'simulation':
            raise ValueError('The terminal console requires simulation tool mode')
        self.settings = settings
        self.runtime = build_orchestrator(settings, http_server=False)
        self.gateway = ToolGateway(self.runtime.capability_registry)
        self.story_memory = None
        self._story_pending = None
        try:
            if story_extractor is _DEFAULT_EXTRACTOR:
                story_extractor = (
                    OpenAIStoryExtractor(
                        api_key=settings.openai_api_key,
                        model=settings.openai_summary_model or settings.openai_model,
                        base_url=settings.openai_base_url,
                        reasoning_effort=settings.openai_summary_reasoning_effort,
                        timeout=max(30, settings.request_timeout_seconds),
                    ) if settings.provider == 'openai' else None
                )
            self._story_available = story_extractor is not None
            self.story_memory = StoryMemoryService(
                self.runtime.conversation_store, self.runtime.memory_store,
                story_extractor,
            )
            self.story_provider = StoryMemoryProvider(
                self.runtime.provider, self.story_memory,
            )
            self.runtime.provider = self.story_provider
            self.runtime.start_background_memory()
            self.conversation_id = self.runtime.conversation_store.resume_or_create(
                settings.user_id,
            ).conversation_id
        except BaseException:
            self.close()
            raise

    def chat(self, text):
        started = time.perf_counter()
        controlled = self._story_control(text)
        if controlled is not None:
            return controlled
        identity = uuid.uuid4().hex
        # Validate before changing the current session, including voice length.
        request = SpeechAgentRequest.from_dict({
            'request_id': 'console-request-' + identity,
            'user_id': self.settings.user_id,
            'conversation_id': self.conversation_id,
            'turn_id': 'console-turn-' + identity,
            'utterance': text,
            'robot_state': {},
            # Match SpeechDialogueWorker without a connected Manager.
            'available_tools': [],
        })
        starts_new = starts_new_conversation(request.utterance)
        if starts_new:
            self.story_memory.flush(self.settings.user_id, timeout=45)
        self.conversation_id = self.runtime.conversation_store.resume_or_create(
            self.settings.user_id, self.conversation_id, start_new=starts_new,
        ).conversation_id
        if request.conversation_id != self.conversation_id:
            request = replace(request, conversation_id=self.conversation_id)
        result = self.runtime.handle(request)
        # The existing serializer runs memory_validator before releasing text.
        metadata = result.to_dict()
        metadata['story_revision'] = self.story_provider.reply_revision(
            self.settings.user_id, request.request_id,
        )
        metadata['story_readset'] = self.story_provider.reply_readset(
            self.settings.user_id, request.request_id,
        )
        if metadata['story_revision'] is not None:
            self._validate_story_revision(metadata['story_revision'], metadata['story_readset'],
                                          request_id=request.request_id)
        self.story_memory.after_turn(self.settings.user_id, request.request_id)
        return {
            'text': result.decision.message,
            'decision_type': result.decision.type,
            'elapsed_s': round(time.perf_counter() - started, 3),
            'conversation_id': self.conversation_id,
            'metadata': metadata,
            'physical_authorized': False,
        }

    def _validate_story_revision(self, revision, readset=(), *, request_id=None):
        try:
            valid = self.story_memory.validate(self.settings.user_id, revision)
            if valid is not False and readset:
                valid = self.story_memory.validate_readset(
                    self.settings.user_id, revision, readset, request_id=request_id,
                )
        except (ValueError, RuntimeError) as error:
            raise ValidationError('memory_changed') from error
        if valid is False:
            raise ValidationError('memory_changed')

    def validate_reply(self, reply):
        """Use the reply's own revision, even after provider receipts are evicted."""
        if reply.get('local_story_control'):
            saved = reply['metadata'].get('story_management_policy')
            if saved is not None:
                self._assert_management_policy(saved)
            return
        revision = reply['metadata'].get('story_revision')
        if revision is not None:
            self._validate_story_revision(revision, reply['metadata'].get('story_readset', ()),
                                          request_id=reply['metadata']['request_id'])
        if not reply.get('local_story_control'):
            self.runtime.personal_memory.assert_fresh(
                self.settings.user_id, reply['metadata']['request_id'],
            )

    def new_conversation(self):
        self._story_pending = None
        self.story_memory.flush(self.settings.user_id, timeout=45)
        self.conversation_id = self.runtime.conversation_store.resume_or_create(
            self.settings.user_id, self.conversation_id, start_new=True,
        ).conversation_id
        return self.conversation_id

    def memories(self):
        store = self.runtime.memory_store
        return {
            'policy': store.policy_state(self.settings.user_id),
            'items': [item.to_dict() for item in store.list_for_user(
                self.settings.user_id,
            )],
            'stories': self.stories(),
            'default_preferences': self.runtime.personal_memory.initial_settings(
                self.settings.user_id,
            ),
        }

    def status(self):
        store = self.runtime.conversation_store
        session = store.get(self.settings.user_id, self.conversation_id)
        with store._lock:
            preferences, _, _ = response_settings(store._connection, session, '')
            turn_count = store._connection.execute(
                '''SELECT COUNT(*) FROM conversation_turns
                   WHERE user_id=? AND conversation_id=? AND generation=?
                   AND status='completed' ''',
                (session.user_id, session.conversation_id, session.generation),
            ).fetchone()[0]
        memory = self.memories()
        return {
            'conversation_id': self.conversation_id,
            'session': session.to_dict(),
            'turn_count': turn_count,
            'memory_count': len(memory['items']),
            'memory_policy': memory['policy'],
            'story_memory': memory['stories'],
            'default_preferences': memory['default_preferences'],
            'session_preferences': preferences,
            'provider': self.settings.provider,
            'database_path': self.settings.database_path,
            'tool_mode': 'simulation',
            'physical_authorized': False,
        }

    def tools(self):
        registry = self.runtime.capability_registry.to_dict()
        for capability in registry['capabilities']:
            name = capability['name']
            capability['description'] = TOOL_SPECS[name].description
            capability['parameters'] = TOOL_SPECS[name].parameters
            capability['console_status'] = (
                'simulation' if capability['executable'] else 'unavailable'
            )
            if name in {'get_weather', 'set_weather_location'}:
                capability['console_note'] = 'Manager 날씨 실행기가 연결되지 않았습니다.'
        registry['physical_authorized'] = False
        return registry

    def query_tool(self, name, arguments):
        query = ToolQuery.from_dict({
            'request_id': 'console-tool-' + uuid.uuid4().hex,
            'user_id': self.settings.user_id,
            'tool_name': name,
            'arguments': arguments,
        })
        result = self.gateway.query(query).to_dict()
        result['physical_authorized'] = False
        if name in {'get_weather', 'set_weather_location'}:
            result['console_note'] = 'Manager 날씨 실행기가 연결되지 않았습니다.'
        return result

    def _assert_management_policy(self, saved):
        current = self.story_memory.policy(self.settings.user_id)
        if any(current.get(key) != saved.get(key)
               for key in ('revision', 'data_revision')):
            raise ValidationError('memory_changed')

    def stories(self):
        policy = self.story_memory.policy(self.settings.user_id)
        items = self.story_memory.list_stories(self.settings.user_id)
        self._assert_management_policy(policy)
        return {'policy': policy, 'items': items,
                'extractor': 'OpenAI' if self.settings.provider == 'openai' else 'offline test / none'}

    def _local_story_reply(self, text, data=None, *, policy=None):
        if policy is None:
            policy = (data.get('policy') if isinstance(data, dict) else None)
        policy = policy or self.story_memory.policy(self.settings.user_id)
        self._assert_management_policy(policy)
        result = {
            'text': text, 'decision_type': 'answer', 'elapsed_s': 0,
            'conversation_id': self.conversation_id,
            'metadata': {
                'request_id': 'story-control-' + uuid.uuid4().hex,
                'story_management_policy': policy,
            },
            'physical_authorized': False, 'local_story_control': True,
        }
        if data is not None:
            result['data'] = data
        return result

    def _story_candidates(self, query):
        normalized = re.sub(r'\s+', '', query).casefold()
        stories = self.story_memory.list_stories(self.settings.user_id)
        exact = [item for item in stories if item['story_id'] == query]
        if exact:
            return exact
        prefix = [item for item in stories if item['story_id'].startswith(query)]
        if prefix:
            return prefix
        words = re.findall(r'[가-힣a-zA-Z0-9]+', query.casefold())
        words = [re.sub(r'(이야기|장기기억|기억|내용|관련|대한|관한|모든|전부)$', '', word)
                 for word in words]
        words = [re.sub(r'(을|를|은|는|이|가|에|의)$', '', word) for word in words]
        words = [word for word in words if len(word) >= 2]
        matched = []
        for item in stories:
            names = [item['title'], *item.get('aliases', ())]
            combined = ' '.join(names).casefold()
            if words and all(word in combined for word in words):
                matched.append(item)
            elif normalized and normalized == re.sub(r'\s+', '', item['title']).casefold():
                matched.append(item)
        return matched

    def _story_labels(self, items):
        return '\n'.join(f"{item['story_id']} · {item['title']}" for item in items)

    def _history_preview(self):
        preview = self.story_memory.history_preview(self.settings.user_id)
        def date(value):
            return time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(value)) if value else '-'
        return {'turn_count': preview['turn_count'], 'from': date(preview['first_at']),
                'through': date(preview['last_at']), 'scope': preview}

    def story_command(self, command):
        """Local management commands are never added to model/source transcripts."""
        parts = command.strip().split(maxsplit=2)
        action = parts[1].casefold() if len(parts) > 1 else 'list'
        target = parts[2].strip() if len(parts) > 2 else ''
        user = self.settings.user_id
        command_policy = self.story_memory.policy(user)
        if action == 'list':
            data = self.stories()
            return self._local_story_reply(
                '장기 이야기 기억 ' + ('켜짐' if data['policy']['enabled'] else '꺼짐')
                + f" · {len(data['items'])}개. /stories on|off|sync|history|sources ID|delete ID",
                data,
            )
        if action in ('on', 'history') and not self._story_available:
            self._story_pending = None
            return self._local_story_reply(
                '현재 mock 모드에는 자동 이야기 요약기가 연결되지 않아 켤 수 없어요. '
                'OpenAI 모드로 다시 실행해 주세요.',
            )
        if action == 'on':
            if self.story_memory.policy(user)['enabled']:
                return self._local_story_reply('이미 켜져 있어요. 과거 대화 포함은 /stories history로 별도 동의하세요.')
            self._story_pending = {'kind': 'enable'}
            return self._local_story_reply(STORY_CONSENT)
        if action == 'off':
            self._story_pending = None
            self.story_memory.disable(user)
            return self._local_story_reply('장기 이야기 기억을 껐어요. 기존 기억은 남겨 두고 새 장기 저장·재사용을 멈췄어요. 현재 대화의 문맥은 유지됩니다.')
        if action == 'history':
            preview = self._history_preview()
            if not preview['turn_count']:
                return self._local_story_reply('포함할 과거 대화가 없어요.')
            self._story_pending = {'kind': 'history', 'preview': preview}
            return self._local_story_reply(
                f"이 사용자의 현재 저장된 대화 {preview['turn_count']}턴 "
                f"({preview['from']} ~ {preview['through']})을 추가로 읽고 장기 이야기 기억을 만들까요? "
                '지금 표시한 범위까지만 포함합니다. '
                '꺼져 있던 기간의 대화도 포함하며, 관련 원문·요약을 OpenAI로 보냅니다. '
                '일상의 경험·감정 등을 같은 이야기로 자동 저장하고 다음 대화에서 사용하며 '
                '삭제할 때까지 보관합니다. /stories off로 저장·재사용을 멈출 수 있어요. '
                '동의하면 “네”, 아니면 “아니요”.', preview,
            )
        if action == 'sync':
            completed = self.story_memory.flush(user, timeout=45, retry_failed=True)
            data = self.stories()
            return self._local_story_reply(
                '이야기 정리가 끝났어요.' if completed else
                '아직 정리가 끝나지 않았어요. 아래 처리 상태를 확인하고 /stories sync로 다시 확인하세요.', data,
            )
        if action in ('sources', 'delete'):
            if not target:
                return self._local_story_reply(f'/stories {action} 뒤에 이야기 ID 또는 제목을 입력하세요.', self.stories())
            candidates = self._story_candidates(target)
            if len(candidates) != 1:
                if not candidates:
                    return self._local_story_reply('해당 이야기를 찾지 못했어요. /stories에서 ID를 확인하세요.')
                self._story_pending = {'kind': action, 'candidates': candidates, 'policy': command_policy}
                return self._local_story_reply('어느 이야기인지 ID를 골라 주세요. 취소하려면 “취소”.\n' + self._story_labels(candidates), policy=command_policy)
            item = candidates[0]
            if action == 'sources':
                return self._local_story_reply('이야기의 원문 근거예요.', self.story_memory.evidence(user, item['story_id']), policy=command_policy)
            try:
                deletion = self.story_memory.forget(user, item['story_id'])
            except StoryServiceError as error:
                self._story_pending = None
                # These are service-authored Korean recovery instructions, not
                # raw provider errors. Off-period consent and retries differ.
                return self._local_story_reply('아직 삭제하지 않았어요. ' + str(error))
            self._story_pending = None
            if not deletion.get('deleted'):
                return self._local_story_reply('선택한 이야기가 이미 없어졌어요. 새로 삭제한 내용은 없어요.')
            # Do not repeat deleted content in a new display/transcript.
            return self._local_story_reply(
                '이 저장소의 해당 원문 부분과 연결된 기억을 삭제했어요. '
                '관련 내용을 재사용한 말벗 답변은 전체가 비워질 수 있어요.',
            )
        return self._local_story_reply('사용법: /stories on|off|sync|history|sources ID|delete ID')

    def _story_control(self, text):
        if not isinstance(text, str):
            return None
        compact = re.sub(r'\s+', '', text).rstrip('.!?。')
        if text.strip().startswith('/stories'):
            return self.story_command(text)
        if re.fullmatch(r'(?:장기기억|장기이야기기억|이야기기억)(?:을)?켜(?:줘|주세요)', compact):
            return self.story_command('/stories on')
        if re.fullmatch(r'(?:장기기억|장기이야기기억|이야기기억)(?:을)?꺼(?:줘|주세요)', compact):
            return self.story_command('/stories off')
        pending = self._story_pending
        if pending is not None:
            if pending.get('policy') is not None:
                try:
                    self._assert_management_policy(pending['policy'])
                except ValidationError:
                    self._story_pending = None
                    return self._local_story_reply('선택 중에 기억이 바뀌었어요. /stories에서 최신 목록을 확인해 주세요.')
            if compact.casefold() in ('아니요', '아니', '취소', '나중에', 'no', 'n'):
                self._story_pending = None
                return self._local_story_reply('취소했어요. 장기 이야기 기억 설정은 그대로예요.')
            if pending['kind'] in ('enable', 'history'):
                if compact.casefold() in ('네', '예', '응', 'ㅇㅇ', '동의', '동의해', '동의합니다', 'yes', 'y'):
                    self._story_pending = None
                    arguments = {'include_history': pending['kind'] == 'history'}
                    if pending['kind'] == 'history':
                        arguments['history_scope'] = pending['preview']['scope']
                    self.story_memory.enable(self.settings.user_id, **arguments)
                    return self._local_story_reply(
                        '장기 이야기 기억을 켰어요.' +
                        (' 동의한 과거 대화도 정리합니다. /stories sync로 진행 상태를 확인하세요.'
                         if pending['kind'] == 'history' else ' 지금부터 나누는 이야기를 자동으로 정리할게요.'),
                        self.story_memory.policy(self.settings.user_id),
                    )
                return self._local_story_reply('장기 이야기 기억 동의에 “네” 또는 “아니요”로 답해 주세요. 이 답은 대화에 저장하지 않아요.')
            selected = [item for item in pending['candidates']
                        if item['story_id'] == text.strip()
                        or item['story_id'].startswith(text.strip())
                        or item['title'] == text.strip()]
            if len(selected) == 1:
                self._story_pending = None
                return self.story_command(f"/stories {pending['kind']} {selected[0]['story_id']}")
            return self._local_story_reply('목록의 이야기 ID를 골라 주세요. 취소하려면 “취소”.\n' + self._story_labels(pending['candidates']), policy=pending['policy'])
        match = re.fullmatch(r'(.+?)(?:을|를)?\s*(?:잊어\s*줘|삭제해\s*줘)[.!?]*', text.strip())
        if match:
            target = match.group(1).strip()
            if self._story_candidates(target) or '이야기' in target or '장기기억' in target:
                return self.story_command('/stories delete ' + target)
        return None

    def close(self):
        try:
            self.gateway.close()
        finally:
            try:
                if self.story_memory is not None:
                    self.story_memory.close()
            finally:
                self.runtime.close()
