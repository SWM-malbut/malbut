"""Bounded camera answers and installed documentation; never execute robot actions."""

from dataclasses import replace
from importlib.resources import files
import json
import re
import time

from malbut_agent_server.gateway import CapabilityRegistry, PROPOSAL_ONLY, ToolCapability
from malbut_agent_server.schemas import AgentDecision
from malbut_agent_server.tools import INFORMATION_QUERY_TOOLS, TOOL_SPECS, validate_tool_arguments


class RobotGuide:
    """Search a packaged, reviewed guide; never recursively index private files."""

    def __init__(self, entries=None):
        self.entries = (entries if entries is not None else json.loads(
            files('malbut_agent_server').joinpath('data/robot_guide.json').read_text()))

    def search(self, query):
        # Korean character bigrams retain matches across particles/spacing.
        def grams(text):
            text = re.sub(r'[^가-힣a-z0-9]', '', text.lower())
            return {text[i:i+2] for i in range(len(text)-1)}
        wanted = grams(query)
        ranked = []
        for entry in self.entries:
            score = (3 * len(wanted & grams(entry['title']))
                     + len(wanted & grams(' '.join(entry['keywords']))))
            if entry['title'].replace(' ', '') in query.replace(' ', ''):
                score += 4
            if score:
                ranked.append((score, entry))
        ranked.sort(key=lambda item: -item[0])
        # Shared words such as "순찰" also match camera-history documentation.
        # Answer the best matching topic instead of appending a related topic
        # that the user did not ask about.
        return [entry for _, entry in ranked[:1]]

    def answer(self, query):
        found = self.search(query)
        if not found:
            return '설치된 사용 설명서에서 관련 내용을 찾지 못했어요.'
        return '\n'.join(entry['answer'] for entry in found)


def configure_information_queries(runtime, *, guide=None, camera=None):
    runtime.robot_guide = guide
    runtime.camera_view = camera
    enabled = {'search_robot_docs': guide is not None, 'describe_camera_view': camera is not None}
    runtime.information_query_tools = tuple(name for name, yes in enabled.items() if yes)
    registry = runtime.capability_registry
    entries = []
    for name in TOOL_SPECS:
        entry = registry.get(name)
        if name in INFORMATION_QUERY_TOOLS:
            entry = ToolCapability(name, mode=PROPOSAL_ONLY, available=enabled[name], timeout_seconds=10)
        if entry is not None:
            entries.append(entry)
    runtime.capability_registry = CapabilityRegistry(
        entries, runtime_mode=registry.runtime_mode, revision=registry.revision)


def answer_information(runtime, request, first_result):
    started = time.monotonic()
    try:
        name = first_result.decision.tool_name
        args = validate_tool_arguments(name, first_result.decision.arguments)
        if name == 'describe_camera_view':
            if request.robot_state.privacy_mode or runtime.camera_view is None:
                raise ValueError('camera unavailable')
            message = runtime.camera_view.answer(args['question'])
        else:
            message = runtime.robot_guide.answer(args['query'])
        if not isinstance(message, str) or not message.strip() or len(message) > 3000:
            raise ValueError('invalid answer')
        reason = 'information_answer'
    except Exception:
        message = ('지금 카메라 화면을 확인하지 못했어요.'
                   if first_result.decision.tool_name == 'describe_camera_view'
                   else '사용 설명서를 조회하지 못했어요.')
        reason = 'information_unavailable'
    return replace(first_result, decision=AgentDecision('message', message, reason=reason),
                   memory_proposal=None,
                   latency_ms=first_result.latency_ms + (time.monotonic()-started)*1000)
