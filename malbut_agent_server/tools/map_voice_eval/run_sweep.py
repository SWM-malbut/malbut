"""Live configured LLM + real Agent/Manager, only robot Actions are fake.

No hardware, microphone, production maps or persistent DB. Every scenario has
its own ROS graph and conversation. Expectations are snapshotted before calls.
Supports separate processes/domains for independent shards, never two graphs
in the same process. Reports retain failed expectations without rewriting them.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from malbut_agent_server import factory
from malbut_agent_server.voice_lab import _dialogue_settings
from malbut_agent_server.voice_lab_graph import VoiceLabGraph
from malbut_agent_server.tools import TOOL_SPECS
from malbut_agent_server.prompting import system_instructions_for_tools
from malbut_agent_server.mission_audio_cases import NOTICE_TEXTS
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from catalog import CASES


def drain(graph, seconds=.25):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        graph.spin()


def latest(events):
    return {e['request_id']: e for e in events}


def payload(tool, args):
    if tool == 'request_mapping':
        return 'autoslam', args
    if tool == 'request_patrol':
        return 'patrol', {'thoroughness': {'light': 0, 'normal': 1, 'thorough': 2}[args['thoroughness']]}
    if tool == 'request_relocalization':
        return 'relocalize', {'method': {'auto': 0, 'global_search': 2}[args['method']]}
    if tool == 'request_navigation':
        # Pose values are checked separately against VoiceLabGraph's fixture.
        return 'navigate_to_pose', {}
    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--domain', type=int, default=194)
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--shards', type=int, default=1)
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--repeat', type=int, default=1,
                        help='Repeat each selected conversation independently')
    args = parser.parse_args()
    if args.repeat < 1 or args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error('repeat/shards must be positive and shard must be within shards')
    settings = _dialogue_settings(SimpleNamespace(chat=True, provider=None, model=None, env_file=None))
    observed = []
    provider_calls = []
    payloads = {}
    build_payload = OpenAIResponsesProvider.build_payload

    def capture_payload(self, request, *a, **kw):
        value = build_payload(self, request, *a, **kw)
        # Synthetic lab input only; never retain headers, credentials or settings.
        payloads.setdefault(request.request_id, []).append(deepcopy({
            key: value[key] for key in ('input', 'tool_choice', 'tools', 'text', 'instructions')
            if key in value
        }))
        return value

    OpenAIResponsesProvider.build_payload = capture_payload
    build = factory.build_orchestrator

    def instrumented_build(*a, **kw):
        runtime = build(*a, **kw)
        handle = runtime.handle
        complete = runtime.provider.complete

        def record_provider(request, *a, **kw):
            history = a[1] if len(a) > 1 else kw.get('conversation_turns', [])
            result = complete(request, *a, **kw)
            provider_calls.append(dict(request_id=request.request_id,
                mapping_name_requested=getattr(request, 'mapping_name_requested', False),
                payloads=deepcopy(payloads.pop(request.request_id, [])),
                conversation_input=[dict(user=t.user_content, assistant=t.assistant_content)
                                    for t in history],
                decision=deepcopy(result.decision.to_dict()),
                provider=deepcopy(result.to_dict())))
            return result

        runtime.provider.complete = record_provider

        def record(request):
            result = handle(request)
            observed.append(dict(request_id=request.request_id, utterance=request.utterance,
                raw_decision=deepcopy(result.raw_decision.to_dict()),
                decision=deepcopy(result.decision.to_dict()), safety=result.safety.to_dict(),
                provider=result.provider_result.to_dict()))
            observed[-1]['provider_before_policy'] = deepcopy([
                call for call in provider_calls if call['request_id'] == request.request_id])
            return result

        runtime.handle = record
        return runtime

    factory.build_orchestrator = instrumented_build
    selected = [c for c in CASES if not args.only or c['id'] in args.only]
    if not selected or (args.only and set(args.only) - {c['id'] for c in selected}):
        parser.error('unknown or empty scenario selection')
    repeated = [dict(deepcopy(c), source_id=c['id'], repeat=repeat,
                     id=c['id'] if args.repeat == 1 else f"{c['id']}.repeat-{repeat}")
                for repeat in range(1, args.repeat + 1) for c in selected]
    cases = [c for i, c in enumerate(repeated) if i % args.shards == args.shard]
    prompt = system_instructions_for_tools(list(TOOL_SPECS.values()))
    report = dict(started_at=datetime.now(timezone.utc).isoformat(),
        configured_model=settings.openai_model, reasoning_effort=settings.openai_reasoning_effort,
        method='Live LLM -> real Agent/Manager -> isolated fake Actions; independent conversations',
        observation_note='raw_decision is OrchestrationResult after policy/commit; provider_before_policy is the unmodified provider response',
        prompt_note='prompt_snapshot is a reference using all registered tools; it is not a capture of each complete provider payload',
        harness_revision='v4: explicit mapping state, typed clarification and actual provider payload capture; retains v3 independent repeats/provider/Manager/speech checks',
        domain=args.domain, repeat=args.repeat, expectations=deepcopy(cases), results=[],
        tool_specs={k: v.to_openai_dict() for k, v in TOOL_SPECS.items()},
        prompt_snapshot=prompt, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    destination = Path(args.output)

    def save():
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(destination)

    save()
    for case in cases:
        timeline = []
        graph = VoiceLabGraph(timeline.append, args.domain, dialogue_settings=settings)
        started = time.monotonic()
        current = dict(id=case['id'], source_id=case['source_id'], repeat=case['repeat'],
                       group=case['group'], turns=[])
        try:
            graph.start(manager_enabled=case.get('manager_enabled', True))
            for cap, outcome in case.get('behaviors', {}).items():
                graph.set_behavior(cap, outcome)
            for expected in case['turns']:
                old_model, old_goal, old_event, old_speech = len(observed), len(graph.goals), len(graph.events), len(graph.speech)
                previous_requests = set(latest(graph.events))
                uid = graph.send(expected['text'])
                reply = graph.wait_reply(uid)
                drain(graph)
                # Wait for real transport/terminal result, except deliberately held missions.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    pending = [e for e in latest(graph.events).values() if not e.get('terminal')
                               and (e.get('cancel_requested') or
                                    case.get('behaviors', {}).get(e.get('capability_id')) != 'hold')]
                    if not pending:
                        break
                    graph.spin()
                drain(graph, .1)
                models = deepcopy(observed[old_model:])
                goals = deepcopy(graph.goals[old_goal:])
                events = deepcopy(graph.events[old_event:])
                speech = deepcopy(graph.speech[old_speech:])
                new_events = [e for e in events if e['request_id'] not in previous_requests]
                decision = models[-1]['raw_decision'] if models else {}
                provider = models[-1]['provider'] if models else {}
                provider_calls_for_turn = models[-1].get('provider_before_policy', []) if models else []
                original = provider_calls_for_turn[-1]['decision'] if provider_calls_for_turn else {}
                original_tool = original.get('tool_name') if original.get('type') == 'tool_call' else None
                actual = decision.get('tool_name') if decision.get('type') == 'tool_call' else None
                allowed = expected['tool'] if isinstance(expected['tool'], list) else [expected['tool']]
                checks = dict(live_model_response=provider.get('provider') == 'openai' and bool(provider.get('response_id')),
                    non_error_reply=reply.get('kind') != 'error')
                if expected.get('no_dispatch'):
                    # Invalid-name handling may clarify or be blocked by server validation.
                    checks['no_mission'] = not goals and not new_events
                else:
                    checks['tool'] = actual in allowed
                    checks['provider_tool'] = bool(provider_calls_for_turn) and original_tool in allowed
                    if expected['tool'] is None or isinstance(expected['tool'], list):
                        checks['no_mission'] = not goals and not new_events
                    else:
                        checks['arguments'] = decision.get('arguments') == expected['arguments']
                        checks['provider_arguments'] = original.get('arguments') == expected['arguments']
                        capability, arguments = payload(expected['tool'], expected['arguments'])
                        if capability:
                            if expected.get('manager_off') or expected.get('manager_rejected'):
                                checks['no_action_goal'] = not goals
                                checks['manager_failure'] = any(e.get('terminal') and e['state'] != 'SUCCEEDED' for e in new_events)
                            else:
                                checks['action_goal'] = len(goals) == 1 and goals[0]['capability_id'] == capability and all(
                                    goals[0]['arguments'].get(k) == v for k, v in arguments.items())
                                if capability == 'navigate_to_pose':
                                    fixture = {'거실': (1., 1.), '주방': (2., 1.), '현관': (1., 2.)}
                                    x, y = fixture[expected['arguments']['location']]
                                    pose = goals[0]['arguments'].get('pose', {}) if goals else {}
                                    checks['registered_pose'] = (
                                        pose.get('header', {}).get('frame_id') == 'map'
                                        and pose.get('pose', {}).get('position') == {'x': x, 'y': y, 'z': 0.}
                                        and pose.get('pose', {}).get('orientation') == {'x': 0., 'y': 0., 'z': 0., 'w': 1.})
                                checks['manager_received'] = any(e.get('capability_id') == capability for e in new_events)
                                if expected.get('terminal'):
                                    checks['terminal'] = any(e.get('terminal') and e['state'] in expected['terminal'] for e in new_events)
                                    checks['failure_notice'] = any(s['text'] == NOTICE_TEXTS['operation.failed'] for s in speech)
                                    checks['no_false_success'] = not any(s['text'] == NOTICE_TEXTS['operation.succeeded'] for s in speech)
                                elif case.get('behaviors', {}).get(capability) != 'hold':
                                    checks['terminal'] = any(e.get('terminal') and e['state'] == 'SUCCEEDED' for e in new_events)
                        elif expected.get('canceled'):
                            checks['cancellation'] = not goals and any(e['kind'] == 'canceled' for e in events)
                if expected.get('reply_pattern'):
                    checks['reply_content'] = bool(re.search(expected['reply_pattern'], reply.get('text', '')))
                if 'mapping_pending' in expected:
                    checks['mapping_pending'] = bool(provider_calls_for_turn) and (
                        provider_calls_for_turn[-1]['mapping_name_requested'] == expected['mapping_pending'])
                if 'clarification_for' in expected:
                    checks['clarification_for'] = original.get('clarification_for') == expected['clarification_for']
                if 'forced_mapping' in expected:
                    actual_payloads = provider_calls_for_turn[-1].get('payloads', []) if provider_calls_for_turn else []
                    forced = bool(actual_payloads) and actual_payloads[-1].get('tool_choice') == {
                        'type': 'function', 'name': 'request_mapping'}
                    checks['forced_mapping'] = bool(actual_payloads) and forced == expected['forced_mapping']
                for text, count in expected.get('speech_counts', {}).items():
                    checks['speech_count:' + text] = sum(s['text'] == text for s in speech) == count
                if expected.get('remains_running'):
                    checks['remains_running'] = any(e['capability_id'] == expected['remains_running'] and not e.get('terminal')
                                                   for e in latest(graph.events).values())
                if expected.get('preempted'):
                    checks['preempted'] = any(e['capability_id'] == expected['preempted'] and e['kind'] == 'failed'
                        and 'preempted' in e.get('reason', '') for e in events)
                    canceled = [e for e in timeline if e.get('kind') == 'actuator_terminal'
                        and e.get('capability_id') == expected['preempted'] and e.get('status') == 'canceled']
                    checks['cancel_before_replacement'] = bool(canceled and goals
                        and canceled[-1]['observed_at'] <= goals[0]['received_at'])
                status = 'pass' if all(checks.values()) else 'fail'
                current['turns'].append(dict(expected=expected, status=status, checks=checks, reply=reply,
                    model=models, goals=goals, events=events, speech=speech))
                print(json.dumps(dict(case=case['id'], text=expected['text'], status=status, tool=actual,
                    arguments=decision.get('arguments'), reply=reply.get('text'),
                    failed=[k for k, v in checks.items() if not v]), ensure_ascii=False), flush=True)
        except Exception as error:
            current['error'] = type(error).__name__
            print(json.dumps(dict(case=case['id'], error=type(error).__name__)), flush=True)
        finally:
            graph.close()
            current['elapsed_s'] = round(time.monotonic() - started, 3)
            current['timeline'] = deepcopy(timeline)
            report['results'].append(current)
            save()
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    if any('error' in case for case in report['results']):
        raise SystemExit(2)
    if any(turn['status'] == 'fail' for case in report['results'] for turn in case['turns']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
