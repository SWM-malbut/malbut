#!/usr/bin/env python3
"""Opt-in live story-memory evaluation, using isolated SQLite databases.

Each phase runs in a new interpreter; every probe starts an empty conversation.
Only synthetic case text is sent or logged. Transport observation delegates to
the real production HTTP transport and never records headers or credentials.
Semantic correctness is reviewed against cases.json, not keyword auto-scoring.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parents[1] / 'malbut_agent_server'


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def trace_events(path, request_id=None):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_trace_events'").fetchone():
            return []
        rows = conn.execute('SELECT * FROM memory_trace_events' +
                            (' WHERE request_id=?' if request_id else '') + ' ORDER BY sequence',
                            (request_id,) if request_id else ()).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['data'] = json.loads(item.pop('data_json'))
            result.append(item)
        return result


def reserve_call(output, limit):
    with (output / 'api-call-counter').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        count = int(stream.read() or '0')
        if count >= limit:
            raise RuntimeError('live_api_call_budget_exhausted')
        stream.seek(0)
        stream.truncate()
        stream.write(str(count + 1))
        stream.flush()
        return count + 1


def install_observer(output, case_dir, phase, limit):
    from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
    original = OpenAIResponsesProvider._urllib_transport
    lock = threading.Lock()

    def observed(url, headers, payload, timeout_seconds):
        number = reserve_call(output, limit)
        started = time.time()
        record = {
            'call': number, 'phase': phase, 'pid': os.getpid(), 'started_at': started,
            'model': payload.get('model'), 'input': payload.get('input'),
            'instructions_sha256': hashlib.sha256(payload.get('instructions', '').encode()).hexdigest(),
            'reasoning': payload.get('reasoning'), 'max_output_tokens': payload.get('max_output_tokens'),
        }
        try:
            response = original(url, headers, payload, timeout_seconds)
            record.update(response_model=response.get('model'), response_status=response.get('status'),
                          response_output=[item for item in response.get('output', [])
                                           if item.get('type') != 'reasoning'], usage=response.get('usage'),
                          incomplete_details=response.get('incomplete_details'))
            return response
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            raise
        finally:
            record['elapsed_seconds'] = round(time.time() - started, 3)
            with lock, (case_dir / 'api-calls.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    OpenAIResponsesProvider._urllib_transport = staticmethod(observed)


def snapshot(runtime, user):
    stories = runtime.story_memory.list_stories(user)
    return {'policy': runtime.story_memory.policy(user), 'stories': stories,
            'evidence': {s['story_id']: runtime.story_memory.evidence(user, s['story_id']) for s in stories}}


def run_phase(args, case):
    from malbut_agent_server.config import Settings, load_env_file
    from malbut_agent_server.factory import build_orchestrator
    from malbut_agent_server.schemas import AgentRequest
    output = Path(args.output).resolve()
    case_dir = output / case['id']
    phase = args.phase
    database = case_dir / 'memory.sqlite3'
    env = load_env_file(Path(args.env_file), target={})
    settings = replace(Settings.from_env(env), provider='openai', database_path=str(database),
                       request_timeout_seconds=30, provider_total_timeout_seconds=61,
                       tool_mode='simulation')
    if not settings.openai_api_key:
        raise RuntimeError('configured_api_key_missing')
    install_observer(output, case_dir, phase, args.max_api_calls)
    report = {'case_id': case['id'], 'phase': phase, 'pid': os.getpid(), 'started_at': time.time(),
              'models': {'dialogue': settings.openai_model, 'extraction': settings.openai_summary_model},
              'timeout_seconds': 30, 'total_timeout_seconds': 61, 'actions': []}
    report_path = case_dir / ('phase-%02d.json' % phase)
    runtime = build_orchestrator(settings, http_server=False)
    try:
        for ordinal, action in enumerate(case['phases'][phase]):
            user = 'eval-' + case['id'] + '-' + action.get('user', 'owner')
            entry = {'action': action, 'user_id': user, 'before': snapshot(runtime, user)}
            report['actions'].append(entry)
            try:
                operation = action['op']
                if operation == 'enable':
                    entry['result'] = runtime.story_memory.enable(user, include_history=False)
                elif operation == 'disable':
                    entry['result'] = runtime.story_memory.disable(user)
                elif operation == 'delete':
                    matches = [s for s in runtime.story_memory.list_stories(user)
                               if action['match'] in json.dumps(s, ensure_ascii=False)]
                    if len(matches) != 1:
                        raise RuntimeError('delete_requires_exactly_one_matching_story')
                    entry['deleted_story_id'] = matches[0]['story_id']
                    entry['result'] = runtime.story_memory.forget(user, matches[0]['story_id'], timeout=90)
                elif operation in ('say', 'probe'):
                    request_id = '%s-p%d-a%d' % (case['id'], phase, ordinal)
                    # Seed turns within a phase share a conversation. Probes never do.
                    room = request_id if operation == 'probe' else '%s-p%d-seed' % (case['id'], phase)
                    runtime.conversation_store.create(user, room)
                    entry['request_id'], entry['conversation_id'] = request_id, room
                    started = time.monotonic()
                    result = runtime.handle(AgentRequest.from_dict({
                        'request_id': request_id, 'turn_id': request_id, 'conversation_id': room,
                        'user_id': user, 'utterance': action['text'], 'robot_state': {}, 'available_tools': []}))
                    entry['latency_seconds'] = round(time.monotonic() - started, 3)
                    entry['answer'] = result.decision.message
                    entry['result'] = result.to_dict()
                    entry['extraction_settled'] = runtime.checkpoint_story_memory(user, timeout=100)
                    entry['trace_events'] = trace_events(database, request_id)
                    if operation == 'probe':
                        inputs = [e['data'] for e in entry['trace_events'] if e['event'] == 'model_input']
                        checks = {'actual_model_input_observed': bool(inputs),
                                  'no_recent_history': bool(inputs) and all(i['history_messages'] == 0 and not i['summary_present'] for i in inputs)}
                        count = len(inputs[-1]['stories']) if inputs else 0
                        if action['retrieval'] == 'required':
                            checks['story_in_actual_model_input'] = count > 0
                        elif action['retrieval'] == 'forbidden':
                            checks['no_story_in_actual_model_input'] = bool(inputs) and count == 0
                        if action.get('evidence') == 'required':
                            checks['source_evidence_in_model_input'] = bool(inputs) and inputs[-1]['evidence_count'] > 0
                        entry['structural_checks'] = checks
                        entry['semantic_verdict'] = 'pending_manual_review'
                else:
                    raise ValueError('unsupported_case_operation')
            except Exception as exc:
                # Runtime exception messages may contain provider data; keep only safe type/code.
                entry['error'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None)}
            entry['after'] = snapshot(runtime, user)
            write_json(report_path, report)
            print(json.dumps({'case': case['id'], 'phase': phase, 'operation': action['op'],
                              'probe': action.get('id'), 'error': entry.get('error'),
                              'stories': len(entry['after']['stories']),
                              'pending': entry['after']['policy']['pending'],
                              'failed': entry['after']['policy']['failed']}, ensure_ascii=False), flush=True)
    finally:
        runtime.close()
        report['ended_at'] = time.time()
        report['trace_events'] = trace_events(database)
        write_json(report_path, report)


def run_case(args, case):
    case_dir = Path(args.output).resolve() / case['id']
    if case_dir.exists():
        raise RuntimeError('case_output_already_exists: ' + case['id'])
    case_dir.mkdir()
    for phase in range(len(case['phases'])):
        command = [sys.executable, str(Path(__file__).resolve()), '--live', '--output', args.output,
                   '--env-file', args.env_file, '--case', case['id'], '--phase', str(phase),
                   '--max-api-calls', str(args.max_api_calls)]
        with (case_dir / ('phase-%02d.log' % phase)).open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        print(case['id'], 'phase', phase, 'finished', result.returncode, flush=True)
        if result.returncode:
            return {'id': case['id'], 'completed': False, 'failed_phase': phase}
    return {'id': case['id'], 'completed': True, 'phases': len(case['phases'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly allow paid API requests')
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--case', action='append')
    parser.add_argument('--workers', type=int, default=2, choices=(1, 2))
    parser.add_argument('--phase', type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument('--max-api-calls', type=int, default=160)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; this experiment makes paid API requests')
    cases = json.loads((HERE / 'cases.json').read_text())
    if args.case:
        requested = set(args.case)
        cases = [case for case in cases if case['id'] in requested]
        if {case['id'] for case in cases} != requested:
            parser.error('unknown case id')
    if args.phase is not None:
        if len(cases) != 1:
            parser.error('a child phase requires exactly one case')
        run_phase(args, cases[0])
        return
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'started_at': time.time(), 'cases': [c['id'] for c in cases],
                'scope': 'Production story memory and live OpenAI models; fact memory disabled; synthetic data only',
                'method': 'Independent SQLite database per case; new interpreter per phase; empty history per probe',
                'max_api_calls': args.max_api_calls, 'source_sha256': {}}
    for path in sorted(PACKAGE.rglob('*.py')) + [HERE / 'cases.json', Path(__file__).resolve()]:
        manifest['source_sha256'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    run_id = str(time.time_ns())
    write_json(output / ('manifest-' + run_id + '.json'), manifest)
    completed = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_case, args, case): case['id'] for case in cases}
        for future in as_completed(futures):
            try:
                completed.append(future.result())
            except Exception as exc:
                completed.append({'id': futures[future], 'completed': False, 'error_type': type(exc).__name__})
            write_json(output / ('execution-' + run_id + '.json'), completed)
    print(json.dumps(completed, ensure_ascii=False, indent=2))
    if any(not case['completed'] for case in completed):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
