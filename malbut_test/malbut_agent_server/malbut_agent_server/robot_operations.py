"""Fixed voice workflows with durable dispatch receipts and observed outcomes.

Inference chooses one semantic operation. Only this ROS-owned runner chooses
preparatory steps. A restarted runner marks unfinished sends unknown; it never
replays an operation whose delivery may have happened.
"""

from collections import deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock, get_ident
import time
from uuid import NAMESPACE_URL, uuid5

from malbut_agent_server.tools import (
    SPEECH_DELEGATED_TOOLS, validate_tool_arguments,
)


_QUERIES = {
    'get_robot_status': 'status', 'get_robot_observations': 'status',
    'list_saved_maps': 'map_list', 'get_map_zones': 'zones_get',
    'get_homecam_status': 'homecam_status', 'get_homecam_events': 'homecam_events',
    'get_homecam_recordings': 'homecam_recordings', 'get_homecam_falls': 'homecam_falls',
}
_MISSIONS = {
    'request_navigation': 'navigate_to_pose', 'request_follow_person': 'follow_person',
    'request_patrol': 'patrol', 'request_mapping': 'autoslam',
    'request_relocalization': 'relocalize', 'request_manual_control': 'manual_drive',
    'request_recovery': 'recovery',
}
_SELECTED = {'request_navigation', 'request_follow_person', 'request_patrol',
             'request_relocalization', 'wake_robot', 'select_saved_map'}
_POSE_REQUIRED = _SELECTED - {'request_relocalization'}
_FINAL = {'succeeded', 'failed', 'canceled', 'unknown'}
_YES = {'네', '예', '응', '그래', '좋아', '진행해', '진행해줘', '확인', '동의해', '해줘'}
_NO = {'아니', '아니요', '안돼', '하지마', '취소', '취소해'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _id(parent, step):
    return 'agent-operation:' + hashlib.sha256((parent + ':' + str(step)).encode()).hexdigest()


def _map_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9가-힣][A-Za-z0-9가-힣_-]{0,63}\.ya?ml', value):
        raise ValueError('저장 지도 목록에 있는 파일 이름이 필요해요.')
    return value


def _maps(value):
    items = value.get('maps', []) if isinstance(value, dict) else []
    result = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, (str, dict)):
            continue
        name = item if isinstance(item, str) else item.get('id', item.get('filename', item.get('name', '')))
        try:
            result[_map_name(name)] = item
        except ValueError:
            continue
    return result


def _active_ids(status):
    system = status.get('system') or {}
    ids = set()
    for field in ('active_foreground_missions', 'active_background_missions',
                  'pending_missions', 'suspended_missions'):
        items = system.get(field, []) if isinstance(system, dict) else []
        if isinstance(items, dict):
            items = list(items.values())
        for item in items if isinstance(items, list) else []:
            value = item if isinstance(item, str) else item.get('mission_id', '')
            if isinstance(value, str) and value:
                ids.add(value)
    return sorted(ids)


@dataclass(frozen=True)
class OperationProposal:
    """Bind the current committed turn to detached server-owned arguments."""

    request_id: str
    tool_name: str
    arguments_json: str
    context_json: str
    message: str = '요청을 확인하고 있어요.'


