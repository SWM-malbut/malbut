#!/usr/bin/env python3
"""Causal 84-video replay: immutable cached first calls + real changed-input rechecks.

No robot/web deployment. Dry run uses an explicitly assumed 5-second duration for
new calls. --execute uses measured new API times; prior calls use recorded times.
Video time is virtual, not Jetson wall-clock performance.
"""
import argparse
from collections import Counter, defaultdict
import copy
from dataclasses import asdict
import heapq
import json
import math
import os
from pathlib import Path
import shutil

from diagnose_fall84_json_fences import diagnose
from experimental_fall_recheck import DropConfig, HorizontalDrop, IncidentRechecks, RecheckConfig
from experimental_request_continuity import ContinuityProbe
from fall_evaluation_v2 import score
from finalize_partial_pose_review import verify_arm
from replay_fall84_cloud_pair import MODEL, cloud_identity
from replay_fall_baseline import sha
from replay_pose_retention import verify_overlay
from replay_vlm_frames import api, digest, save
from review_fall_annotations import require
from run_ollama_fall_suite import extract_prefix
import run_free_cloud_fall_suite as cloud


def read(path):
    return json.loads(path.read_text())


def verify_files(root):
    for name, value in read(root/'completed.json')['files'].items():
        p = (root/name).resolve()
        require(p.is_relative_to(root.resolve()) and sha(p) == value, 'changed cached result')


def replay_case(rows, events, meta, invoke):
    """invoke(call) -> (duration, outcome). Scheduling never receives GT/model labels."""
    engine, drop, continuity = IncidentRechecks(), HorizontalDrop(), ContinuityProbe()
    size = meta['width'], meta['height']
    rows_by_frame = {r['frame_index']: r for r in rows}
    aliases, tracks, signals, proposals, calls = {}, {}, [], [], []
    queue, sequence = [], 0

    def push(time, priority, kind, payload):
        nonlocal sequence
        sequence += 1
        heapq.heappush(queue, (time, priority, sequence, kind, payload))

    for frame in range(meta['frames']):
        push(frame/meta['fps'], 0, 'frame', frame)
    for e in events:
        push(e['dispatch_time_s'], 1, 'event', e)

    def ingest(original):
        e = copy.deepcopy(original)
        tid, rid = e['origin']['candidate']['targetTrackId'], e['request_id']
        if e['dispatch_kind'] == 'request':
            p = continuity.route(e)
            proposals.append(p)
            if p['action'] == 'attach_evidence':
                aliases[rid] = p['parent_request_id']
                e['dispatch_kind'] = 'update'
        rid = aliases.get(rid, rid)
        # A new drop on a previously observed track belongs to its existing incident.
        if rid not in engine.incidents and tid in tracks:
            aliases[rid] = rid = tracks[tid]
            e['dispatch_kind'] = 'update'
        e['request_id'] = rid
        tracks[tid] = rid
        engine.ingest(e, stable_head=rid in continuity.roots)

    latest_frame = 0
    while queue:
        now = queue[0][0]
        # All evidence with the same timestamp enters before issuing a snapshot.
        while queue and abs(queue[0][0]-now) < 1e-9:
            _, _, _, kind, payload = heapq.heappop(queue)
            if kind == 'frame':
                latest_frame = payload
                row = rows_by_frame.get(payload)
                if row:
                    continuity.observe(row, size)
                    for c in drop.observe(row, size):
                        e = dict(
                            origin=dict(case_id=row['case_id'], frame_index=payload,
                                        timestamp_s=now, candidate=c),
                            request_id=c['candidateId'], dispatch_kind='request',
                            dispatch_time_s=now, decision_frame_index=payload)
                        signals.append(copy.deepcopy(e))
                        push(now, 2, 'event', e)
            elif kind == 'event':
                ingest(payload)
            elif kind == 'complete':
                engine.complete(payload[0], now, payload[1])
        for call in engine.tick(now, latest_frame, latest_frame/meta['fps']):
            require(call['available_through_frame']/meta['fps'] <= now+1e-9, 'future RGB')
            seconds, outcome = invoke(call)
            require(math.isfinite(seconds) and seconds > 0, 'invalid call duration')
            call.update(request_s=seconds, completed_at_s=now+seconds)
            calls.append(call)
            push(now+seconds, 3, 'complete', (call['call_id'], outcome))
        # When RGB ends, a bounded timer can still use already arrived, newer RGB.
        if latest_frame == meta['frames']-1:
            due = [max(min(p['time'] for p in s['pending'])+engine.config.post_change_s,
                       s['last_call']+engine.config.minimum_interval_s)
                   for s in engine.incidents.values()
                   if s['pending'] and s['inflight'] is None
                   and s['count'] <= engine.config.maximum_rechecks
                   and latest_frame > s['last_frame']]
            if due and min(due) > now+1e-9:
                push(min(due), 4, 'timer', None)
    return dict(calls=calls, signals=signals, continuity=proposals, ledger=engine.ledger,
                unresolved=[dict(incident_id=rid,
                                 pending_reasons=[p['reason'] for p in s['pending']],
                                 review_required=s['review_required'])
                            for rid, s in engine.incidents.items() if s['pending']])


