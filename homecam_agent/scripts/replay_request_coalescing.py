#!/usr/bin/env python3
"""Three-arm replay: immutable candidate evidence, separate dispatch timestamps."""
import argparse
from collections import Counter, defaultdict
import copy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from experimental_request_coalescing import CoalescingConfig, RequestCoalescer
from experimental_request_dedup import RequestDedupExperiment
from replay_fall_baseline import AGENT, sha, write_json
from replay_pose_retention import verify_overlay, timing_stats
from replay_pose_stability import compare, read_run, semantic
from replay_request_dedup import payloads
from review_fall_annotations import require
from score_fall_baseline import anchors, events, validate_rows


MODES = dict(control=None, geometry_gap=replace(CoalescingConfig(), pending_enabled=False),
             bounded_pending=CoalescingConfig())


def unpack(row):
    """Restore exactly the candidates entering the most recent dedup pass."""
    out = copy.deepcopy(row)
    out['fall_analysis'] = copy.deepcopy(row['retention_upstream_analysis'])
    out['fall_analysis']['candidates'].extend(copy.deepcopy(row['retention_raw_candidates']))
    out['verification_updates'] = copy.deepcopy(row['retention_upstream_updates'])
    return out


def origins(row):
    items = [(c, None) for c in row['fall_analysis']['candidates']]
    items.extend((u['evidence'], u) for u in row['verification_updates'])
    return [dict(case_id=row['case_id'], frame_index=row['frame_index'],
                 timestamp_s=row['timestamp_s'], item_index=i,
                 candidate=copy.deepcopy(c), original_update=copy.deepcopy(u))
            for i, (c, u) in enumerate(items)]


def key(origin):
    return origin['case_id'], origin['frame_index'], origin['item_index']


def control_dispatch(row, engine, size):
    requests, updates, details = engine.update(row, image_size=size)
    available = origins(row)
    output = []
    for candidate, update in [(c, None) for c in requests]+[(u['evidence'], u) for u in updates]:
        original = next(o for o in available if o['candidate'] == candidate)
        available.remove(original)
        output.append(dict(origin=original, dispatch_kind='request' if update is None else 'update',
                           request_id=(candidate['candidateId'] if update is None
                                       else update['requestCandidateId']), update=update,
                           dispatch_time_s=row['timestamp_s'],
                           decision_frame_index=row['frame_index'],
                           delay_sec=0.0, reason='control_immediate'))
    require(not available, 'control evidence lost')
    return output, details


def replay(rows, metas, config, emit):
    """GT is not an input. Deadline callbacks run before later video frames."""
    engine, current, last_time = None, None, None
    diagnostics = []
    for saved in rows:
        row = unpack(saved)
        cid, stamp = row['case_id'], row['timestamp_s']
        if cid != current:
            if engine is not None and config is not None:
                for event in engine.flush(last_time):
                    emit(event)
            engine = RequestDedupExperiment() if config is None else RequestCoalescer(config)
            current = cid
        size = metas[cid]['width'], metas[cid]['height']
        start = time.perf_counter()
        if config is None:
            output, details = control_dispatch(row, engine, size)
            routes, pending = dict(engine.routes), []
        else:
            while engine.pending and min(i['deadline'] for i in engine.pending) <= stamp+1e-9:
                for event in engine.poll(min(i['deadline'] for i in engine.pending)):
                    emit(event)
            output = engine.update(row, image_size=size, now_s=stamp)
            details, routes = engine.details, dict(engine.tracker.routes)
            pending = [dict(origin_key=list(key(i['origin'])), deadline_s=i['deadline'])
                       for i in engine.pending]
        for event in output:
            emit(event)
        diagnostics.append(dict(case_id=cid, frame_index=row['frame_index'],
                                timestamp_s=stamp, details=details, routes=routes, pending=pending,
                                processing_ms=(time.perf_counter()-start)*1000))
        last_time = stamp
    if engine is not None and config is not None:
        for event in engine.flush(last_time):
            emit(event)
    return diagnostics


def source_aligned(rows, dispatches, diagnostics):
    """Scoring view ONLY: real dispatch chronology is in dispatches.jsonl.

    Restore requests to their evidence frame to validate original references.
    This view must never be consumed as a realtime trace or used to hide delay.
    """
    output = copy.deepcopy(rows)
    lookup = {(r['case_id'], r['frame_index']): r for r in output}
    expected = {key(o): o for r in rows for o in origins(unpack(r))}
    seen = set()
    for r, diagnostic in zip(output, diagnostics):
        r['fall_analysis']['candidates'] = []
        r['verification_updates'] = []
        r['verification_routes'] = diagnostic['routes']
        r['scoring_view'] = 'source-aligned evidence, NOT dispatch chronology'
    for event in dispatches:
        origin = event['origin']
        identity = key(origin)
        require(identity not in seen and expected.get(identity) == origin,
                'duplicate, changed or unknown evidence')
        seen.add(identity)
        r = lookup[identity[:2]]
        if event['dispatch_kind'] == 'request':
            r['fall_analysis']['candidates'].append(copy.deepcopy(origin['candidate']))
        else:
            require(event['update']['evidence'] == origin['candidate'], 'changed update payload')
            r['verification_updates'].append(copy.deepcopy(event['update']))
    require(seen == set(expected), 'undispatched evidence at end of replay')
    for before, after in zip(rows, output):
        raw = unpack(before)
        require(payloads(raw['fall_analysis']['candidates'], raw['verification_updates'])
                == payloads(after['fall_analysis']['candidates'], after['verification_updates']),
                'source-frame evidence changed')
        require(before['observations'] == after['observations'], 'Pose/IDs changed')
    return output


