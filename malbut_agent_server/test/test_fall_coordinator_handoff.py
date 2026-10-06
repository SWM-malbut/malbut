"""Core-to-coordinator migration checks with synthetic Cloud and mission futures.

Use the real event codec, coordinator, mission-link callbacks and VLM decisions.
Do not start ROS, a mission manager, an Agent, inference, playback or HTTP.
"""

import asyncio
from concurrent.futures import Future
from dataclasses import replace
import json
import sys
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_agent_server.domain.fall_monitoring import (
    CandidateKind, CloudFallReply, CloudPersonFinding, CloudPersonRegion,
    IncidentState, VideoAssessment, VoiceAnswer,
)
from malbut_agent_server.fall_runtime import apply_decision, event_metadata
from malbut_fall_coordinator.fall_confirmation import FallConfirmationCoordinator
from malbut_fall_coordinator.fall_confirmation_link import FallConfirmationLink
from test_cloud_fall_monitor import candidate, enable, frame, make


def manager_state(*active):
    return SimpleNamespace(active_foreground_missions=list(active),
                           active_background_missions=[], pending_missions=[],
                           suspended_missions=[])


def suspected_scene(assessment=VideoAssessment.SUSPECTED_FALL):
    kind = (CandidateKind.MOTION_SEEN if assessment is VideoAssessment.OBSERVED_FALL
            else CandidateKind.ALREADY_DOWN)
    finding = CloudPersonFinding(assessment, kind, (
        CloudPersonRegion(0, (.1, .4, .7, .9)),
        CloudPersonRegion(1, (.1, .4, .7, .9)),
    ))
    return CloudFallReply(assessment, 'private-model-text-not-an-instruction', (finding,))


class Handoff:
    """Leave production callback logic intact; replace only external peers."""

    def __init__(self):
        self.monitor, self.clock, self.provider = make(clip_window_s=5)
        enable(self.monitor)
        self.now = 0.0
        link = self.link = object.__new__(FallConfirmationLink)
        link.node, link.lock, link.clock = Mock(), RLock(), lambda: self.now
        link.goal_response_timeout_s = link.server_loss_timeout_s = 5.0
        link.result_timeout_s = 610.0
        link.coordinator = FallConfirmationCoordinator(runtime_id='vlm')
        link.action_type = SimpleNamespace(Goal=SimpleNamespace)
        link.message_type = SimpleNamespace
        link.client, link.decisions, link.timer = Mock(), Mock(), Mock()
        link.client.server_is_ready.return_value = True
        link.client.send_goal_async.side_effect = lambda goal: Future()
        link.agent_presence = Mock()
        link.agent_presence.server_is_ready.return_value = True
        link.manager_state = manager_state()
        link.request = link.goal_future = link.handle = None
        link.sent_at = link.next_attempt = 0.0
        link.accepted_at = link.server_missing_since = None

    def relay(self, events):
        for event in events:
            metadata = dict(event_metadata(event), boot_id=self.monitor.boot_id,
                            runtime_id='vlm')
            self.link.on_event(SimpleNamespace(data=json.dumps(metadata)))

    def scan(self, reply=None, *, stamp=160.0):
        self.clock.value = stamp
        self.monitor.ingest_rgb(frame(stamp - .5))
        self.monitor.ingest_rgb(frame(stamp))
        self.provider.reply = reply or suspected_scene()
        assert asyncio.run(self.monitor.run_once())
        events = self.monitor.drain_events()
        self.relay(events)
        return events

    def accept(self):
        pending = self.link.goal_future
        assert pending is not None
        result = Future()
        handle = Mock(accepted=True)
        handle.get_result_async.return_value = result
        handle.cancel_goal_async.return_value = Future()
        pending.set_result(handle)
        return handle, result

    def complete(self, future, *, assessment='resolved', help_needed=False,
                 status=4, result_yaml=None):
        payload = (json.dumps(dict(situation_assessment=assessment, help_needed=help_needed))
                   if result_yaml is None else result_yaml)
        future.set_result(SimpleNamespace(status=status, result=SimpleNamespace(
            message='', result_yaml=payload)))

    def commands(self):
        return [json.loads(call.args[0].data)
                for call in self.link.decisions.publish.call_args_list]

    def apply(self, command):
        return apply_decision(self.monitor, json.dumps(command))


