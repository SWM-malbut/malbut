#!/usr/bin/env python3
"""84-video v2 benchmark with a 1x replay clock and fresh Pose-gated calls.

Offline Linux CPU Pose + Mac Ollama timing, NOT robot/ROS performance. Labels
are read only for post-call scoring. No paid calls, downloads, retries or tuning.
"""
import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import run_ollama_fall_suite as suite
from stream_pose_candidates import iter_rows, wait_until
from replay_fall_baseline import sample_frames, sha, verify_freeze
from review_fall_annotations import require
from fall_evaluation_v2 import score, report_lines, PREDICTION_JSON_SCHEMA


PROTOCOL = Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1/FALL84_RUN_PROTOCOL.md'


def wire_schema():
    """Ollama grammar repetition limit rejects maxLength=4000; enforce it locally."""
    schema = copy.deepcopy(PREDICTION_JSON_SCHEMA)
    del schema['properties']['explanation_ko']['maxLength']
    return schema


def first_call(candidates, already_called):
    """First trigger frame, regardless of GT/person confidence; subsequent evidence stays in log."""
    return bool(candidates) and not already_called


def compare_row(actual, expected):
    # The former subprocess interface compared JSON on both sides. The in-process
    # generator still has dataclass tuples: normalize only JSON representation,
    # never round numerical evidence or relax matching tolerances.
    actual = json.loads(json.dumps(actual, allow_nan=False))
    require((actual['case_id'], actual['frame_index']) ==
            (expected['case_id'], expected['frame_index']), 'fresh Pose frame order differs')
    for key in ('observations',):
        require(suite.normalize_ids(actual[key]) == suite.normalize_ids(expected[key]), 'fresh Pose values differ')
    require(suite.normalize_ids(actual['fall_analysis']['candidates']) ==
            suite.normalize_ids(expected['fall_analysis']['candidates']), 'fresh Pose candidates differ')


def validate_reference(args, media):
    complete = json.loads((args.reference/'completed.json').read_text())
    for name, key in (('run.json', 'run_sha256'), ('frames.jsonl', 'frames_sha256')):
        require(sha(args.reference/name) == complete[key], 'reference changed: ' + name)
    config = json.loads((args.reference/'run.json').read_text())
    require(config['freeze_sha256'] == sha(args.frozen/'freeze.json')
            and config['media_sha256'] == sha(args.frozen/'media.json'), 'wrong reference dataset')
    rows = [json.loads(line) for line in (args.reference/'frames.jsonl').read_text().splitlines()]
    expected = [(m['case_id'], f) for m in media for f in sample_frames(m['frames'], m['fps'], 5)]
    require([(r['case_id'], r['frame_index']) for r in rows] == expected, 'reference coverage/order mismatch')
    return rows


def ledger(replay, result, mode):
    start = replay['replay_start_s']
    value = dict(replay, mode=mode, scope='1x replay benchmark; identity association not yet audited',
                 result_status=result['status'], valid_result=bool(result.get('valid')),
                 realtime_robot_claim=False)
    # All timestamps originate on the same Linux controller clock, not Ollama's host clock.
    for key in ('request_started_monotonic_s', 'response_received_monotonic_s',
                'validation_finished_monotonic_s', 'request_finished_monotonic_s'):
        if key in result:
            require(result[key] >= start, 'invalid mixed replay clock')
            value[key] = result[key]
    ready = result.get('validation_finished_monotonic_s')
    value['valid_result_after_replay_start_s'] = ready-start if ready and result.get('valid') else None
    value['outcome_label'] = (result.get('prediction') or {}).get('label') if result.get('valid') else None
    return value