def audit(rows, dispatches, stage, metas, match):
    bundle = json.loads((stage/'evaluation_labels.json').read_text())
    labels = {c['case_id']: c for c in bundle['classifications']['cases']}
    annotations = {c['case_id']: c for c in bundle['annotations']['cases']}
    cross, by_case, exact = [], defaultdict(list), {}
    for r in rows:
        by_case[r['case_id']].append(r)
    for event in dispatches:
        proof = (event.get('update') or {}).get('deduplication')
        if not proof:
            continue
        cid = event['origin']['case_id']
        if cid not in exact:
            exact[cid] = anchors(annotations[cid], by_case[cid], metas[cid], match)
        checks = []
        for sample in proof['actualSamples']:
            bindings = {a['track_id']: a['person_id'] for a in exact[cid]
                        if a['frame_index'] == sample['frameIndex'] and a['status'] == 'matched'}
            people = [bindings.get(tid) for tid in proof['trackIds']]
            checks.append(dict(frame_index=sample['frameIndex'], person_ids=people,
                               known_different_people=all(people) and len(set(people)) > 1))
        cross.append(dict(event=event, review_case_id=labels[cid].get('review_case_id'),
                          original_source=labels[cid].get('original_source_path'),
                          exact_frame_checks=checks,
                          limitation='unmatched/sparse GT is unknown, not proof of same person'))
    multi = [dict(case_id=cid, review_case_id=labels[cid].get('review_case_id'),
                  original_source=labels[cid].get('original_source_path'),
                  annotated_people=len(c['persons']),
                  requests=[dict(frame=e['origin']['frame_index'],
                                 track=e['origin']['candidate']['targetTrackId'])
                            for e in dispatches if e['origin']['case_id'] == cid
                            and e['dispatch_kind'] == 'request'],
                  cross_track_updates=sum(e['event']['origin']['case_id'] == cid for e in cross))
             for cid, c in annotations.items() if len(c['persons']) > 1]
    return dict(cross_track_updates=cross, multi_person_cases=multi)


