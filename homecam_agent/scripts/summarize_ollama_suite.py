#!/usr/bin/env python3
"""Read-only audit/score of model evidence; writes a new private report directory.

Reports partial runs explicitly; never scores an incomplete model as a completed test.
"""
import argparse
from collections import Counter
import csv
import json
import math
import os
from pathlib import Path
import re
import statistics

from replay_fall_baseline import sha, verify_freeze
from replay_vlm_frames import assess_response, digest, save
from run_ollama_fall_suite import metrics, verify_completed
from review_fall_annotations import require


def wilson(success, total):
    if not total:
        return None
    z, p = 1.959963984540054, success / total
    denominator = 1 + z*z / total
    centre = (p + z*z / (2*total)) / denominator
    radius = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [max(0, centre-radius), min(1, centre+radius)]


def distribution(values):
    values = [v for v in values if v is not None]
    return dict(count=len(values), median=statistics.median(values) if values else None,
                p95=sorted(values)[math.ceil(.95*len(values))-1] if values else None,
                minimum=min(values) if values else None, maximum=max(values) if values else None)


def inspect_final_json(raw, duration):
    """Supplementary audit only: unwrap ONE fenced final JSON, never thinking/prose.

The original strict scores/raw response stay unchanged. No label enters this function.
"""
    message = raw.get('message')
    text = message.get('content') if isinstance(message, dict) else None
    answer = dict(wrapper=None, assessment=None, shape_valid=False, semantic_valid=False,
                  schema_errors=[], semantic_errors=[])
    if not isinstance(text, str) or not text.strip():
        answer['schema_errors'] = ['empty_final_answer']
        return answer
    text = text.strip()
    match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', text, flags=re.I)
    answer['wrapper'] = 'single_markdown_json_fence' if match else 'bare_or_invalid'
    if match:
        text = match.group(1)

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate key')
            value[key] = item
        return value

    try:
        value = json.loads(text, object_pairs_hook=unique_object)
    except ValueError:
        answer['schema_errors'] = ['invalid_final_json']
        return answer
    if not isinstance(value, dict):
        answer['schema_errors'] = ['final_json_not_object']
        return answer
    fall = value.get('fall')
    if (isinstance(fall, dict) and fall.get('assessment') in
            ('confirmed_fall', 'found_down', 'normal_activity', 'unobservable')):
        answer['assessment'] = fall['assessment']
    inspected = dict(raw, message=dict(message, content=text))
    checked = assess_response(inspected, round(duration, 3))
    answer.update(shape_valid=not checked['schema_errors'], semantic_valid=checked['valid'],
                  schema_errors=checked['schema_errors'],
                  semantic_errors=checked['semantic_errors'])
    return answer


def supplementary_counts(rows, labels):
    truth = dict(observed_fall='confirmed_fall', found_down='found_down',
                 normal_activity='normal_activity')
    scored = [r for r in rows if labels[r['case_id']]['label'] in truth]
    called = [r for r in scored if r['status'] != 'not_triggered']

    def correct(row):
        return (row.get('final_json_audit') or {}).get('assessment') == truth[
            labels[row['case_id']]['label']]

    return dict(
        eligible=len(scored), invoked_eligible=len(called),
        invoked_label_only_correct=sum(correct(r) for r in called),
        invoked_semantic_correct=sum(correct(r) and
                                     (r.get('final_json_audit') or {}).get('semantic_valid', False)
                                     for r in called),
        wrappers=dict(Counter((r.get('final_json_audit') or {}).get('wrapper')
                              for r in rows if r['status'] == 'responded')),
        note='supplementary final-answer audit; unwrap one JSON fence only; no output/GT rewriting')