def write_summary(args):
    """Only completed modes get accuracy; partial cases never masquerade as full evaluation."""
    labels = {c['case_id']: c for c in json.loads((args.frozen/'evaluation_labels.json').read_text())
              ['classifications']['cases']}
    measurements, summary = [], {}
    root = args.output/args.model.replace(':', '--')
    for mode in ('gated', 'full'):
        out = root/mode
        if not (out/'completed.json').exists():
            summary[mode] = dict(status='incomplete_or_not_started',
                                 saved_cases=len(list(out.glob('SYN*.result.json'))))
            continue
        suite.verify_completed(out)
        rows = [json.loads(p.read_text()) for p in sorted(out.glob('SYN*.result.json'))]
        require({r['case_id'] for r in rows} == set(labels), 'summary coverage mismatch')
        result = score(rows, labels, mode)
        summary[mode] = dict(status='completed', **result)
        measurements.append((args.model, mode, result))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    suite.save(args.output/f'report-{stamp}.json', summary)
    lines = ['# 84개 평가 · Gemma 우선', '', *report_lines(measurements),
             '## 시간 해석', '',
             '요청 왕복 시간과 1배속 재생 시계는 실제로 측정했다. 전체 영상을 본 full의 응답 시간은',
             '온라인 최초 감지 지연이 아니다. 다른 사람/배경 후보를 제외하는 대상 연결 검토 전에는',
             '낙상 대상의 감지 지연 중앙값·p95를 확정하지 않는다. 원본 시간 기록은 각 timing.json에 있다.',
             'Jetson·ROS·주행·음성과 동시 실행한 결과가 아니다.', '']
    with (args.output/f'report-{stamp}.md').open('x') as f:
        f.write('\n'.join(lines))
    return summary


def run_mode(args, model, details, mode, media, reference):
    out = args.output/args.model.replace(':', '--')/mode
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    contract = suite.mode_contract(args, model, details, mode)
    contract.update(replay_protocol='fall84-1x-first-trigger-v1',
                    wire_schema=wire_schema(), wire_schema_sha256=suite.digest(wire_schema()),
                    wire_schema_note='Only explanation maxLength omitted on transport; '
                                     'full schema remains in prompt and strict client validation.',
                    multiple_candidates='one_call_first_trigger_frame; later requests logged, no extra call',
                    source_sha256=dict(contract['source_sha256'], **{
                        str(Path(__file__)): sha(Path(__file__)), str(PROTOCOL): sha(PROTOCOL)}),
                    timing='same Linux monotonic clock; 1x replay, no frame drops, '
                           'synchronous VLM may queue later frames; new clip starts after previous finishes',
                    model_host='Mac Ollama through SSH loopback; not Jetson')
    suite.save(out/'run.json', dict(contract=contract, contract_sha256=suite.digest(contract)))
    metas = {m['case_id']: m for m in media}

    def invoke(meta, frame, replay, candidates=None, pose_ms=None):
        # VLM receives only RGB prefix. Candidate metadata is stored privately, not sent.
        candidate = candidates[0] if candidates else None
        suite.call_case(args, out, contract, meta, frame, candidate=candidate, pose_ms=pose_ms)
        result = json.loads((out/f'{meta["case_id"]}.result.json').read_text())
        evidence = ledger(replay, result, mode)
        evidence['all_candidates_at_first_trigger'] = candidates or []
        suite.save(out/f'{meta["case_id"]}.timing.json', evidence)

    if mode == 'full':
        for meta in media:
            start = time.monotonic()
            last = meta['frames']-1
            due = start+last/meta['fps']
            wait_until(due)
            invoke(meta, last, dict(clock='Linux controller time.monotonic', replay_start_s=start,
                                    input_last_frame=last, input_last_pts_s=last/meta['fps'],
                                    clip_last_frame_due_s=due, observation_ready_s=time.monotonic()))
    else:
        called, seen, n = set(), set(), 0
        counts = Counter()
        worker_args = SimpleNamespace(dataset=args.dataset, frozen=args.frozen, reference=args.reference,
                                      model=args.pose_model, realtime=True)
        generator = iter_rows(worker_args)
        with (out/'pose-frames.jsonl').open('x', buffering=1) as stream:
            try:
                for row in generator:
                    require(n < len(reference), 'extra Pose frame')
                    compare_row(row, reference[n])
                    cid = row['case_id']
                    n += 1
                    seen.add(cid)
                    counts[cid] += row['pipeline_ms']
                    stream.write(json.dumps(row, allow_nan=False)+'\n')
                    candidates = row['fall_analysis']['candidates']
                    if first_call(candidates, cid in called):
                        called.add(cid)
                        invoke(metas[cid], row['frame_index'], dict(row['replay_timing'],
                               input_last_frame=row['frame_index'], input_last_pts_s=row['timestamp_s']),
                               candidates, counts[cid])
                require(n == len(reference) and seen == set(metas), 'incomplete fresh Pose replay')
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                generator.close()
        for cid in sorted(seen-called):
            suite.save(out/f'{cid}.result.json', dict(case_id=cid, status='not_triggered', valid=False,
                         prediction=None, contract_sha256=suite.digest(contract), pose_compute_ms=counts[cid]))
    require(suite.local_model(args.endpoint, model['name']) == model, 'model changed during evaluation')
    for path, h in contract['source_sha256'].items():
        require(sha(Path(path)) == h, 'source changed during evaluation: '+path)
    require(len(list(out.glob('SYN*.result.json'))) == 84, 'results missing')
    suite.save(out/'completed.json', dict(cases=84, files={p.name: sha(p) for p in out.iterdir() if p.is_file()}))
    write_summary(args)


