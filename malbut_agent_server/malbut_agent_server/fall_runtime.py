"""Composition/settings for the opt-in Cloud fall runtime; no ROS on import."""

from dataclasses import dataclass
from pathlib import Path
import re
import time
from uuid import uuid4

from malbut_agent_server.application.fall_detector_input import bounded_object
from malbut_agent_server.domain.fall_monitoring import (
    AgentCheckReply, FallRuntimePolicy, SubjectCheckState, SubjectObservation,
    VoiceAnswer, positive,
)


@dataclass(frozen=True)
class FallNodeSettings:
    device_id: str
    journal_path: Path
    cloud_key_file: Path
    model: str
    image_topic: str
    policy: FallRuntimePolicy
    retention_s: float
    buffer_bytes: int
    buffer_frames: int
    input_fps: float
    max_source_age_s: float
    control_lease_s: float

    @classmethod
    def parse(cls, text):
        data = bounded_object(text)
        fields = {'device_id', 'journal_path', 'cloud_key_file', 'model', 'image_topic',
                  'policy', 'retention_s', 'buffer_bytes', 'buffer_frames', 'input_fps',
                  'max_source_age_s', 'control_lease_s'}
        if set(data) != fields:
            raise ValueError('runtime configuration fields do not match contract')
        if not isinstance(data['policy'], dict):
            raise ValueError('policy must be an object')
        policy = FallRuntimePolicy.agreed(**data['policy'])
        from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (
            OllamaCloudFallProvider,
        )
        OllamaCloudFallProvider(model=data['model'], api_key='validate-only')
        if (not isinstance(data['device_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', data['device_id'])):
            raise ValueError('invalid device ID')
        if (not isinstance(data['image_topic'], str)
                or not re.fullmatch(r'/[A-Za-z0-9_/]{1,180}', data['image_topic'])):
            raise ValueError('invalid image topic')
        for name in ('journal_path', 'cloud_key_file'):
            if not isinstance(data[name], str) or not Path(data[name]).is_absolute():
                raise ValueError('protected paths must be absolute')
            data[name] = Path(data[name])
        for name in ('retention_s', 'input_fps', 'max_source_age_s', 'control_lease_s'):
            positive(data[name], name)
        for name, maximum in (('buffer_bytes', 256 * 1024 * 1024), ('buffer_frames', 10000)):
            if type(data[name]) is not int or not 1 <= data[name] <= maximum:
                raise ValueError('invalid bounded buffer configuration')
        if (data['retention_s'] < policy.clip_window_s or data['retention_s'] > 300
                or data['input_fps'] > 30 or data['control_lease_s'] > 60
                or data['buffer_frames'] < policy.max_images or policy.max_images > 64
                or data['max_source_age_s'] > policy.max_frame_age_s):
            raise ValueError('inconsistent runtime limits')
        data['policy'] = policy
        return cls(**data)


class FallRuntimeControl:
    """Experimental Manager boundary; OFF until a fresh runtime-bound lease.

    It does not replace authentication/SROS2 on a shared ROS graph. Stored
    messages from a previous process cannot enable a newly started process.
    """

    def __init__(self, adapter, *, lease_s, clock=time.monotonic):
        positive(lease_s, 'lease_s')
        self.adapter, self._clock, self.lease_s = adapter, clock, lease_s
        self.runtime_id = str(uuid4())
        self._settings = None
        self._last_control = None
        self._media_permission = False
        self._closed = False

    @property
    def accepting_images(self):
        return bool(not self._closed and self._settings and self._last_control is not None
                    and self._clock() - self._last_control < self.lease_s
                    and self._settings['enabled'] and self._media_permission)

    def settings(self, payload):
        data = bounded_object(payload)
        if (set(data) != {'runtimeId', 'revision', 'enabled', 'cloudConsent', 'connected'}
                or data['runtimeId'] != self.runtime_id
                or type(data['revision']) is not int or data['revision'] < 1
                or any(type(data[k]) is not bool
                       for k in ('enabled', 'cloudConsent', 'connected'))):
            raise ValueError('invalid runtime control')
        if self._closed:
            raise ValueError('runtime already closed')
        if self._settings is not None:
            if data['revision'] < self._settings['revision']:
                raise ValueError('stale settings')
            if data['revision'] == self._settings['revision'] and data != self._settings:
                raise ValueError('conflicting settings revision')
        self._settings, self._last_control = data, self._clock()
        self.refresh()

    def media_permission(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('invalid media permission')
        self._media_permission = enabled
        self.refresh()

    def refresh(self):
        active = self.accepting_images
        self.adapter.configure(enabled=active, camera_enabled=active,
                               cloud_consent=bool(active and self._settings['cloudConsent']),
                               connected=bool(active and self._settings['connected']))

    def close(self):
        self._closed = True
        self.refresh()


def parse_agent_reply(payload):
    data = bounded_object(payload)
    if set(data) != {'incident_id', 'question_id', 'subject_key', 'evidence_revision',
                     'answer', 'question_played'}:
        raise ValueError('invalid Agent reply fields')
    data['answer'] = VoiceAnswer(data['answer'])
    return AgentCheckReply(**data)


def parse_subject_observation(payload):
    data = bounded_object(payload)
    if set(data) != {'incident_id', 'subject_key', 'evidence_revision', 'request_id',
                     'observed_at', 'state', 'association_verified'}:
        raise ValueError('invalid subject observation fields')
    data['state'] = SubjectCheckState(data['state'])
    return SubjectObservation(**data)


def apply_decision(monitor, payload):
    """Explicit Manager/operator decisions; no inferred medical or voice state."""
    data = bounded_object(payload)
    action = data.get('action')
    fields = {'incident_id', 'evidence_revision', 'action'}
    if action == 'resolve':
        fields.add('reason')
    elif action == 'unresolved':
        fields.add('suspicion_persists')
    elif action not in {'recheck', 'ask_again'}:
        raise ValueError('unsupported decision')
    if set(data) != fields or type(data['evidence_revision']) is not int:
        raise ValueError('invalid decision')
    incident = monitor.incident(data['incident_id'])
    if incident.revision != data['evidence_revision']:
        raise ValueError('stale decision')
    if action == 'resolve':
        return monitor.resolve(incident.incident_id, revision=incident.revision,
                               reason=data['reason'])
    if action == 'unresolved':
        return monitor.mark_unresolved(incident.incident_id, revision=incident.revision,
                                       suspicion_persists=data['suspicion_persists'])
    if action == 'recheck':
        return monitor.request_recheck(incident.incident_id)
    return monitor.ask_question(incident.incident_id)


def event_metadata(event):
    """Local Manager handoff only; never serialize RGB, prompts or Cloud secrets."""
    result = dict(event_id=event.event_id, kind=event.kind, incident_id=event.incident_id,
                  reason=event.reason, question_id=event.question_id,
                  subject_key=event.subject_key, evidence_revision=event.evidence_revision,
                  notification_level=(event.notification_level.value
                                      if event.notification_level else None))
    if event.reply is not None:
        result['video_assessment'] = event.reply.assessment.value
        # Free model explanations are not instructions and are not routed to
        # the conversation Agent by this transport.
    if event.request is not None:
        result['request_id'] = event.request.request_id
        result['sample_times'] = [f.captured_at for f in event.request.window.frames]
    return result