@pytest.fixture
def handoff(monkeypatch):
    monkeypatch.setitem(sys.modules, 'action_msgs.msg', SimpleNamespace(
        GoalStatus=SimpleNamespace(STATUS_SUCCEEDED=4, STATUS_ABORTED=6)))
    flow = Handoff()
    yield flow
    flow.link.close()
    asyncio.run(flow.monitor.close())


@pytest.mark.parametrize('assessment', ['resolved', 'unknown', 'confirmed_incident'])
@pytest.mark.parametrize('help_needed', [False, True])
def test_cloud_only_scene_round_trip_keeps_null_subject(handoff, assessment, help_needed):
    flow = handoff
    events = flow.scan()
    question, = [event for event in events if event.kind == 'question_requested']
    assert question.confirmation_scope == 'scene' and question.subject_key is None
    assert flow.provider.calls[0].purpose == 'crosscheck'
    assert len(flow.provider.calls) == 1
    goal = flow.link.client.send_goal_async.call_args.args[0]
    assert goal.capability_id == 'fall_confirmation'
    arguments = json.loads(goal.arguments_yaml)
    assert set(arguments) == {'request_id', 'situation_type', 'summary'}
    assert arguments['request_id'] == question.question_id
    assert arguments['situation_type'] == 'fall'
    assert '특정인을 지목하지 말고' in arguments['summary']
    assert '다른 사람의 상태로 판단하지 마세요' in arguments['summary']
    assert 'private-model-text' not in arguments['summary']
    flow.link.agent_presence.send_goal_async.assert_not_called()

    _, result = flow.accept()
    flow.complete(result, assessment=assessment, help_needed=help_needed)
    command, = flow.commands()
    assert command['subject_key'] is None
    assert command['incident_id'] == question.incident_id
    assert flow.apply(command)
    incident = flow.monitor.incident(question.incident_id)
    assert incident.subject_key is None and incident.situation_assessment == assessment
    assert incident.state is (IncidentState.HELP_REQUIRED if help_needed
                              else IncidentState.RECHECK_REQUIRED)
    assert incident.answer is (VoiceAnswer.HELP if help_needed else VoiceAnswer.UNCLEAR)
    assert incident.close_reason is None
    flow.monitor.drain_events()
    assert flow.apply(command)
    assert not flow.monitor.drain_events()


def test_scene_answer_does_not_resolve_or_answer_another_person_case(handoff):
    flow = handoff
    events = flow.scan()
    scene = next(event for event in events if event.kind == 'question_requested')
    _, scene_result = flow.accept()
    flow.clock.value += 1
    flow.monitor.ingest_rgb(frame(flow.clock()))
    person_id = flow.monitor.candidate(candidate(flow.clock(), subject='person-2'))
    flow.provider.reply = CloudFallReply(VideoAssessment.SUSPECTED_FALL, 'person fixture')
    assert asyncio.run(flow.monitor.run_once())
    flow.relay(flow.monitor.drain_events())
    person_before = replace(flow.monitor.incident(person_id))
    assert len(flow.link.coordinator.requests) == 2
    assert flow.link.client.send_goal_async.call_count == 1

    flow.complete(scene_result)
    command, = flow.commands()
    assert command['incident_id'] == scene.incident_id and flow.apply(command)
    assert flow.monitor.incident(person_id) == person_before
    assert flow.link.request.subject_key == 'person-2'
    assert flow.link.client.send_goal_async.call_count == 2
    assert '특정인을 지목하지 말고' not in flow.link.request.summary
    # The known person's own final result still follows the existing policy.
    _, person_result = flow.accept()
    flow.complete(person_result)
    assert flow.apply(flow.commands()[-1])
    assert flow.monitor.incident(person_id).state is IncidentState.RESOLVED
    assert flow.monitor.incident(scene.incident_id).state is IncidentState.RECHECK_REQUIRED


def test_repeated_scene_handoffs_retry_result_not_question_or_analysis(handoff):
    flow = handoff
    events = flow.scan()
    question = next(event for event in events if event.kind == 'question_requested')
    for _ in range(30):
        flow.relay((question,))
    assert flow.link.client.send_goal_async.call_count == 1
    _, result = flow.accept()
    flow.complete(result, help_needed=True)
    command, = flow.commands()
    assert flow.apply(command)
    flow.monitor.drain_events()
    for _ in range(30):
        flow.relay((question,))
        assert flow.commands()[-1] == command
        assert flow.apply(flow.commands()[-1])
    assert not flow.monitor.drain_events()
    assert len(flow.provider.calls) == flow.link.client.send_goal_async.call_count == 1
    flow.link.agent_presence.send_goal_async.assert_not_called()


