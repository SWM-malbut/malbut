"""Prepare fixed voice commands and submit them on the ROS owning thread."""

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from threading import RLock, get_ident
import time

from malbut_agent_server.mission_speech import event_speech
from malbut_agent_server.tools import SPEECH_MISSION_TOOLS


_CAPABILITIES = {
    'request_navigation': 'navigate_to_pose',
    'request_follow_person': 'follow_person',
    'request_patrol': 'patrol',
}
_TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELED', 'REJECTED', 'UNAVAILABLE'}
_NAV_UNAVAILABLE = '목적지 이동 설정을 확인할 수 없어 이동하지 않았어요.'
_MAP_UNAVAILABLE = '사용 중인 저장 지도를 확인할 수 없어 이동하지 않았어요.'
_NAV_CHANGED = '지도나 목적지 설정이 변경되어 이동하지 않았어요. 다시 말씀해 주세요.'
_UNKNOWN = '실행 요청의 상태를 확인할 수 없어요. 시작 요청을 다시 보내지는 않았어요.'
_GUARD_FAILED = '요청 조건을 확인할 수 없어 요청을 보내지 않았어요.'


@dataclass(frozen=True)
class SpeechMissionProposal:
    """Detach model arguments from the server-owned execution binding."""

    request_id: str
    tool_name: str
    arguments_json: str
    prepared_at: float
    message: str
    location: str = ''
    map_path: str = ''
    map_generation: int = 0
    target_digest: str = ''
    blocked: bool = False
    _issuer: object = field(default=None, repr=False, compare=False)


@dataclass
class _OwnedMission:
    capability_id: str
    state: str = 'SUBMITTING'
    terminal: bool = False