def run(args):
    require(args.model == 'gemma4:12b', 'this first-stage run is scoped to Gemma 4 12B')
    verify_freeze(args.frozen)
    suite.check_evaluation_labels(args.frozen, 'v2')
    args.evaluation_version = 'v2'
    require(args.timeout == 300, 'run protocol timeout must be explicit 300 s; no retries')
    media = json.loads((args.frozen/'media.json').read_text())['cases']
    require(len(media) == 84, '84-video set required')
    reference = validate_reference(args, media)
    catalog = json.loads(args.catalog.read_text())
    pinned = next(m for m in catalog['models'] if m['name'] == args.model)
    model = suite.local_model(args.endpoint, args.model)
    require(model['digest'] == pinned['manifest_sha256'], 'model weights differ from pinned prior run')
    details = suite.api(args.endpoint, '/api/show', dict(model=args.model))
    args.output.mkdir(mode=0o700, exist_ok=False)
    suite.save(args.output/'started.json', dict(created_utc=datetime.now(timezone.utc).isoformat(),
               model=model, freeze_sha256=sha(args.frozen/'freeze.json'), protocol_sha256=sha(PROTOCOL),
               script_sha256=sha(Path(__file__)), timeout_s=args.timeout, retries=0,
               order=['gated', 'full'], paid_calls=False))
    try:
        for mode in ('gated', 'full'):
            run_mode(args, model, details, mode, media, reference)
        verify_freeze(args.frozen)
        suite.save(args.output/'completed.json', dict(model=args.model, modes=['gated', 'full'],
                   created_utc=datetime.now(timezone.utc).isoformat(), cases=84))
        print('ALL_MODES_COMPLETED', args.output, flush=True)
    except BaseException as error:
        suite.save(args.output/'stopped.json', dict(error_type=type(error).__name__, reason=str(error)[:500],
                   no_automatic_retry=True, created_utc=datetime.now(timezone.utc).isoformat()))
        write_summary(args)
        raise
    finally:
        # Unload only the model this run owns; do not delete weights or restart Ollama.
        try:
            suite.api(args.endpoint, '/api/generate', dict(model=args.model, keep_alive=0), timeout=30)
        except (OSError, ValueError):
            pass


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('frozen', 'dataset', 'reference', 'pose-model', 'pose-python', 'catalog', 'output'):
        parser.add_argument('--'+key, type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    parser.add_argument('--timeout', type=float, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
