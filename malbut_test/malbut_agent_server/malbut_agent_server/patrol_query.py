"""Read-only retrieval of the robot owner's durable visual observations."""

import copy
from dataclasses import replace
import logging
import os
import sqlite3

from malbut_agent_server.gateway import CapabilityRegistry, PROPOSAL_ONLY, ToolCapability
from malbut_agent_server.providers.base import accepts_memory_context
from malbut_agent_server.schemas import AgentDecision, ProviderUsage
from malbut_agent_server.tools import PATROL_QUERY_TOOLS, TOOL_SPECS


def configure_patrol_queries(runtime, store, worker=None):
    """Advertise retrieval only when trusted local storage is configured."""
    previous = getattr(runtime, 'patrol_observation_worker', None)
    if previous is not None and previous is not worker:
        previous.close()
    runtime.patrol_memory = store
    runtime.patrol_observation_worker = worker
    runtime.patrol_query_tools = PATROL_QUERY_TOOLS if store is not None else ()
    registry = runtime.capability_registry
    entries = []
    for name in TOOL_SPECS:
        entry = registry.get(name)
        if name in PATROL_QUERY_TOOLS:
            entry = ToolCapability(name, mode=PROPOSAL_ONLY, available=store is not None,
                                   timeout_seconds=5.0)
        if entry is not None:
            entries.append(entry)
    runtime.capability_registry = CapabilityRegistry(
        entries, runtime_mode=registry.runtime_mode, revision=registry.revision)


def configure_patrol_from_settings(runtime, settings, *, http_server):
    """Start image analysis with an explicitly separate simulation dataset when selected."""
    configure_patrol_queries(runtime, None)
    if settings.provider != 'openai' or os.environ.get('MALBUT_PATROL_OBSERVATIONS', '1') != '1':
        return
    from malbut_agent_server.patrol_memory import PatrolMemoryStore, observation_directory

    directory = observation_directory()
    if http_server and not (directory / 'observations.sqlite3').is_file():
        return
    try:
        store = PatrolMemoryStore(
            directory, settings.user_id,
            simulation=os.environ.get('MALBUT_PATROL_SIMULATED', '0') == '1')
        worker = None
        if not http_server:
            from malbut_agent_server.factory import openai_key_for
            from malbut_agent_server.patrol_observer import (
                OpenAIPatrolObserver, PatrolObservationWorker,
            )

            observer = OpenAIPatrolObserver(
                openai_key_for(settings),
                os.environ.get('MALBUT_PATROL_VISION_MODEL') or settings.openai_model,
                settings.openai_base_url)
            worker = PatrolObservationWorker(store, observer)
        configure_patrol_queries(runtime, store, worker)
    except (ValueError, OSError, sqlite3.Error):
        logging.getLogger(__name__).warning('Patrol observation memory unavailable')


def _answer_evidence(result):
    """Keep camera provenance in storage; expose only measured object regions to answers."""
    evidence = copy.deepcopy(result)
    evidence['location_semantics'] = 'object_regions_only_camera_location_withheld'
    room_names = {name for o in result['observations'] for name in o.get('capture_rooms', [])}
    room_names.update(r['name'] for o in result['observations']
                      for r in o.get('capture_regions', []))
    room_names.update(s['region']['name'] for o in result['observations']
                      for obj in o['objects'] for s in [obj.get('spatial', {})]
                      if s.get('status') == 'located' and s.get('region'))
    for observation in evidence['observations']:
        for key in ('capture_rooms', 'capture_regions', 'camera_pose'):
            observation.pop(key, None)
        unknown = False
        for item in observation['objects']:
            for key in ('image_bbox', 'image_anchors'):
                item.pop(key, None)
            spatial = item.get('spatial', {})
            if spatial.get('status') != 'located':
                unknown = True
                item['spatial'] = {'status': 'unknown', 'region': None,
                                   'reason': spatial.get('reason', 'legacy_without_depth')}
                # Free-form scene descriptions must not supply an unmeasured room.
                location = item.pop('location', None)
                if location and not any(name in location for name in room_names):
                    item['image_location'] = location
        if unknown:
            observation.pop('summary', None)
            observation.pop('uncertainties', None)
    return evidence


def answer_patrol(runtime, request, memories, turns, summary, first_result, *, memory_context):
    """Query by the server-bound user, then answer with no execution or memory proposal."""
    store = getattr(runtime, 'patrol_memory', None)
    try:
        if store is None or not accepts_memory_context(runtime.provider):
            raise ValueError('patrol observations unavailable')
        result = store.search(request.user_id, first_result.decision.arguments,
                              include_recent_context=True)
    except Exception:
        return replace(first_result, decision=AgentDecision(
            'message', '순찰 관찰 기록을 조회하지 못했어요.', reason='patrol_query_unavailable'),
            memory_proposal=None)
    if not result['observations']:
        counts = result['counts']
        message = '질문에 맞는 순찰 사진 관찰 기록을 찾지 못했어요.'
        if counts.get('pending'):
            message += ' 아직 분석을 기다리는 사진이 있어요.'
        if counts.get('failed'):
            message += ' 분석하지 못한 사진도 있어요.'
        return replace(first_result, decision=AgentDecision(
            'message', message, reason='patrol_observation_not_found'), memory_proposal=None)
    context = copy.deepcopy(memory_context)
    context['patrol_observations_untrusted'] = _answer_evidence(result)
    value = request.to_dict()
    value['available_tools'] = []
    answer = runtime.provider.complete(
        type(request).from_dict(value), list(memories), copy.deepcopy(list(turns)), [],
        conversation_summary=copy.deepcopy(summary), memory_context=context)
    answer.validate()
    if answer.decision.type == 'tool_call' or answer.memory_proposal is not None:
        return replace(first_result, decision=AgentDecision(
            'refusal', '순찰 관찰 기록으로 답변을 만들지 못했어요.',
            reason='patrol_followup_tool_forbidden'), memory_proposal=None)
    return replace(answer, latency_ms=first_result.latency_ms + answer.latency_ms,
                   usage=ProviderUsage(**{
                       name: (getattr(first_result.usage, name) + getattr(answer.usage, name)
                              if getattr(first_result.usage, name) is not None
                              and getattr(answer.usage, name) is not None else None)
                       for name in ('input_tokens', 'output_tokens', 'total_tokens')}))