def load_mode(directory, frozen, labels, annotations):
    metadata = json.loads((directory / 'run.json').read_text())
    contract = metadata['contract']
    version = contract.get('evaluation_version', 'v1')
    require(digest(contract) == metadata['contract_sha256'], 'changed run contract')
    require(contract['freeze_sha256'] == sha(frozen / 'freeze.json'), 'wrong frozen data')
    complete = (directory / 'completed.json').exists()
    if complete:
        verify_completed(directory)
    rows = []
    for path in sorted(directory.glob('SYN*.result.json')):
        value = json.loads(path.read_text())
        cid = value['case_id']
        require(cid in labels, 'unknown case')
        require(value['contract_sha256'] == metadata['contract_sha256'], 'changed result contract')
        row = dict(value, model=contract['model']['name'], mode=contract['mode'],
                   filename=Path(labels[cid]['source_path']).name,
                   label=labels[cid]['label'], full_model_run_completed=complete)
        if version == 'v2':
            row.update(judgment_evidence_limited=labels[cid].get('judgment_evidence_limited', False),
                       judgment_note=labels[cid].get('judgment_note', ''))
        source_path = directory / f'{cid}.input.json'
        if source_path.exists():
            source = json.loads(source_path.read_text())
            require(source['media_sha256'] == labels[cid]['source_sha256'], 'wrong source video')
            require(source['contract_sha256'] == metadata['contract_sha256'],
                    'changed input contract')
            row.update(input_frames=len(source['frames']),
                       input_last_frame=source['available_through_frame'],
                       input_duration_s=source['duration_s'],
                       pose_compute_ms_until_request=source.get('pose_compute_ms_until_request'))
            if contract['mode'] == 'gated':
                require(max(f['frame_index'] for f in source['frames']) <=
                        source['available_through_frame'], 'future frame in causal request')
            if value['status'] == 'responded':
                response_path = directory / f'{cid}.response.json'
                require(sha(response_path) == value['response_sha256'], 'raw response changed')
                raw = json.loads(response_path.read_text())
                content = raw.get('message') or {}
                if version == 'v2':
                    row.update(assess_response(raw, source['duration_s'], evaluation_version='v2'))
                else:
                    row['final_json_audit'] = inspect_final_json(raw, source['duration_s'])
                row.update(final_content_chars=len(content.get('content') or ''),
                           thinking_chars=len(content.get('thinking') or ''),
                           prompt_tokens=raw.get('prompt_eval_count'),
                           output_tokens=raw.get('eval_count'),
                           load_s=(raw.get('load_duration') or 0)/1e9,
                           decode_s=(raw.get('eval_duration') or 0)/1e9,
                           prefill_s=(raw.get('prompt_eval_duration') or 0)/1e9)
                span = annotations[cid].get('onset_frames')
                if span and value.get('trigger_s') is not None:
                    # Frozen media are 12 fps. Derive it from original frame timestamps instead
                    # of silently substituting a wall-clock time or an uncertain point label.
                    valid = [f for f in source['frames'] if f['timestamp_s'] > 0]
                    fps = valid[0]['frame_index'] / valid[0]['timestamp_s'] if valid else None
                    if fps:
                        row['source_trigger_delay_interval_s'] = [
                            value['trigger_s'] - span[1]/fps,
                            value['trigger_s'] - span[0]/fps]
        rows.append(row)
    if complete:
        require({r['case_id'] for r in rows} == set(labels), 'incomplete case set')
    measured = metrics(rows, labels, version, contract['mode']) if complete else None
    if measured:
        if version == 'v2':
            measured['classification_wilson95'] = wilson(
                measured['classification']['numerator'], measured['classification']['denominator'])
        else:
            measured['pipeline_accuracy_wilson95'] = wilson(
                measured['pipeline_correct'], measured['eligible'])
        measured['invoked_accuracy_wilson95'] = wilson(
            measured['invoked_correct'], measured['invoked_eligible'])
    all_responded = [r for r in rows if r['status'] == 'responded']
    unexpected = (sum(bool(r.get('thinking_chars')) for r in all_responded)
                  if contract['thinking'] == 'disabled' else None)
    errors = [e for r in rows for e in r.get('schema_errors', []) + r.get('semantic_errors', [])]
    return dict(
        model=contract['model']['name'], mode=contract['mode'], complete=complete,
        evaluation_version=version,
        saved_cases=len(rows), expected_cases=len(labels), measurements=measured,
        final_json_supplement=supplementary_counts(rows, labels) if complete and version == 'v1' else None,
        source=str(directory), contract_sha256=metadata['contract_sha256'], rows=rows,
        valid_output_rate=dict(valid=sum(bool(r.get('valid')) for r in all_responded),
                               responded=len(all_responded)),
        final_answer_empty=sum(r.get('final_content_chars') == 0 for r in all_responded),
        unexpected_thinking=unexpected,
        errors=dict(Counter(errors)),
        request_latency_s=distribution([r.get('request_s') for r in rows]),
        warm_request_latency_s=distribution([r.get('request_s') for r in all_responded[1:]]),
        load_latency_s=distribution([r.get('load_s') for r in rows]))