class SpeechMissions:
    """Limit voice execution to fixed capabilities and retain uncertain Goals.

    Construction and dispatch belong to the ROS owner. Preparation may run on
    the dialogue worker: it reads local destination configuration but never
    contacts Manager. Ownership and once-only dispatch records are local to
    this process, matching ManagerClient's lifecycle.
    """

    def __init__(self, manager, navigation_targets=None, clock=time.monotonic):
        """Bind an existing Manager client without creating ROS entities."""
        self._manager = manager
        self._targets = navigation_targets
        self._clock = clock
        self._owner = get_ident()
        self._issuer = object()
        self._lock = RLock()
        self._responses = {}
        self._owned = {}
        self._active_map = None
        self._localization_identity = None
        self._map_generation = 0

    def observe_localization(self, payload):
        """Invalidate prepared destinations whenever the selected map changes.

        Manager publishes a latched state on transitions, not a heartbeat.
        Consequently elapsed time alone cannot invalidate a valid observation.
        Malformed states and mapping/switching modes clear the usable map.
        """
        identity = None
        active_map = None
        try:
            if isinstance(payload, str):
                if len(payload) > 16384:
                    raise ValueError('localization state is too large')
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                raise ValueError('localization state must be an object')
            mode, map_path = payload.get('mode'), payload.get('map')
            if mode not in {'LOCALIZATION', 'MAPPING', 'SWITCHING', 'ERROR'}:
                raise ValueError('invalid localization mode')
            if map_path is not None and (
                not isinstance(map_path, str) or not map_path.strip()
                or not Path(map_path).is_absolute()
            ):
                raise ValueError('invalid map path')
            identity = (mode, map_path)
            if mode == 'LOCALIZATION' and map_path is not None:
                active_map = map_path
        except (TypeError, ValueError):
            pass
        with self._lock:
            if identity != self._localization_identity:
                self._map_generation += 1
            self._localization_identity = identity
            self._active_map = active_map

    def prepare(self, request_id, tool_name, arguments):
        """Validate inputs and bind a destination without executing it."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError('request_id must be nonblank')
        if tool_name not in SPEECH_MISSION_TOOLS:
            raise ValueError('unsupported voice mission tool')
        if not isinstance(arguments, dict):
            raise ValueError('tool arguments must be an object')
        location = ''
        if tool_name == 'request_navigation':
            if set(arguments) != {'location'} or not isinstance(
                arguments['location'], str,
            ) or not arguments['location'].strip():
                raise ValueError('navigation requires exactly one location')
            location = arguments['location']
        elif tool_name == 'request_patrol':
            levels = {'light': 0, 'normal': 1, 'thorough': 2}
            level = arguments.get('thoroughness')
            if set(arguments) != {'thoroughness'} or (
                not isinstance(level, str) or level not in levels
            ):
                raise ValueError('patrol requires a supported thoroughness')
            manager_arguments = {'thoroughness': levels[level]}
        else:
            if arguments:
                raise ValueError('this voice mission takes no arguments')
            manager_arguments = ({
                'target_mode': 0, 'target_person_id': '',
                'desired_distance_m': 1.0,
            } if tool_name == 'request_follow_person' else {})
        with self._lock:
            fields = {
                'request_id': request_id, 'tool_name': tool_name,
                'prepared_at': self._clock(), 'message': '요청을 확인하고 있어요.',
                '_issuer': self._issuer,
            }
            if tool_name == 'request_navigation':
                fields.update(
                    location=location, map_path=self._active_map or '',
                    map_generation=self._map_generation,
                )
                if self._targets is None:
                    return self._blocked(fields, _NAV_UNAVAILABLE)
                if self._active_map is None:
                    return self._blocked(fields, _MAP_UNAVAILABLE)
                try:
                    target = self._targets.resolve(location, self._active_map)
                    manager_arguments = target.arguments
                    fields.update(
                        map_path=target.map_path, target_digest=target.digest,
                    )
                except (OSError, ValueError):
                    return self._blocked(
                        fields, '저장된 지도에서 그 목적지를 확인하지 못했어요. '
                        '등록된 목적지 이름을 포함해 이동 요청을 다시 말씀해 주세요.',
                    )
            return SpeechMissionProposal(
                arguments_json=self._encode(manager_arguments), **fields,
            )

    def dispatch(self, proposal, *, guard=None):
        """Consume once and recheck caller conditions just before Manager I/O.

        ``guard`` returns None to allow sending, or a trusted refusal string.
        It runs after destination file reads, so those reads cannot consume the
        caller's validity window unnoticed. Errors block before sending.
        """
        if get_ident() != self._owner:
            raise RuntimeError('voice missions must dispatch on the ROS owner')
        if (not isinstance(proposal, SpeechMissionProposal)
                or proposal._issuer is not self._issuer):
            raise ValueError('proposal belongs to another dispatcher')
        with self._lock:
            if proposal.request_id in self._responses:
                return self._responses[proposal.request_id]
            # Consume before any operation which could send a Goal. Even an
            # exception leaves the request spent; a transport error is not a
            # reason to retry a possibly delivered request.
            self._responses[proposal.request_id] = _UNKNOWN
            response = self._dispatch(proposal, guard)
            self._responses[proposal.request_id] = response
            return response

    def handle(self, event):
        """Track only voice-owned Manager events without consuming others."""
        if not isinstance(event, dict):
            return False
        with self._lock:
            owned = self._owned.get(event.get('request_id'))
            if (owned is None
                    or event.get('capability_id') != owned.capability_id):
                return False
            state = event.get('state')
            if owned.terminal:
                return True
            if isinstance(state, str):
                owned.state = state
                # UNKNOWN remains cancellable, including while Goal acceptance
                # is late. Only a confirmed terminal state releases ownership.
                owned.terminal = state in _TERMINAL
            return True

    def _dispatch(self, proposal, guard):
        if proposal.blocked:
            return proposal.message
        if proposal.tool_name == 'cancel_voice_mission':
            return self._cancel(guard)
        if proposal.tool_name == 'request_navigation':
            if (self._active_map != proposal.map_path
                    or self._map_generation != proposal.map_generation):
                return _NAV_CHANGED
            try:
                target = self._targets.resolve(
                    proposal.location, self._active_map,
                )
                unchanged = (
                    target.map_path == proposal.map_path
                    and target.digest == proposal.target_digest
                    and self._encode(target.arguments)
                    == proposal.arguments_json
                )
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                return _NAV_CHANGED
        capability = _CAPABILITIES[proposal.tool_name]
        manager_id = 'speech-mission:' + hashlib.sha256(
            proposal.request_id.encode('utf-8'),
        ).hexdigest()
        arguments = json.loads(proposal.arguments_json)
        refusal = self._guard_refusal(guard)
        if refusal is not None:
            return refusal
        self._owned[manager_id] = _OwnedMission(capability)
        try:
            submitted_id = self._manager.submit(
                capability, arguments, request_id=manager_id,
            )
            if submitted_id != manager_id:
                raise ValueError('Manager request identity changed')
            snapshot = self._manager.snapshot(manager_id)
            if (snapshot.get('request_id') != manager_id
                    or snapshot.get('capability_id') != capability):
                raise ValueError('Manager observation identity changed')
            self.handle(snapshot)
            speech = event_speech(snapshot)
            return speech or '실행 요청을 보냈어요. 접수 결과를 확인할게요.'
        except Exception:
            self._owned[manager_id].state = 'UNKNOWN'
            return _UNKNOWN

    def _cancel(self, guard):
        pending = [
            request_id for request_id, owned in self._owned.items()
            if not owned.terminal
        ]
        if not pending:
            return '음성으로 요청한 실행 중인 동작이 없어요.'
        refusal = self._guard_refusal(guard)
        if refusal is not None:
            return refusal
        uncertain = False
        for request_id in pending:
            try:
                snapshot = self._manager.cancel(request_id)
                self.handle(snapshot)
                if snapshot.get('kind') in {
                    'cancel_unknown', 'cancel_rejected',
                }:
                    uncertain = True
            except Exception:
                uncertain = True
        if uncertain:
            return ('음성으로 요청한 동작의 취소를 요청했지만 접수 여부를 '
                    '확인하지 못했어요. 종료된 것으로 판단하지 않을게요.')
        return '음성으로 요청한 동작의 취소를 요청했어요. 종료 여부를 확인할게요.'

    @staticmethod
    def _guard_refusal(guard):
        if guard is None:
            return None
        try:
            refusal = guard()
        except Exception:
            return _GUARD_FAILED
        if refusal is None:
            return None
        if isinstance(refusal, str) and refusal.strip():
            return refusal
        return _GUARD_FAILED

    @staticmethod
    def _encode(arguments):
        return json.dumps(
            arguments, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False,
        )

    @staticmethod
    def _blocked(fields, message):
        return SpeechMissionProposal(
            arguments_json='{}', blocked=True,
            **dict(fields, message=message),
        )