def test_scene_label_strengthening_keeps_current_confirmation_without_reasking(handoff):
    flow = handoff
    events = flow.scan()
    first = next(event for event in events if event.kind == 'question_requested')
    old_handle, old_result = flow.accept()
    events = flow.scan(suspected_scene(VideoAssessment.OBSERVED_FALL), stamp=220)
    assert not any(event.kind == 'question_requested' for event in events)
    flow.relay(flow.monitor.pending_questions())
    old_handle.cancel_goal_async.assert_not_called()
    assert flow.link.request.question_id == first.question_id
    assert flow.link.request.revision == first.evidence_revision
    assert flow.link.client.send_goal_async.call_count == 1
    flow.complete(old_result)
    old_command, = flow.commands()
    assert old_command['question_id'] == first.question_id
    assert flow.apply(old_command)
    incident = flow.monitor.incident(first.incident_id)
    assert incident.state is IncidentState.RECHECK_REQUIRED
    assert incident.answer is VoiceAnswer.UNCLEAR
    assert incident.video.assessment is VideoAssessment.OBSERVED_FALL
    assert incident.fall_seen
    assert incident.revision == first.evidence_revision
    assert incident.question_id == first.question_id
    events = flow.monitor.drain_events()
    assert not any(event.kind == 'question_requested' for event in events)
    flow.relay(events)
    flow.relay(flow.monitor.pending_questions())
    assert flow.link.request is None
    assert flow.link.client.send_goal_async.call_count == 1
    # Re-delivery of the original result or finding is not a second dialogue.
    assert flow.apply(old_command)
    assert not flow.monitor.drain_events()
    events = flow.scan(suspected_scene(VideoAssessment.OBSERVED_FALL), stamp=280)
    assert not any(event.kind == 'question_requested' for event in events)
    assert flow.link.client.send_goal_async.call_count == 1


@pytest.mark.parametrize('fault', ['aborted', 'canceled', 'invalid_result', 'manager_lost'])
def test_scene_transport_failure_is_not_a_user_reply(handoff, fault):
    flow = handoff
    events = flow.scan()
    question = next(event for event in events if event.kind == 'question_requested')
    handle, result = flow.accept()
    if fault == 'manager_lost':
        flow.link.client.server_is_ready.return_value = False
        flow.link.tick()
        flow.now = 5.0
        flow.link.tick()
        handle.cancel_goal_async.assert_called_once()
        flow.complete(result)  # A late successful callback cannot revive the request.
    elif fault == 'invalid_result':
        flow.complete(result, result_yaml='{}')
    else:
        flow.complete(result, status=6 if fault == 'aborted' else 5)
    command, = flow.commands()
    assert command['action'] == 'confirmation_failed'
    assert 'help_needed' not in command and 'situation_assessment' not in command
    assert flow.apply(command)
    incident = flow.monitor.incident(question.incident_id)
    assert incident.answer is VoiceAnswer.FAILED and incident.help_needed is None
    assert incident.state is IncidentState.RECHECK_REQUIRED
    assert not any(event.kind == 'notification_requested'
                   for event in flow.monitor.drain_events())


def test_scene_waits_for_managed_confirmation_to_finish(handoff):
    flow = handoff
    flow.link.on_state(manager_state(SimpleNamespace(capability_id='fall_confirmation')))
    flow.scan()
    assert len(flow.link.coordinator.requests) == 1
    flow.link.client.send_goal_async.assert_not_called()
    flow.link.on_state(manager_state())
    flow.link.client.send_goal_async.assert_called_once()
    flow.link.agent_presence.send_goal_async.assert_not_called()


def test_later_normal_scene_does_not_clear_unidentified_case(handoff):
    flow = handoff
    events = flow.scan()
    question = next(event for event in events if event.kind == 'question_requested')
    _, result = flow.accept()
    flow.complete(result)
    assert flow.apply(flow.commands()[0])
    before = replace(flow.monitor.incident(question.incident_id))
    flow.scan(CloudFallReply(VideoAssessment.NORMAL_ACTIVITY, 'normal fixture'), stamp=220)
    assert len(flow.commands()) == 1
    assert flow.monitor.incident(question.incident_id) == before
    assert flow.link.client.send_goal_async.call_count == 1