class WorkflowJournal:
    """Record every external attempt before dispatch in the existing local DB."""

    def __init__(self, path):
        if path != ':memory:':
            path = str(Path(path).expanduser())
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS agent_robot_workflows (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_robot_steps (
                request_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
                operation TEXT NOT NULL, arguments TEXT NOT NULL,
                state TEXT NOT NULL, result TEXT, updated_at REAL NOT NULL, goal_id TEXT);
        ''')
        # Previous process ownership cannot establish whether an external send ran.
        for row in self.db.execute("SELECT * FROM agent_robot_workflows WHERE state NOT IN ('succeeded','failed','canceled','unknown')").fetchall():
            job = json.loads(row['payload'])
            job.update(state='unknown', message='이전 실행의 결과를 확인할 수 없어 자동으로 다시 요청하지 않았어요.')
            self.save(job)
        self.db.execute("UPDATE agent_robot_steps SET state='unknown' WHERE state='sent'")
        self.db.commit()

    def create(self, proposal, now):
        raw = _json([proposal.tool_name, proposal.arguments_json, proposal.context_json])
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            row = self.db.execute('SELECT * FROM agent_robot_workflows WHERE request_id=?', (proposal.request_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('같은 발화 ID의 요청 내용이 달라 실행하지 않았어요.')
                return json.loads(row['payload']), False
            job = dict(request_id=proposal.request_id, fingerprint=fingerprint,
                       tool=proposal.tool_name, arguments=json.loads(proposal.arguments_json),
                       context=json.loads(proposal.context_json), state='queued', phase='queued',
                       step=0, created_at=now, deadline=now + 600, confirmed_ids=[],
                       message='실행 요청을 준비하고 있어요.', result={})
            self.save(job)
            return job, True

    def save(self, job):
        with self.lock:
            self.db.execute('''INSERT INTO agent_robot_workflows VALUES (?,?,?,?,?)
                ON CONFLICT(request_id) DO UPDATE SET payload=excluded.payload,
                state=excluded.state,updated_at=excluded.updated_at''',
                (job['request_id'], job['fingerprint'], _json(job), job['state'], time.time()))
            self.db.commit()

    def step(self, job, operation, arguments, goal_id=None):
        job['step'] += 1
        request_id = _id(job['request_id'], job['step'])
        with self.lock:
            self.db.execute('INSERT INTO agent_robot_steps VALUES (?,?,?,?,?,?,?,?)',
                            (request_id, job['request_id'], operation, _json(arguments),
                             'sent', None, time.time(), goal_id))
            self.save(job)
        return request_id

    def observed(self, request_id, result):
        with self.lock:
            self.db.execute('UPDATE agent_robot_steps SET state=?,result=?,updated_at=? WHERE request_id=?',
                            ('observed', _json(result), time.time(), request_id))
            self.db.commit()

    def recent(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                'SELECT payload FROM agent_robot_workflows ORDER BY updated_at DESC LIMIT 20')]

    def close(self):
        with self.lock:
            self.db.close()


class RobotOperations:
    """Advance bounded fixed preparations without blocking the dialogue worker."""

    def __init__(self, manager, device, journal, *, navigation_targets=None,
                 notify=lambda event: None, clock=time.monotonic):
        self.manager, self.device, self.journal = manager, device, journal
        self.targets, self.notify, self.clock = navigation_targets, notify, clock
        self.owner = get_ident()
        self.jobs, self.waiting = {}, {}
        self.events = deque()
        self.localization = {}
        self.recent_results = {}
        self.closed = False

    def observe_localization(self, payload):
        try:
            state = json.loads(payload) if isinstance(payload, str) else payload
            self.localization = state if isinstance(state, dict) else {}
        except (ValueError, TypeError):
            self.localization = {}

    def observe_device_state(self, payload):
        """Reconcile old sends using retained Manager results, never resume steps."""
        try:
            if isinstance(payload, str):
                if len(payload) > 262144:
                    return
                payload = json.loads(payload)
            records = payload.get('recent_results', [])
            if not isinstance(records, list):
                return
        except (ValueError, TypeError, AttributeError):
            return
        self.recent_results = {str(record.get('mission_id', '')).replace('-', ''): record
                               for record in records if isinstance(record, dict)}
        for job in self.journal.recent():
            if job['state'] != 'unknown' or not job.get('goal_id'):
                continue
            match = next((record for record in records if isinstance(record, dict)
                          and str(record.get('mission_id', '')).replace('-', '') == job['goal_id']
                          and record.get('capability_id') == job.get(
                              'goal_capability', _MISSIONS.get(job['tool']))), None)
            if match is None or match.get('state') not in {'SUCCEEDED', 'CANCELED', 'ABORTED'}:
                continue
            if job.get('result') == match:
                continue
            if match.get('downstream_terminal') is not True:
                job['result'] = dict(match)
                job['message'] = 'Manager 요청은 종료됐지만 하위 동작의 종료는 확인되지 않았어요. 자동으로 다시 실행하지 않을게요.'
                self.journal.save(job)
                continue
            if job.get('phase') == 'pose_preparation':
                job['result'] = dict(match)
                job['message'] = '이전 위치 준비 작업의 종료만 확인했어요. 원래 요청한 동작은 자동으로 재개하지 않았어요.'
                self.journal.save(job)
                continue
            state = {'SUCCEEDED': 'succeeded', 'CANCELED': 'canceled', 'ABORTED': 'failed'}[match['state']]
            self.jobs[job['request_id']] = job
            self._finish(job, state, '이전 요청의 Manager 종료 결과를 다시 확인했어요. ' +
                         str(match.get('message', ''))[:500], dict(match))

    def prepare_request(self, request, result):
        arguments = validate_tool_arguments(result.decision.tool_name, result.decision.arguments)
        if result.decision.tool_name not in SPEECH_DELEGATED_TOOLS:
            raise ValueError('unsupported robot operation')
        if 'map' in arguments and arguments['map'] is not None:
            _map_name(arguments['map'])
        if result.decision.tool_name == 'request_mapping' and not re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', arguments['map_name']):
            raise ValueError('새 지도 이름은 경로와 확장자 없이 지정해 주세요.')
        context = dict(user_id=request.user_id, conversation_id=request.conversation_id,
                       generation=result.conversation_generation, utterance=request.utterance)
        return OperationProposal(request.request_id, result.decision.tool_name,
                                 _json(arguments), _json(context))

    def dispatch(self, proposal, *, guard=None):
        if get_ident() != self.owner:
            raise RuntimeError('robot operations belong to the ROS owner')
        if self.closed:
            return '로봇 연결을 종료하고 있어 요청하지 않았어요.'
        refusal = guard() if guard else None
        if refusal:
            return refusal
        job, fresh = self.journal.create(proposal, self.clock())
        if not fresh:
            return job['message']
        self.jobs[job['request_id']] = job
        if job['tool'] == 'confirm_pending_operation':
            return self._confirm(job)
        if job['tool'] not in {*_QUERIES, 'stop_robot_movement', 'cancel_voice_mission'}:
            preparing = [other for other in self.jobs.values() if other is not job
                         and other['state'] not in _FINAL and other['tool'] not in _QUERIES
                         and other['phase'] != 'manager']
            if preparing:
                self._finish(job, 'failed', '앞선 실행 준비나 확인이 남아 있어요. 먼저 확인에 답하거나 중지해 주세요.')
                return job['message']
        if job['tool'] in {'stop_robot_movement', 'cancel_voice_mission'}:
            job['stop_owned_pending'] = []
            job['stop_device_pending'] = []
            for other in list(self.jobs.values()):
                if (other is not job and other['phase'] in {'manager', 'pose_preparation'}
                        and other['state'] not in {'succeeded', 'failed', 'canceled'}):
                    pending_id = other['pending_id']
                    job['stop_owned_pending'].append(pending_id)
                    self.manager.cancel(pending_id)
                if (other is not job and other['state'] not in _FINAL
                        and other['tool'] not in _QUERIES and other['phase'] != 'manager'):
                    if other.get('pending_operation') in {'runtime_start', 'map_select'}:
                        job['stop_device_pending'].append(other['pending_id'])
                    self.device.cancel(other.get('pending_id', ''))
                    self._finish(other, 'canceled', '중지 요청으로 남은 실행 단계를 취소했어요.')
            self._send(job, 'stop', {}, 'stopped')
        elif job['tool'] in _QUERIES:
            args = dict(job['arguments'])
            if 'event_type' in args:
                value = args.pop('event_type')
                if value is not None:
                    args['eventType'] = value
            self._send(job, _QUERIES[job['tool']], args, 'query')
        elif job['tool'] == 'delete_saved_map':
            self._send(job, 'map_list', {}, 'delete_check')
        elif job['tool'] == 'update_map_zone':
            self._send(job, 'zones_get', {}, 'zone_check')
        elif job['tool'] == 'update_homecam_settings':
            args = {key: value for key, value in job['arguments'].items() if value is not None}
            if args:
                self._send(job, 'homecam_settings', args, 'query')
            else:
                self._finish(job, 'failed', '변경할 Homecam 설정을 말씀해 주세요.')
        else:
            self._send(job, 'status', {}, 'prepare')
        self._publish(job, 'accepted')
        return job['message']

    def _send(self, job, operation, arguments, phase):
        if not self._fresh(job):
            return
        if operation in {'runtime_start', 'map_select'} and job.get('movement_binding'):
            arguments = dict(arguments, **job['movement_binding'])
        job.update(state='running', phase=phase, pending_operation=operation)
        request_id = self.journal.step(job, operation, arguments)
        job['pending_id'] = request_id
        self.journal.save(job)
        self.waiting[request_id] = job['request_id']
        callback = lambda result: self.events.append((request_id, result))
        try:
            if operation == 'stop':
                self.device.stop(request_id, callback, confirmed_ids=arguments.get('confirmed_ids'))
            else:
                self.device.send(request_id, operation, arguments, callback)
        except Exception:
            callback(dict(success=False, code='unknown', message='실행 요청의 전달 여부를 확인하지 못했어요.', result={}))

    def tick(self):
        if self.closed:
            return
        while self.events:
            request_id, event = self.events.popleft()
            parent = self.waiting.pop(request_id, None)
            if parent is None:
                continue
            self.journal.observed(request_id, event)
            job = self.jobs.get(parent)
            if job is None or job['state'] in _FINAL:
                continue
            self._advance(job, event)
        now = self.clock()
        for job in list(self.jobs.values()):
            if job['state'] not in _FINAL and job.get('phase') == 'stopping_owned':
                self._complete_owned_stop(job)
            if job['state'] in _FINAL or job.get('phase') == 'manager':
                continue
            if now >= job.get('confirmation_deadline', job['deadline']):
                self.device.cancel(job.get('pending_id', ''))
                self._finish(job, 'unknown', '요청의 확인 시간이 지나 결과를 확정하지 못했어요. 자동 재실행하지 않을게요.')

    def _advance(self, job, event):
        value = event.get('result') or {}
        if not isinstance(value, dict):
            value = {'items': value}
        if not event.get('success'):
            if event.get('code') == 'preemption_confirmation_required':
                ids = value.get('conflicting_mission_ids', [])
                if ids and all(isinstance(item, str) for item in ids):
                    job['conflicts'] = sorted(set(ids))
                    self._ask_conflicts(job)
                    return
            state = ('unknown' if event.get('code') in {'unknown', 'timeout', 'result_unknown'}
                     else 'canceled' if event.get('code') == 'canceled' else 'failed')
            self._finish(job, state, event.get('message') or '요청을 완료하지 못했어요.', value)
            return
        phase = job['phase']
        if phase == 'stopped' and (job.get('stop_owned_pending') or job.get('stop_device_pending')):
            job.update(phase='stopping_owned', stop_result=value,
                       deadline=min(job['deadline'], self.clock() + 30))
            self.journal.save(job)
            self._complete_owned_stop(job)
            return
        if phase in {'query', 'manager', 'stopped', 'standby'}:
            if job['tool'] == 'get_robot_status':
                value = dict(value, recent_voice_operations=[
                    {key: previous.get(key) for key in ('tool', 'state', 'message', 'goal_id')}
                    for previous in self.journal.recent() if previous['request_id'] != job['request_id']][:5])
            self._finish(job, 'succeeded', self._describe(job, value, event.get('message')), value)
        elif phase == 'prepare':
            self._prepare(job, value)
        elif phase in {'prepared', 'pose_preparation'}:
            if phase == 'prepared':
                prepared = value.get('preparation_movement_binding') or {}
                if job.get('starting_manager'):
                    if (not isinstance(prepared.get('runtime_id'), str)
                            or not prepared['runtime_id'] or type(prepared.get('epoch')) is not int):
                        self._finish(job, 'unknown', '시작한 로봇의 이동 중지 세대를 확인하지 못해 다음 작업을 보내지 않았어요.')
                        return
                    job['movement_binding'] = dict(movement_runtime_id=prepared['runtime_id'],
                                                   movement_epoch=prepared['epoch'])
                    job.pop('starting_manager', None)
            # Re-read the authoritative state; accepting a transition is not pose readiness.
            self._send(job, 'status', {}, 'ready_check')
        elif phase == 'ready_check':
            runtime = value.get('runtime') or {}
            localization = runtime.get('localization') or {}
            if not runtime.get('ready') or (job['tool'] in _POSE_REQUIRED and not localization.get('pose_ready')):
                self._finish(job, 'failed', '실행 준비 또는 지도 위치 확인이 완료되지 않아 동작을 시작하지 않았어요.', value)
            else:
                self._execute(job, value)
        elif phase == 'preempted':
            self._send(job, 'status', {}, 'prepare')
        elif phase == 'delete_check':
            target = _maps(value).get(job['arguments']['map'])
            if not isinstance(target, dict) or not target.get('revision'):
                self._finish(job, 'failed', '삭제할 지도를 목록에서 확인하지 못했어요.')
            elif not job.get('delete_confirmed'):
                job['bound_map'] = target
                self._ask(job, f"{job['arguments']['map']} 지도를 삭제할까요? 이 작업은 지도를 지웁니다.")
            elif target != job.get('bound_map'):
                self._finish(job, 'failed', '확인하는 동안 지도 정보가 바뀌어 삭제하지 않았어요.')
            else:
                self._send(job, 'map_delete', {'map': job['arguments']['map'],
                           'confirmed': True, 'revision': target['revision']}, 'query')
        elif phase == 'zone_check':
            arguments = {key: item for key, item in job['arguments'].items() if item is not None}
            if not value.get('revision') or not set(arguments) & {'name', 'behavior'}:
                self._finish(job, 'failed', '구역의 현재 설정이나 변경할 항목을 확인하지 못했어요.')
            else:
                arguments['revision'] = value['revision']
                self._send(job, 'zones_update', arguments, 'query')

    def _prepare(self, job, status):
        runtime = status.get('runtime') or {}
        localization = runtime.get('localization') or {}
        job['localization_binding'] = {key: localization.get(key) for key in ('runtime_id', 'transition_id')}
        if not self._bind_movement(job, status):
            return
        tool = job['tool']
        ids = _active_ids(status)
        needs_transition = (tool in {'standby_robot', 'select_saved_map', 'request_mapping'}
                            or not runtime.get('ready')
                            or tool in _POSE_REQUIRED and not localization.get('pose_ready'))
        if needs_transition and set(ids) - set(job['confirmed_ids']):
            job['conflicts'] = ids
            job['conflict_status'] = status.get('system', {})
            self._ask_conflicts(job)
            return
        if needs_transition and ids and not job.get('preempted'):
            job['preempted'] = True
            job['own_stop_binding'] = dict(job.get('movement_binding') or {})
            self._send(job, 'stop', {'confirmed_ids': job['confirmed_ids']}, 'preempted')
            return
        if tool == 'standby_robot':
            self._send(job, 'runtime_stop', {'confirmed_mission_ids': job['confirmed_ids']}, 'standby')
            return
        if tool == 'request_recovery':
            self._execute(job, status)
            return
        if tool in _SELECTED:
            chosen = job['arguments'].get('map') or self._choose_map(status)
            if chosen is None:
                names = ', '.join(_maps(status))
                self._finish(job, 'failed', '사용할 지도를 말씀해 주세요.' + (' 저장 지도: ' + names if names else ' 저장된 지도가 없어요.'))
                return
            if chosen not in _maps(status):
                self._finish(job, 'failed', '지정한 지도를 현재 저장 지도 목록에서 확인하지 못했어요.')
                return
            selected = localization.get('map') or runtime.get('map') or ''
            if not runtime.get('ready'):
                job['map_transition'] = True
                job['starting_manager'] = True
                self._send(job, 'runtime_start', {'mode': 'navigation', 'map': chosen}, 'prepared')
            elif Path(selected).name == chosen and tool in _POSE_REQUIRED and not localization.get('pose_ready'):
                self._submit_mission(job, 'relocalize', {'method': 0, 'initial_pose': {}}, 'pose_preparation')
            elif Path(selected).name != chosen:
                if job.get('map_attempted'):
                    self._finish(job, 'failed', '지도 위치 확인에 실패해 실행을 시작하지 않았어요.')
                    return
                job['map_attempted'] = True
                job['map_transition'] = True
                self._send(job, 'map_select', {'map': chosen}, 'prepared')
            else:
                self._execute(job, status)
        elif tool == 'request_mapping' and (
                not runtime.get('ready') or localization.get('mode') not in {'LOCALIZATION', 'MAPPING'}):
            job['starting_manager'] = not runtime.get('ready')
            # A new map starts on the blank map, not the last chosen one (2026-10-09).
            self._send(job, 'runtime_start', {'mode': 'mapping', 'last_map': False}, 'prepared')
        elif not runtime.get('ready'):
            job['starting_manager'] = True
            self._send(job, 'runtime_start', {'mode': 'mapping'}, 'prepared')
        else:
            self._execute(job, status)

    @staticmethod
    def _choose_map(status):
        maps = _maps(status)
        runtime = status.get('runtime') or {}
        for candidate in (runtime.get('map'), status.get('last_selected_map')):
            if isinstance(candidate, dict):
                candidate = candidate.get('name', candidate.get('map'))
            if isinstance(candidate, str) and Path(candidate).name in maps:
                return Path(candidate).name
        return None

    def _execute(self, job, status):
        if not self._fresh(job):
            return
        localization = (status.get('runtime') or {}).get('localization') or {}
        job['localization_binding'] = {key: localization.get(key) for key in ('runtime_id', 'transition_id')}
        if not self._bind_movement(job, status):
            return
        tool, arguments = job['tool'], job['arguments']
        if tool in {'wake_robot', 'select_saved_map'}:
            self._finish(job, 'succeeded', '저장 지도를 선택하고 실행 준비를 확인했어요.', status)
            return
        if tool == 'request_navigation':
            localization = (status.get('runtime') or {}).get('localization') or {}
            try:
                if self.targets is None or not localization.get('pose_ready'):
                    raise ValueError('no resolver or pose')
                current = self.localization
                selected = localization.get('map') or ''
                if (current.get('mode') != 'LOCALIZATION' or not current.get('pose_ready')
                        or Path(current.get('map') or '').name != Path(selected).name
                        or any(current.get(key) != localization.get(key)
                               for key in ('runtime_id', 'transition_id'))):
                    raise ValueError('localization binding changed')
                pose = self.targets.resolve(arguments['location'], current.get('map'))
                args = pose.arguments
            except (OSError, ValueError):
                self._finish(job, 'failed', '선택 지도에서 등록된 목적지를 확인하지 못해 이동하지 않았어요.')
                return
        elif tool == 'request_follow_person':
            args = {'target_mode': 0, 'target_person_id': '', 'desired_distance_m': 1.0}
        elif tool == 'request_patrol':
            args = {'thoroughness': {'light': 0, 'normal': 1, 'thorough': 2}[arguments['thoroughness']]}
        elif tool == 'request_mapping':
            args = {'map_name': arguments['map_name']}
        elif tool == 'request_relocalization':
            if job.get('map_transition') and arguments['method'] == 'auto':
                localization = (status.get('runtime') or {}).get('localization') or {}
                if localization.get('pose_ready'):
                    self._finish(job, 'succeeded', '지도 준비 중 실행한 위치 보정의 완료를 확인했어요.', localization)
                else:
                    self._finish(job, 'failed', '지도 준비 중 위치 보정을 확인하지 못했어요.', localization)
                return
            args = {'method': 2 if arguments['method'] == 'global_search' else 0, 'initial_pose': {}}
        elif tool == 'request_manual_control':
            args = {'time_allowance': {'sec': 0, 'nanosec': 0}}
        else:
            args = {}
        self._submit_mission(job, _MISSIONS[tool], args, 'manager')

    def _submit_mission(self, job, capability, args, phase):
        if not self._fresh(job):
            return
        binding = job.get('localization_binding') or {}
        guarded_map = capability in {'navigate_to_pose', 'follow_person', 'patrol', 'relocalize', 'autoslam'}
        guarded_movement = guarded_map or capability == 'manual_drive'
        movement = job.get('movement_binding') or {}
        movement_id = movement.get('movement_runtime_id') if guarded_movement else ''
        movement_epoch = movement.get('movement_epoch') if guarded_movement else 0
        if guarded_movement and (not isinstance(movement_id, str) or not movement_id
                                 or type(movement_epoch) is not int or movement_epoch < 0):
            self._finish(job, 'unknown', '현재 이동 중지 세대 정보를 확인하지 못해 실행하지 않았어요.')
            return
        runtime_id = binding.get('runtime_id') if guarded_map else ''
        transition_id = binding.get('transition_id') if guarded_map else 0
        if guarded_map and (not isinstance(runtime_id, str) or not runtime_id
                            or type(transition_id) is not int or transition_id < 0):
            self._finish(job, 'failed', '현재 지도 전환 식별자를 확인하지 못해 실행하지 않았어요.')
            return
        job.update(state='running', phase=phase, message='Manager에 실행을 요청했어요. 결과를 확인할게요.')
        goal_id = uuid5(NAMESPACE_URL, _id(job['request_id'], job['step'] + 1))
        job['goal_id'] = goal_id.hex
        job['goal_capability'] = capability
        policy = dict(require_preemption_confirmation=True,
                      confirmed_preemption_mission_ids=tuple(job['confirmed_ids']),
                      expected_localization_runtime_id=runtime_id,
                      expected_localization_transition_id=transition_id,
                      require_movement_epoch=guarded_movement,
                      movement_runtime_id=movement_id, movement_epoch=movement_epoch)
        request_id = self.journal.step(job, capability, dict(arguments=args, **policy), goal_id.hex)
        job['pending_id'] = request_id
        self.journal.save(job)
        self.waiting[request_id] = job['request_id']
        try:
            self.manager.submit(capability, args, request_id=request_id,
                                goal_uuid=goal_id, **policy)
            self.handle(self.manager.snapshot(request_id))
        except Exception:
            self.events.append((request_id, dict(success=False, code='unknown', result={},
                                                message='Manager 요청 상태를 확인하지 못했어요. 다시 보내지 않았어요.')))

    def _bind_movement(self, job, status):
        current = {key: (status.get('system') or {}).get(key)
                   for key in ('movement_runtime_id', 'movement_epoch')}
        previous = job.get('movement_binding')
        own_stop = job.get('own_stop_binding')
        if own_stop:
            if (current['movement_runtime_id'] != own_stop.get('movement_runtime_id')
                    or type(own_stop.get('movement_epoch')) is not int
                    or current['movement_epoch'] != own_stop['movement_epoch'] + 1):
                self._finish(job, 'failed', '준비 중 다른 이동 중지나 로봇 재시작을 관측해 다음 동작을 보내지 않았어요.')
                return False
            job.pop('own_stop_binding', None)
        elif previous and current != previous:
            self._finish(job, 'failed', '준비 중 다른 이동 중지나 로봇 재시작을 관측해 다음 동작을 보내지 않았어요.')
            return False
        if (isinstance(current['movement_runtime_id'], str) and current['movement_runtime_id']
                and type(current['movement_epoch']) is int):
            job['movement_binding'] = current
        return True

    def _complete_owned_stop(self, job):
        for request_id in job.get('stop_device_pending', []):
            observed = self.device.snapshot(request_id)
            if not observed['done'] or observed['code'] != 'canceled':
                return
        for request_id in job['stop_owned_pending']:
            observed = self.manager.snapshot(request_id)
            if not observed.get('terminal'):
                return
            if observed.get('state') == 'FAILED':
                retained = self.recent_results.get(observed.get('goal_id'), {})
                if retained.get('downstream_terminal') is not True:
                    return
        self._finish(job, 'succeeded', '현재 이동 작업과 늦게 접수된 음성 이동 요청의 종료를 확인했어요.',
                     job['stop_result'])

    def handle(self, event):
        request_id = event.get('request_id')
        if request_id not in self.waiting:
            return False
        if event.get('kind') == 'unknown' or event.get('terminal') or event.get('state') in {'SUCCEEDED', 'FAILED', 'CANCELED', 'REJECTED', 'UNAVAILABLE'}:
            import yaml
            try:
                value = yaml.safe_load(event.get('result_yaml') or '{}') or {}
            except (ValueError, yaml.YAMLError):
                value = {}
            if not isinstance(value, dict):
                value = {}
            value['mission_id'] = event.get('mission_id')
            self.events.append((request_id, dict(success=event.get('state') == 'SUCCEEDED',
                code=value.get('code', event.get('kind', 'failed')), result=value,
                message=event.get('reason') or 'Manager의 실행 결과를 확인했어요.')))
        return True

    def _fresh(self, job):
        if self.closed or job['state'] in _FINAL:
            return False
        if self.clock() >= job['deadline']:
            self._finish(job, 'unknown', '실행 준비 시간이 지나 다음 작업을 보내지 않았어요.')
            return False
        return True

    def preempt_preparations(self):
        for job in list(self.jobs.values()):
            if job['state'] not in _FINAL and job['phase'] != 'manager':
                if job['phase'] == 'pose_preparation':
                    self.manager.cancel(job['pending_id'])
                else:
                    self.device.cancel(job.get('pending_id', ''))
                self._finish(job, 'canceled', '상황 확인을 우선해 남은 실행 준비를 취소했어요.')

    def context(self, user_id, conversation_id):
        values = []
        for job in self.journal.recent():
            if (job['context'].get('user_id') != user_id
                    or job['context'].get('conversation_id') != conversation_id):
                continue
            item = {key: job.get(key) for key in ('tool', 'state', 'message', 'result', 'publication')}
            # Keep whole fields; never truncate JSON claims mid-value.
            if len(_json(item)) > 3000:
                item['result'] = {'available_in_result_link': True}
            if len(_json(values + [item])) > 4000:
                break
            values.append(item)
            if len(values) == 3:
                break
        return values

    def _ask_conflicts(self, job):
        labels = {}
        system = job.get('conflict_status') or {}
        for collection in system.values():
            for mission in collection if isinstance(collection, list) else []:
                if isinstance(mission, dict):
                    labels[mission.get('mission_id')] = mission.get('capability_id', '작업')
        targets = ', '.join(f'{labels.get(item, "작업")} [{item}]' for item in job['conflicts'])
        if len(targets) > 1000:
            self._finish(job, 'failed', '확인할 작업이 너무 많아요. 웹에서 실행 중인 작업을 정리해 주세요.')
            return
        self._ask(job, targets + ' 작업을 종료하고 요청을 계속할까요?')

    def _ask(self, job, message):
        job.update(state='awaiting_confirmation', message=message, confirmation_deadline=self.clock() + 120)
        self.journal.save(job)
        self.notify(dict(request_id=job['request_id'], state=job['state'], text=message, result={}))

    def _confirm(self, confirmation):
        context = confirmation['context']
        candidates = [job for job in self.jobs.values() if job['state'] == 'awaiting_confirmation'
                      and all(job['context'].get(key) == context.get(key) for key in ('user_id', 'conversation_id', 'generation'))
                      and self.clock() < job['confirmation_deadline']]
        answer = re.sub(r'[\s.!?。！？]+', '', context['utterance'])
        accepted = confirmation['arguments']['confirm']
        if len(candidates) != 1 or answer not in (_YES if accepted else _NO):
            self._finish(confirmation, 'failed', '지금 확인할 요청과 직접 말씀하신 예/아니요를 확인하지 못했어요.')
            return confirmation['message']
        job = candidates[0]
        if not accepted:
            self._finish(job, 'canceled', '요청을 취소했어요. 확인이 필요했던 작업은 실행하지 않았어요.')
        else:
            job.pop('confirmation_deadline', None)
            job['confirmed_ids'] = job.get('conflicts', [])
            job['delete_confirmed'] = True
            job['preempted'] = False
            self._send(job, 'map_list' if job['tool'] == 'delete_saved_map' else 'status', {},
                       'delete_check' if job['tool'] == 'delete_saved_map' else 'prepare')
        self._finish(confirmation, 'succeeded', '확인 답변을 처리했어요.')
        return confirmation['message']

    def _finish(self, job, state, message, result=None):
        job.update(state=state, message=message[:1500], result=result or {})
        job.pop('confirmation_deadline', None)
        self.journal.save(job)
        self.notify(dict(request_id=job['request_id'], state=state, text=job['message'], result=job['result']))
        self._publish(job, state)

    def _publish(self, job, state):
        homecam = 'homecam' in job['tool']
        if homecam and state != 'succeeded':
            return
        kind = {'get_homecam_events': 'event', 'get_homecam_recordings': 'recording',
                'get_homecam_falls': 'fall'}.get(job['tool'], 'homecam' if homecam
                                             else 'status' if job['tool'] in _QUERIES else 'mission')
        arguments = {'kind': kind,
                     'title': job['tool'], 'summary': job['message'][:1000], 'state': state}
        if job['tool'] in {'wake_robot', 'select_saved_map'} and state == 'succeeded':
            runtime = job['result'].get('runtime') or {}
            selected = runtime.get('map') or (runtime.get('localization') or {}).get('map')
            if isinstance(selected, str) and selected:
                filename = Path(selected).name
                arguments.update(kind='map', referenceId='real-' +
                                 hashlib.sha256(filename.encode('utf-8')).hexdigest()[:24])
        fields = {'event': ('events',), 'recording': ('recordings',), 'fall': ('incidents',)}
        for field in fields.get(kind, ()):
            items = job['result'].get(field, [])
            if isinstance(items, list) and items and isinstance(items[0], dict):
                reference = items[0].get('id')
                if isinstance(reference, str) and len(reference) <= 128:
                    arguments['referenceId'] = reference
                    break
        if kind in {'event', 'recording', 'fall'} and 'referenceId' not in arguments:
            return
        request_id = self.journal.step(job, 'result_publish', arguments)
        def completed(result):
            if self.closed:
                return
            self.journal.observed(request_id, result)
            if result.get('success'):
                job['publication'] = result.get('result', {})
                self.journal.save(job)
        try:
            self.device.send(request_id, 'result_publish', arguments, completed)
        except Exception:
            completed({'success': False, 'code': 'unknown', 'result': {}})

    @staticmethod
    def _describe(job, value, message):
        tool = job['tool']
        if tool == 'get_robot_status':
            runtime = value.get('runtime') or {}
            active = _active_ids(value)
            battery = value.get('battery')
            state = runtime.get('state')
            description = {
                'STOPPED': '로봇 기능은 꺼져 있어요.',
                'STARTING': '로봇 기능을 켜고 있어요.',
                'RUNNING': '로봇 기능이 켜져 있어요.',
                'STOPPING': '로봇 기능을 끄고 있어요.',
                'ERROR': '로봇 기능에 오류가 있어 확인이 필요해요.',
            }.get(state, '로봇 기능의 현재 상태를 아직 확인하지 못했어요.')
            if state == 'STOPPED' and (value.get('voice') or {}).get('ready') is True:
                description = '로봇 기능은 꺼져 있고, 음성 대화는 대기 중이에요.'
            return (description + ' '
                    f"확인된 진행 작업은 {len(active)}개예요. "
                    + (f'배터리 관측값은 {battery}예요.' if battery is not None else '배터리 값은 아직 확인되지 않았어요.'))
        if tool == 'list_saved_maps':
            return '저장 지도: ' + (', '.join(_maps(value)) or '현재 확인된 지도가 없어요.')
        if tool == 'get_robot_observations':
            observations = value.get('observations') or {}
            person = observations.get('person') or {}
            tracking = value.get('tracking') or {}
            if not person.get('current') and not tracking.get('current'):
                return '현재 시점으로 확인된 사람 탐지나 추적 관측이 없어요. 과거 관측을 현재 상태로 판단하지 않을게요.'
            return ('현재 사람 탐지 관측이 있어요. ' if person.get('current') else '') + (
                '현재 추적 상태 관측도 확인했어요. 발화자나 등록 인물의 신원을 확인한 것은 아니에요.'
                if tracking.get('current') else '이 관측만으로 인물의 신원을 알 수는 없어요.')
        if tool in {'get_homecam_events', 'get_homecam_recordings', 'get_homecam_falls'}:
            field, label = {'get_homecam_events': ('events', '이벤트'),
                            'get_homecam_recordings': ('recordings', '녹화'),
                            'get_homecam_falls': ('incidents', '낙상 기록')}[tool]
            items = value.get(field, [])
            count = len(items) if isinstance(items, list) else 0
            latest = items[0] if count and isinstance(items[0], dict) else {}
            at = latest.get('occurredAt') or latest.get('createdAt') or latest.get('startedAt')
            return f'최근 {label} {count}건을 조회했어요.' + (f' 가장 최근 기록 시각은 {at}예요.' if at else '') + (
                ' 과거 기록이며 현재 사고 여부를 뜻하지는 않아요.' if tool == 'get_homecam_falls'
                else ' 자세한 내용은 결과 링크에서 확인할 수 있어요.')
        if tool == 'get_homecam_status':
            names = {'cameraEnabled': '카메라', 'microphoneEnabled': '마이크',
                     'monitoringEnabled': '감시', 'fallEnabled': '낙상 감시'}
            settings = [f'{label} {"켜짐" if value[key] else "꺼짐"}'
                        for key, label in names.items() if type(value.get(key)) is bool]
            parts = ['저장된 설정은 ' + ', '.join(settings) + '이에요.' if settings
                     else '저장된 Homecam 설정값은 아직 확인되지 않았어요.']
            for field, label in (('mediaApplyReceipt', '홈캠'), ('fallApplyReceipt', '낙상 감시')):
                receipt = value.get(field) or {}
                state = receipt.get('state')
                if state in {'reported_applied', 'reported_failed'}:
                    outcome = '설정을 적용했다는' if state == 'reported_applied' else '설정 적용에 실패했다는'
                    age = ('최근 ' if receipt.get('fresh') is True else
                           '오래된 ' if receipt.get('fresh') is False else '')
                    parts.append(f'{label} {outcome} {age}회신이 있어요.')
                elif state == 'waiting':
                    parts.append(f'{label} 설정의 적용 회신을 기다리고 있어요.')
                elif state == 'no_response':
                    parts.append(f'{label} 설정의 적용 회신은 아직 없어요.')
                else:
                    parts.append(f'{label} 설정의 적용 회신 상태는 아직 확인되지 않았어요.')
            if value.get('runtimeVerified') is not True:
                parts.append('현재 정상 동작 여부는 별도 확인이 필요해요.')
            return ' '.join(parts)
        if tool == 'get_map_zones':
            zones = value.get('zones', [])
            labels = [str(zone.get('name') or f'{index + 1}번 구역')
                      for index, zone in enumerate(zones) if isinstance(zone, dict)]
            return f'등록된 구역 {len(labels)}개를 확인했어요. ' + ', '.join(labels[:10])
        if tool == 'update_homecam_settings':
            return ('설정을 저장했어요. 기기의 실제 적용은 아직 확인 전이에요.'
                    if value.get('saved') and not value.get('runtimeVerified')
                    else 'Homecam 설정 변경 결과를 확인했어요.')
        if tool == 'standby_robot':
            return '로봇 기능의 대기 전환을 확인했어요. 음성 대화는 계속할 수 있어요.'
        if tool in {'stop_robot_movement', 'cancel_voice_mission'}:
            return '현재 이동 작업의 중지를 확인했어요.'
        if tool == 'delete_saved_map':
            return '확인한 지도를 삭제했어요.'
        if tool == 'update_map_zone':
            return '선택한 구역 설정을 저장했어요.'
        return message or '요청한 작업의 완료 결과를 확인했어요.'

    def close(self):
        self.closed = True
        self.device.close()
        self.journal.close()