def percent(num, total):
    return f'{num}/{total} ({100*num/total:.1f}%)' if total else '대상 없음'


def csv_cell(value):
    # Spreadsheet viewers must not execute model-generated text as a formula.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def run(args):
    verify_freeze(args.frozen)
    gt = json.loads((args.frozen / 'evaluation_labels.json').read_text())
    labels = {r['case_id']: r for r in gt['classifications']['cases']}
    annotations = {r['case_id']: r for r in gt['annotations']['cases']}
    outputs = []
    for root in args.runs:
        for meta in sorted(root.glob('*/*/run.json')):
            outputs.append(load_mode(meta.parent, args.frozen, labels, annotations))
    args.output.mkdir(mode=0o700, exist_ok=False)
    save(args.output / 'details.json', dict(
        models=outputs, scoring_script_sha256=sha(Path(__file__)),
        label_counts=dict(Counter(r['label'] for r in labels.values())),
        labels_sha256=sha(args.frozen / 'evaluation_labels.json')))
    columns = ['model', 'mode', 'case_id', 'filename', 'label', 'status', 'valid', 'assessment',
               'request_s', 'trigger_s', 'input_last_frame', 'input_frames',
               'pose_compute_ms_until_request', 'source_trigger_delay_interval_s',
               'prompt_tokens', 'output_tokens', 'load_s', 'prefill_s', 'decode_s',
               'final_content_chars', 'thinking_chars', 'full_model_run_completed',
               'schema_errors', 'semantic_errors', 'explanation_ko',
               'final_json_assessment', 'json_wrapper', 'unwrapped_semantic_valid',
               'judgment_evidence_limited', 'judgment_note']
    with (args.output / 'cases.csv').open('x', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, columns, extrasaction='ignore')
        writer.writeheader()
        for result in outputs:
            for row in result['rows']:
                prediction = row.get('prediction') or {}
                assessment = (prediction.get('label') if result['evaluation_version'] == 'v2'
                              else (prediction.get('fall') or {}).get('assessment'))
                inspected = row.get('final_json_audit') or {}
                flat = dict(row, assessment=assessment,
                            explanation_ko=prediction.get('explanation_ko'),
                            final_json_assessment=inspected.get('assessment'),
                            json_wrapper=inspected.get('wrapper'),
                            unwrapped_semantic_valid=inspected.get('semantic_valid'))
                writer.writerow({k: csv_cell(v) for k, v in flat.items()})
    lines = ['# Ollama 낙상 영상 실측', '',
             f'총 {len(labels)}개 합성 개발 영상. 명확한 36개로 정확도를 계산한다.',
             '애매한 5개는 분포만 표시. 모델을 비교한 결과로 실물 성능을 보장하지 않는다.', '',
             '| 모델 | 방식 | 진행 | 호출된 명확한 영상 정답 | 전체 경로 정답 | 유효 출력 | 응답 중앙값 |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for out in outputs:
        if out['evaluation_version'] == 'v2':
            continue
        data = out['measurements']
        valid = out['valid_output_rate']
        latency = out['request_latency_s']['median']
        fields = [out['model'], out['mode'], '완료' if out['complete'] else
                  f'진행/중단 ({out["saved_cases"]}/{out["expected_cases"]})',
                  percent(data['invoked_correct'], data['invoked_eligible']) if data else '-',
                  percent(data['pipeline_correct'], data['eligible']) if data else '-',
                  percent(valid['valid'], valid['responded']),
                  f'{latency:.2f}초' if latency is not None else '-']
        lines.append('| ' + ' | '.join(fields) + ' |')
    lines += ['', '## 형식과 판정을 나눠 본 보조 결과', '',
              '최종 답변의 JSON 코드 블록 한 겹만 벗겨 확인했다. 원본·위 엄격 점수는 바꾸지 않았다.',
              '생각 과정(thinking)의 JSON은 최종 답으로 사용하지 않는다.', '',
              '| 모델 | 방식 | 판정 필드만 맞음 | 포장 제거 후 형식·의미도 맞음 |',
              '| --- | --- | --- | --- |']
    for out in outputs:
        audit = out['final_json_supplement']
        if not audit:
            continue
        total = audit['invoked_eligible']
        lines.append('| ' + ' | '.join([
            out['model'], out['mode'], percent(audit['invoked_label_only_correct'], total),
            percent(audit['invoked_semantic_correct'], total)]) + ' |')
    lines += ['', '## 읽는 법', '',
              '- full: 전체 영상에서 RGB 12장. gated: YOLO 후보 시점까지의 RGB만 전달.',
              '- gated에서 호출되지 않은 정상 영상은 시스템 정답에 포함한다. VLM의 답변은 아니다.',
              '- 후보를 못 만든 낙상 영상은 전체 경로 정답에서 미탐으로 남긴다.',
              '- 최종 답변이 비어 있는 경우는 출력 경로 실패다. 시각적인 판단 능력 0%로 해석하지 않는다.',
              '- Cloud는 서버 JSON 형식 강제를 지원하지 않으므로 prompt-only 조건이다.',
              '- 모델별 분포, 오류, 프레임, 지연, 원문은 details.json과 cases.csv에 있다.',
              '- source_trigger_delay는 영상 속 후보 지연 범위다. 실제 로봇 전체 처리 시간이 아니다.', '']
    for out in outputs:
        if not out['complete']:
            continue
        data = out['measurements']
        lines += [f'## {out["model"]} · {out["mode"]}', '',
                  f'실제 호출 {data["invocations"]}건 / 유효 응답 {data["valid"]}건.', '',
                  '| 라벨 | 개수 | 유효한 정답 | 호출 안 됨 | 유효 응답 분포 |',
                  '| --- | ---: | ---: | ---: | --- |']
        for label, group in data['groups'].items():
            lines.append(f'| {label} | {group["count"]} | {group["correct"]} | '
                         f'{group["not_triggered"]} | {group["accepted"]} |')
        lines += ['', f'오류 종류: {out["errors"]}',
                  f'최종 답변이 빈 호출: {out["final_answer_empty"]}건.', '']
    v2_results = [out for out in outputs if out['evaluation_version'] == 'v2']
    if v2_results:
        from fall_evaluation_v2 import report_lines
        if len(v2_results) == len(outputs):
            lines = ['# VLM 3분류 평가', '', f'평가 목록: {len(labels)}개. 미완료 실행에는 전체 점수를 표시하지 않는다.', '']
        lines += report_lines([(out['model'], out['mode'], out['measurements'])
                               for out in v2_results if out['complete']])
        for out in v2_results:
            if not out['complete']:
                lines.append(f'- {out["model"]} {out["mode"]}: 미완료 '
                             f'({out["saved_cases"]}/{out["expected_cases"]}), 전체 점수 없음')
            else:
                lines += [f'### {out["model"]} · {out["mode"]}', '',
                          '실패/미호출: ' + json.dumps(out['measurements']['failure_counts'], ensure_ascii=False),
                          '', '정답별 예측 분포: ' + json.dumps(out['measurements']['confusion'], ensure_ascii=False), '']
    with (args.output / 'report.md').open('x') as stream:
        stream.write('\n'.join(lines))
    print(args.output / 'report.md', flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--runs', nargs='+', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
