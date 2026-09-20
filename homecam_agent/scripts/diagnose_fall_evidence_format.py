#!/usr/bin/env python3
"""Post-hoc format diagnostic, NOT a new primary score or production normalizer.

Only equal duplicate values and two known schema/count metadata fields can be
removed. Conflicting duplicates, other extra fields and content errors remain
invalid. Never writes into the original evaluation or makes provider calls.
"""
import argparse
import json
import os
from pathlib import Path
import re

import fall_evidence_decision as decision
from fall_evaluation_v2 import score
from replay_fall_baseline import sha
from replay_fall_rechecks import terminal_predictions, verify_files
from replay_vlm_frames import canonical, save
from review_fall_annotations import require


def parse_format_only(raw, image_count):
    changes = []
    require(raw.get('done') is True and raw.get('done_reason') == 'stop', 'incomplete response')
    text = raw['message']['content'].strip()
    match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', text)
    if match:
        text = match.group(1)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                require(canonical(result[key]) == canonical(value), 'conflicting duplicate')
                changes.append('identical_duplicate:'+key)
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=unique)
    if 'additionalProperties' in value:
        require(value.pop('additionalProperties') is False, 'unexpected schema metadata')
        changes.append('schema_metadata')
    if 'observations_count' in value:
        count = value.pop('observations_count')
        require(type(count) is int and count == len(value['observations']), 'incorrect count')
        changes.append('count_metadata')
    errors = decision.validate(value, image_count)
    return dict(valid=not errors, evidence=value, schema_errors=errors, semantic_errors=[]), changes


def run(args):
    require(not args.output.exists(), 'output exists')
    verify_files(args.run)
    plan = json.loads((args.run/'plan.json').read_text())
    require(plan['prompt_sha256'] == decision.PROMPT_SHA256, 'different decision version')
    require(sha(args.spatial_final/'freeze.json') == plan['spatial_freeze_sha256'], 'different GT')
    labels = {c['case_id']: c for c in json.loads(
        (args.spatial_final/'evaluation_labels.json').read_text())['classifications']['cases']}
    parsed, diagnostics = {}, {}
    for c in plan['conditions']:
        key = c['input_id']
        if key in parsed:
            continue
        raw = json.loads((args.run/'calls'/key/'response.json').read_text())
        parsed[key], diagnostics[key] = parse_format_only(raw, len(c['frames']))
    summary = {}
    for mode in ('gated', 'full'):
        summary[mode] = {}
        for variant in ('model', 'policy'):
            rows = []
            for c in plan['conditions']:
                if c['mode'] != mode:
                    continue
                out = decision.project(parsed[c['input_id']], apply_policy=variant == 'policy')
                rows.append(dict(case_id=c['case_id'], incident_id=c['incident_id'], strict=dict(
                    out, case_id=c['case_id'], status='responded')))
            values = terminal_predictions(rows, labels, False) if mode == 'gated' else [
                r['strict'] for r in rows]
            summary[mode][variant] = score(values, labels, mode)
    args.output.mkdir(mode=0o700)
    save(args.output/'diagnostic.json', dict(
        scope='POST-HOC DIAGNOSTIC ONLY. Original scores/results remain immutable.',
        changed_input_count=sum(bool(v) for v in diagnostics.values()),
        operations=diagnostics, scores=summary, provider_calls=0,
        source_run_sha256=sha(args.run/'completed.json'),
        diagnostic_source_sha256=sha(Path(__file__))))
    for mode, values in summary.items():
        for variant, s in values.items():
            print('DIAGNOSTIC_NOT_PRIMARY', mode, variant, s['classification'])


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'spatial-final', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    run(parser.parse_args())