def terminal_predictions(records, labels, cleaned):
    """Latest result per incident; multiple incidents use highest valid concern.

    Not oracle/best-of scoring. A newer failed call is unresolved, not replaced
    by the old normal answer. No call is never a normal prediction.
    """
    latest = {}
    for r in records:
        latest[r['case_id'], r['incident_id']] = r
    output = []
    rank = {'normal_activity': 0, 'suspected_fall': 1, 'observed_fall': 2}
    for cid in labels:
        case = [r['cleaned' if cleaned else 'strict'] for (c, _), r in latest.items() if c == cid]
        if not case:
            output.append(dict(case_id=cid, status='not_triggered', valid=False, prediction=None))
            continue
        if any(not r['valid'] or not r.get('prediction')
               or r['prediction']['label'] is None for r in case):
            output.append(dict(case_id=cid, status='responded', valid=False, prediction=None))
        else:
            best = max(case, key=lambda r: rank[r['prediction']['label']])
            output.append(dict(best, case_id=cid, status='responded'))
    return output


def run(args):
    require(not args.output.exists(), 'output exists; never overwrite or retry silently')
    verify_overlay(args.spatial_final, args.dataset)
    cases = verify_arm(args.pose_run, 'partial_brief')
    verify_files(args.baseline)
    metas = {m['case_id']: m for m in read(args.spatial_final/'media.json')['cases']}
    require(len(metas) == 84, 'requires frozen 84 videos')
    prior = read(args.baseline/'run.json')['contract']
    rows, events = defaultdict(list), defaultdict(list)
    for name, groups in [('frames', rows), ('dispatches', events)]:
        for line in (args.pose_run/'partial_brief'/f'{name}.jsonl').read_text().splitlines():
            r = json.loads(line)
            groups[r['case_id'] if name == 'frames' else r['origin']['case_id']].append(r)
    cache = {}
    for c in cases:
        for n, e in enumerate(c['requests'], 1):
            cache[c['case_id'], e['request_id']] = args.baseline/'calls'/(
                f'{c["case_id"]}-request-{n:02d}')
    args.output.mkdir(mode=0o700)
    (args.output/'records').mkdir()
    (args.output/'cases').mkdir()
    sources = [Path(__file__), Path(__file__).with_name('experimental_fall_recheck.py'),
               Path(__file__).with_name('experimental_request_continuity.py'),
               Path(cloud.__file__), Path(__file__).with_name('fall_evaluation_v2.py'),
               Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1' /
               'HORIZONTAL_DROP_RECHECK_PROTOCOL.md']
    hashes = {str(p): sha(p) for p in sources}
    snapshot = args.output/'code-snapshot'
    snapshot.mkdir()
    for p in sources:
        shutil.copy2(p, snapshot/p.name)
    contract = dict(prior, mode='gated', evaluation_version='v2',
                    version='horizontal-drop-recheck-v1', source_sha256=hashes)
    save(args.output/'plan.json', dict(
        execute=args.execute, drop=asdict(DropConfig()), recheck=asdict(RecheckConfig()),
        source_sha256=hashes, baseline_marker_sha256=sha(args.baseline/'completed.json'),
        spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
        pose_marker_sha256=sha(args.pose_run/'partial_brief/completed.json'),
        clock='virtual video time; cached measured API times, new actual API times '
              'if execute, otherwise assumed 5 seconds',
        policy='Latest per incident, highest concern across incidents; failures unresolved; '
               'no call is not normal. First request inputs only reused by exact hash.'))
    if args.execute:
        cloud.free_account(args)
        require(cloud_identity(args.endpoint)[0] == prior['model'], 'model metadata changed')
        contract['ollama_version'] = api(args.endpoint, '/api/version')['version']
    save(args.output/'run.json', dict(contract=contract))
    records, results = [], []
    new_count, reused_count, active = 0, 0, None
    try:
        for cid, meta in metas.items():
            def invoke(call):
                nonlocal new_count, reused_count, active
                active = f'{cid}-call-{len([r for r in records if r["case_id"] == cid])+1:02d}'
                root = (cache.get((cid, call['initial']['request_id']))
                        if call['sequence'] == 1 else None)
                last = call['available_through_frame']
                if root is not None:
                    source = read(root/f'{cid}.input.json')
                    require(source['available_through_frame'] == last, 'changed first snapshot')
                    images, frames = extract_prefix(args.dataset, meta, last)
                    payload = cloud.cloud_payload(MODEL, images, frames, (last+1)/meta['fps'],
                                                  prior['options'], prior['thinking'], 'v2')
                    require(digest(payload) == source['request_sha256'], 'changed cached input')
                    require(frames == source['frames'], 'JPEG environment changed')
                    reused_count += 1
                elif args.execute:
                    root = args.output/'calls'/active
                    root.mkdir(parents=True, mode=0o700)
                    cloud.invoke(args, root, contract, meta, last,
                                 candidate=dict(dispatch_time_s=call['dispatch_time_s']))
                    new_count += 1
                if root is None:
                    strict = cleaned = dict(valid=False, prediction=None, status='planned')
                    seconds, outcome = 5.0, 'failed'
                else:
                    strict = read(root/f'{cid}.result.json')
                    cleaned = copy.deepcopy(strict)
                    if strict['status'] == 'responded':
                        cleaned.update(diagnose(read(root/f'{cid}.response.json'),
                                                (last+1)/meta['fps'])['assessment'])
                    seconds = strict['request_s']
                    p = cleaned.get('prediction')
                    outcome = ('normal' if p and p['label'] == 'normal_activity' else
                               'needs_check') if cleaned['valid'] else 'failed'
                record = dict(case_id=cid, incident_id=call['incident_id'],
                              call=copy.deepcopy(call), reused=root is not None and
                              root.is_relative_to(args.baseline),
                              source=str(root) if root else None,
                              strict=strict, cleaned=cleaned, request_s=seconds)
                records.append(record)
                # Durable after each call, even if later auth/limit/network failure stops the run.
                save(args.output/'records'/f'{active}.json', record)
                return seconds, outcome

            result = replay_case(rows[cid], events[cid], meta, invoke)
            results.append(dict(case_id=cid, **result))
            save(args.output/'cases'/f'{cid}.json', result)
            if result['signals'] or any(c['mode'] == 'recheck' for c in result['calls']):
                print('CASE', cid, 'new_drop=', len(result['signals']),
                      'calls=', [(c['mode'], round(c['dispatch_time_s'], 3),
                                  c['available_through_frame'])
                                 for c in result['calls']], flush=True)
        # GT is loaded only for reporting, never for detection, scheduling or API payloads.
        labels = {c['case_id']: c for c in
                  read(args.spatial_final/'evaluation_labels.json')['classifications']['cases']}
        summary = dict(videos=len(results), new_api_calls=new_count, cached_calls=reused_count,
                       planned_new_calls=sum(r['source'] is None for r in records),
                       calls=len(records), requested_videos=len({r['case_id'] for r in records}),
                       modes=dict(Counter(r['call']['mode'] for r in records)),
                       drop_signals=[dict(case_id=r['case_id'], event=e)
                                     for r in results for e in r['signals']],
                       unresolved=[dict(case_id=r['case_id'], **s)
                                   for r in results for s in r['unresolved']])
        if args.execute:
            summary.update(strict=score(terminal_predictions(records, labels, False),
                                        labels, 'gated'),
                           outer_fence_only=score(terminal_predictions(records, labels, True),
                                                  labels, 'gated'))
        save(args.output/'results.json', records)
        save(args.output/'replay.json', results)
        save(args.output/'summary.json', summary)
        for p, expected in hashes.items():
            require(sha(Path(p)) == expected, 'source changed during execution')
        save(args.output/'completed.json', dict(
            files={str(p.relative_to(args.output)): sha(p) for p in args.output.rglob('*')
                   if p.is_file()}, new_api_calls=new_count))
        print('COMPLETE', json.dumps({k: summary[k] for k in (
            'videos', 'calls', 'cached_calls', 'new_api_calls', 'planned_new_calls', 'modes')},
                                    ensure_ascii=False), flush=True)
    except BaseException as exc:
        save(args.output/'stopped.json', dict(
            error_type=type(exc).__name__, active_call=active,
            new_api_calls=new_count, cached_calls=reused_count))
        raise


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'spatial-final', 'pose-run', 'baseline', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--endpoint', default='http://127.0.0.1:21434')
    parser.add_argument('--no-paid-balance-confirmed', action='store_true')
    parser.add_argument('--execute', action='store_true')
    run(parser.parse_args())