def run(args):
    require(not args.output.exists(), 'output exists; no overwrite')
    overlay = verify_overlay(args.spatial_final, args.parent_frozen)
    marker, meta, rows = read_run(args.upstream, args.parent_frozen)
    require(meta['spatial_freeze_sha256'] == sha(args.spatial_final/'freeze.json'),
            'input does not use approved spatial overlay')
    require(meta['fall_config']['mode'] == 'combined', 'requires retention combined')
    require(meta['robot_motion'] == 'unknown' and meta['depth'] is None, 'requires RGB-only data')
    locks = {Path(p): digest for p, digest in json.loads(args.source_lock.read_text()).items()}
    locks.update({Path(p): digest for p, digest in meta['retention_source_sha256'].items()})
    locks.update({args.upstream/'frames.jsonl': marker['frames_sha256'],
                  args.upstream/'run.json': marker['run_sha256'],
                  args.upstream/'completed.json': sha(args.upstream/'completed.json'),
                  args.source_lock: sha(args.source_lock)})
    source_paths = [
        Path(__file__),
        Path(__file__).with_name('experimental_request_coalescing.py'),
        Path(__file__).with_name('experimental_request_dedup.py'),
        Path(__file__).with_name('replay_request_dedup.py'),
        Path(__file__).with_name('replay_pose_retention.py'),
        Path(__file__).with_name('replay_pose_stability.py'),
        Path(__file__).with_name('score_fall_baseline.py'),
        AGENT/'evaluations/synthetic_fall_v1/REQUEST_COALESCING_PROTOCOL.md']
    sources = {p: sha(p) for p in source_paths}
    for p, digest in {**locks, **sources}.items():
        require(sha(p) == digest, 'changed source/input: '+str(p))
    media = json.loads((args.spatial_final/'media.json').read_text())
    metas = {m['case_id']: m for m in media['cases']}
    args.output.mkdir(mode=0o700)
    summary = dict(created_utc=datetime.now(timezone.utc).isoformat(),
                   scope='offline known synthetic development data; not deployed',
                   videos=len(metas), frames=len(rows), model_calls=0, vlm_calls=0, paid_calls=0,
                   source_sha256={str(p): d for p, d in sources.items()},
                   spatial_freeze_sha256=sha(args.spatial_final/'freeze.json'),
                   upstream_frames_sha256=marker['frames_sha256'], arms={})
    write_json(args.output/'plan.json', dict(
        modes={k: asdict(v) if v is not None else 'legacy' for k, v in MODES.items()},
        **{k: v for k, v in summary.items() if k != 'arms'}))
    for name, cfg in MODES.items():
        folder = args.output/name
        folder.mkdir()
        dispatches = []
        with (folder/'dispatches.jsonl').open('x') as stream:
            def emit(event):
                stream.write(json.dumps(event, allow_nan=False)+'\n')
                stream.flush()
                dispatches.append(event)
            diagnostics = replay(rows, metas, cfg, emit)
            os.fsync(stream.fileno())
        scored = source_aligned(rows, dispatches, diagnostics)
        if name == 'control':
            require(all(semantic(a) == semantic(b) for a, b in zip(rows, scored)),
                    'control differs from saved combined output')
        settings = dict(upstream=meta['fall_config'], coalescing=asdict(cfg) if cfg else None)
        digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        alignment_note = 'frames.jsonl is a scoring view; dispatches.jsonl records dispatch time'
        outmeta = dict(meta, fall_config=settings, fall_config_sha256=digest,
                       scope=summary['scope'], source_alignment_warning=alignment_note)
        for r in scored:
            r['fall_analysis'].update(configSha256=digest,
                                      algorithmVersion='request-coalescing-experiment-v1')
        validate_rows(scored, media, outmeta)
        evidence = copy.deepcopy(scored)
        for r in evidence:
            r['fall_analysis']['candidates'].extend(
                u['evidence'] for u in r['verification_updates'])
        events(evidence)
        comparison = compare(rows, scored, args.spatial_final)
        checks = audit(scored, dispatches, args.spatial_final, metas, overlay['match'])
        write_json(folder/'comparison.json', comparison)
        write_json(folder/'association-audit.json', checks)
        write_json(folder/'run.json', outmeta)
        for filename, records in [('frames.jsonl', scored), ('diagnostics.jsonl', diagnostics)]:
            with (folder/filename).open('x') as stream:
                for record in records:
                    stream.write(json.dumps(record, allow_nan=False)+'\n')
        labels = {c['case_id']: c['label'] for c in comparison['cases']}
        requests = [e for e in dispatches if e['dispatch_kind'] == 'request']
        first = {}
        for event in requests:
            first.setdefault(event['origin']['case_id'], event)
        before_counts = Counter(e['case_id'] for e in events(rows))
        after_counts = Counter(e['origin']['case_id'] for e in requests)
        changes = [dict(case_id=cid, before=before_counts[cid], after=after_counts[cid])
                   for cid in sorted(metas) if before_counts[cid] != after_counts[cid]]
        delayed = [e for e in dispatches if e['delay_sec'] > 1e-9]
        request_counts = Counter(labels[e['origin']['case_id']] for e in requests)
        conflicts = sum(c['known_different_people'] for e in checks['cross_track_updates']
                        for c in e['exact_frame_checks'])
        arm = dict(groups=comparison['after']['groups'], requests=len(requests),
                   requests_by_label=dict(request_counts), changed_request_counts=changes,
                   changed_first_requests=comparison['changed_cases'],
                   delayed_evidence=len(delayed), delayed_events=delayed,
                   maximum_delay_sec=max((e['delay_sec'] for e in dispatches), default=0),
                   first_request_maximum_delay_sec=max((e['delay_sec'] for e in first.values()),
                                                       default=0),
                   cross_track_updates=len(checks['cross_track_updates']),
                   exact_gt_conflicts=conflicts,
                   lost_candidate_cases=[cid for cid in before_counts if cid not in after_counts],
                   processing_ms=timing_stats([d['processing_ms'] for d in diagnostics]),
                   control_parity=True if name == 'control' else None,
                   metric='request anywhere in video, NOT target-verified recall',
                   delay_scope='simulated source-time dispatch; NOT live wall clock or VLM latency')
        require(not arm['lost_candidate_cases'], 'lost an entire video candidate')
        require(arm['first_request_maximum_delay_sec'] == 0, 'delayed first request')
        require(arm['maximum_delay_sec'] <= .5+1e-9, 'exceeded dispatch wait')
        require(not arm['exact_gt_conflicts'], 'known distinct people merged')
        summary['arms'][name] = arm
        write_json(folder/'completed.json', dict(
            files={p.name: sha(p) for p in folder.iterdir() if p.is_file()},
            evidence_preserved=True, pose_unchanged=True, pending_at_end=0, **arm))
        print(json.dumps(dict(mode=name, requests=arm['requests'],
                              requests_by_label=arm['requests_by_label'],
                              changes=changes, delayed=len(delayed),
                              maximum_delay=arm['maximum_delay_sec']), ensure_ascii=False),
              flush=True)
    for p, digest in {**locks, **sources}.items():
        require(sha(p) == digest, 'source/input changed during replay: '+str(p))
    verify_overlay(args.spatial_final, args.parent_frozen)
    write_json(args.output/'summary.json', summary)
    write_json(args.output/'completed.json', dict(summary_sha256=sha(args.output/'summary.json'),
               plan_sha256=sha(args.output/'plan.json'), source_inputs_unchanged=True,
               arms={name: sha(args.output/name/'completed.json') for name in MODES}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('upstream', 'parent-frozen', 'spatial-final', 'source-lock', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
