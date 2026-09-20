#!/usr/bin/env python3
"""Replay request-only deduplication on an immutable, completed Pose-loss run.

No new model inference, label-conditioned choices, API calls or product changes.
All upstream candidates survive as requests or verbatim evidence updates.
"""
import argparse
from collections import Counter
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from experimental_request_dedup import RequestDedupConfig, RequestDedupExperiment
from replay_fall_baseline import AGENT, SOURCE, sha, verify_freeze, write_json
from review_fall_annotations import require
from score_fall_baseline import events, validate_rows


def payloads(candidates, updates):
    return Counter(json.dumps(c, sort_keys=True, allow_nan=False)
                   for c in [*candidates, *(u['evidence'] for u in updates)])


def run(args):
    verify_freeze(args.frozen)
    completed = json.loads((args.upstream / 'completed.json').read_text())
    upstream_sources = {args.upstream / n: completed[k] for n, k in (
        ('frames.jsonl', 'frames_sha256'), ('run.json', 'run_sha256'))}
    for path, digest in upstream_sources.items():
        require(sha(path) == digest, f'changed input: {path.name}')
    metadata = json.loads((args.upstream / 'run.json').read_text())
    require(metadata['freeze_sha256'] == sha(args.frozen / 'freeze.json'), 'wrong freeze')
    require(metadata['media_sha256'] == sha(args.frozen / 'media.json'), 'wrong media')
    require(metadata['fall_config']['mode'] in {'pose-loss', 'pose-gap'},
            'requires isolated pose-loss or pose-gap input')
    for name, digest in metadata['source_sha256'].items():
        upstream_sources[SOURCE / name] = digest
    for path, digest in metadata['experiment_source_sha256'].items():
        upstream_sources[Path(path)] = digest
    for path, digest in upstream_sources.items():
        require(sha(path) == digest, f'changed upstream source: {path.name}')
    media = json.loads((args.frozen / 'media.json').read_text())
    rows = [json.loads(line) for line in (args.upstream / 'frames.jsonl').read_text().splitlines()]
    validate_rows(rows, media, metadata)
    # Validate both initial and subsequent evidence references before suppressing any request.
    evidence_rows = copy.deepcopy(rows)
    for r in evidence_rows:
        r['fall_analysis']['candidates'].extend(u['evidence'] for u in r['verification_updates'])
    events(evidence_rows)
    cfg = RequestDedupConfig()
    sources = {str(p): sha(p) for p in (
        Path(__file__), Path(__file__).with_name('experimental_request_dedup.py'),
        Path(__file__).with_name('score_fall_baseline.py'),
        AGENT / 'evaluations/synthetic_fall_v1/REQUEST_DEDUP_PROTOCOL.md')}
    settings = dict(upstream=metadata['fall_config'], request_dedup=asdict(cfg))
    config_sha = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    outmeta = dict(metadata, created_utc=datetime.now(timezone.utc).isoformat(),
                   scope='offline request coalescing; not deployed; cached inference only',
                   upstream_frames_sha256=completed['frames_sha256'],
                   upstream_run_sha256=completed['run_sha256'],
                   request_dedup_source_sha256=sources,
                   fall_config=settings, fall_config_sha256=config_sha,
                   timing_note='pose_ms/pipeline_ms/experiment_ms inherited from upstream; '
                               'request_dedup_ms measures only new request processing')
    args.output.mkdir(mode=0o700, exist_ok=False)
    write_json(args.output / 'run.json', outmeta)
    metas = {m['case_id']: m for m in media['cases']}
    current, counts, output_rows = None, Counter(), []
    with (args.output / 'frames.jsonl').open('x', buffering=1) as stream:
        for row in rows:
            cid = row['case_id']
            if cid != current:
                experiment, current = RequestDedupExperiment(cfg), cid
            size = metas[cid]['width'], metas[cid]['height']
            start = time.perf_counter()
            requests, updates, details = experiment.update(row, image_size=size)
            elapsed = (time.perf_counter()-start)*1000
            require(payloads(requests, updates) == payloads(
                row['fall_analysis']['candidates'], row['verification_updates']),
                f'lost or changed candidate: {cid}:{row["frame_index"]}')
            out = copy.deepcopy(row)
            out['upstream_analysis'] = copy.deepcopy(row['fall_analysis'])
            out['upstream_verification_updates'] = copy.deepcopy(row['verification_updates'])
            out['request_dedup_ms'] = elapsed
            out['request_dedup_details'] = details
            out['verification_updates'] = updates
            out['fall_analysis'].update(
                algorithmVersion='pose-loss-request-dedup-experiment-v1',
                configSha256=config_sha, candidates=requests)
            # Original diagnostics and track IDs are untouched; routing is a separate layer.
            out['verification_routes'] = dict(experiment.routes)
            stream.write(json.dumps(out, allow_nan=False)+'\n')
            output_rows.append(out)
            counts.update(frames=1, input_requests=len(row['fall_analysis']['candidates']),
                          output_requests=len(requests), evidence_updates=len(updates),
                          cross_track_updates=sum('deduplication' in u for u in updates))
        stream.flush()
        os.fsync(stream.fileno())
    # Scorable request references remain genuine after the routing transformation.
    events(output_rows)
    validate_rows(output_rows, media, outmeta)
    for path, digest in {**upstream_sources, **{Path(k): v for k, v in sources.items()}}.items():
        require(sha(path) == digest, f'source changed during run: {path.name}')
    verify_freeze(args.frozen)
    write_json(args.output / 'completed.json', dict(
        counts, cases=len(metas), preserved_candidate_payloads=True,
        frames_sha256=sha(args.output / 'frames.jsonl'), run_sha256=sha(args.output / 'run.json')))
    print(json.dumps(dict(counts), sort_keys=True))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
