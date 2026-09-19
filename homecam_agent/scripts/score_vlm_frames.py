#!/usr/bin/env python3
"""Score an offline RGB-frame run AFTER inference, without changing any label."""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics

from replay_fall_baseline import sha, verify_freeze
from replay_vlm_frames import assess_response, digest, save
from review_fall_annotations import require


LABELS = ('observed_fall', 'found_down', 'normal_activity', 'suspected_fall', 'unobservable')
OUTPUTS = ('confirmed_fall', 'found_down', 'normal_activity', 'unobservable')


def summarize(rows):
    versions = {r.get('evaluation_version', 'v1') for r in rows}
    if 'v2' in versions:
        from fall_evaluation_v2 import score
        require(versions == {'v2'}, 'mixed scoring versions')
        return score(rows, {r['case_id']: dict(
            label=r['label'], judgment_evidence_limited=r.get('judgment_evidence_limited', False),
            judgment_note=r.get('judgment_note', '')) for r in rows}, 'full')
    groups = {}
    for label in LABELS:
        subset = [r for r in rows if r['label'] == label]
        if not subset:
            continue
        valid = [r for r in subset if r['valid']]
        groups[label] = dict(
            total=len(subset), valid=len(valid), invalid=len(subset) - len(valid),
            accepted_distribution=dict(Counter(r['assessment'] for r in valid)),
            raw_distribution_not_validated=dict(Counter(
                r['assessment'] for r in subset if r['assessment'] is not None)),
            accepted_fall_or_found_down=sum(
                r['assessment'] in ('confirmed_fall', 'found_down') for r in valid),
        )
    seconds = [r['request_s'] for r in rows if r['request_s'] is not None]
    return dict(
        cases=len(rows), valid=sum(r['valid'] for r in rows), groups=groups,
        request_latency_s=dict(
            count=len(seconds), median=statistics.median(seconds) if seconds else None,
            p95=sorted(seconds)[math.ceil(.95 * len(seconds)) - 1] if seconds else None,
            minimum=min(seconds) if seconds else None, maximum=max(seconds) if seconds else None,
        ),
        semantic_errors=dict(Counter(e for r in rows for e in r['semantic_errors'])),
        schema_errors=dict(Counter(e for r in rows for e in r['schema_errors'])),
    )


def load_rows(frozen, run):
    verify_freeze(frozen)
    metadata = json.loads((run / 'run.json').read_text())
    complete = json.loads((run / 'completed.json').read_text())
    require(sha(run / 'run.json') == complete['run_sha256'], 'run metadata changed')
    for name, expected in complete['files'].items():
        require(Path(name).name == name, 'invalid evidence path')
        require(sha(run / name) == expected, 'result evidence changed')
    contract = metadata['contract']
    require(digest(contract) == metadata['contract_sha256'], 'contract hash mismatch')
    require(sha(frozen / 'freeze.json') == contract['freeze_sha256'], 'wrong dataset freeze')
    require(sha(frozen / 'media.json') == contract['media_sha256'], 'wrong media')
    labels = json.loads((frozen / 'evaluation_labels.json').read_text())['classifications']['cases']
    lookup = {row['case_id']: row for row in labels}
    require(set(lookup) == set(contract['case_order']), 'label/case mismatch')
    rows = []
    for case_id in contract['case_order']:
        result = json.loads((run / f'{case_id}.result.json').read_text())
        source = json.loads((run / f'{case_id}.input.json').read_text())
        require(result['contract_sha256'] == metadata['contract_sha256'], 'result contract changed')
        require(source['media_sha256'] == lookup[case_id]['source_sha256'], 'label/video mismatch')
        if result.get('status') == 'responded':
            require(sha(run / f'{case_id}.response.json') == result['response_sha256'],
                    'response changed')
        original_valid = result['valid']
        original_errors = result.get('semantic_errors', [])
        # The prompt publishes seconds to 3 decimals (e.g. 62/12 -> 5.167).
        # Validate on that SAME clock. Preserve the original strict result for audit;
        # do not penalize an answer equal to the end time we actually supplied.
        if result.get('status') == 'responded':
            raw = json.loads((run / f'{case_id}.response.json').read_text())
            result.update(assess_response(raw, round(source['duration_s'], 3),
                                          evaluation_version=contract.get('evaluation_version', 'v1')))
        prediction = result.get('prediction') or {}
        telemetry = result.get('telemetry') or {}
        rows.append(dict(
            case_id=case_id, filename=Path(lookup[case_id]['source_path']).name,
            evaluation_version=contract.get('evaluation_version', 'v1'),
            status=result['status'], prediction=result.get('prediction'),
            label=lookup[case_id]['label'], valid=result['valid'],
            judgment_evidence_limited=lookup[case_id].get('judgment_evidence_limited', False),
            judgment_note=lookup[case_id].get('judgment_note', ''),
            original_strict_valid=original_valid, original_semantic_errors=original_errors,
            timestamp_rounding_revalidated=original_errors != result.get('semantic_errors', []),
            assessment=(prediction.get('label') if contract.get('evaluation_version') == 'v2'
                        else prediction.get('fall', {}).get('assessment')),
            risk=prediction.get('risk'), request_s=result.get('request_s'),
            wall_s=result.get('wall_s'),
            server_load_s=(telemetry.get('load_duration') or 0) / 1e9,
            schema_errors=result.get('schema_errors', []),
            semantic_errors=result.get('semantic_errors', []),
            error_type=result.get('error_type'),
            explanation_ko=prediction.get('explanation_ko'),
            evidence_ko=prediction.get('evidence_ko'),
        ))
    return rows


def markdown(rows, summary):
    if summary.get('evaluation_version') == 'v2':
        from fall_evaluation_v2 import report_lines
        return '\n'.join(['# RGB 영상 3분류 평가', ''] + report_lines([
            ('평가 모델(모델 버전은 run.json 참조)', 'full', summary)]))
    lines = ['# Qwen RGB 프레임 비교 결과', '',
             '합성 개발 영상의 클립 단위 결과다. 제품 낙상 감지율이나 Jetson 속도가 아니다.',
             '모델 출력이 서로 모순되면 분류 정답으로 인정하지 않았다.', '',
             '| 정답 | 전체 | 유효 응답 | 낙상 과정 | 이미 쓰러짐 | 정상 | 판단 불가 | 오류 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    names = dict(observed_fall='낙상 과정', found_down='이미 쓰러짐', normal_activity='정상',
                 suspected_fall='애매함', unobservable='판단 불가')
    for label, group in summary['groups'].items():
        counts = [group['accepted_distribution'].get(key, 0) for key in OUTPUTS]
        values = [names[label], group['total'], group['valid'], *counts, group['invalid']]
        lines.append('| ' + ' | '.join(map(str, values)) + ' |')
    lines += ['', '오류와 판단 불가는 정상 또는 감지 성공으로 바꾸어 세지 않는다.',
              '애매한 5개는 낙상 확정/정상 정확도에서 제외한다.', '', '## 영상별 원본 답', '',
              '아래 판정은 오류가 있는 답도 포함한다. `유효=아니오`는 위 정답 집계에 들어가지 않는다.',
              '', '| 영상 | 정답 | 모델 판정 | 유효 | 요청 시간(초) | 문제 |',
              '| --- | --- | --- | --- | ---: | --- |']
    for row in rows:
        seconds = f'{row["request_s"]:.2f}' if row['request_s'] is not None else '-'
        errors = row['schema_errors'] + row['semantic_errors']
        if row['error_type']:
            errors.append(row['error_type'])
        cells = [row['filename'], row['label'], row['assessment'] or '응답 없음',
                 '예' if row['valid'] else '아니오', seconds, ', '.join(errors) or '-']
        lines.append('| ' + ' | '.join(cells) + ' |')
    adjusted = [r['case_id'] for r in rows if r['timestamp_rounding_revalidated']]
    lines += ['', '시간 경계는 모델에 보낸 값과 같은 소수점 3자리로 검사했다.',
              '원본 응답·정답은 변경하지 않았다. 최초 엄격 검사 결과는 report.json에 별도 보존.',
              '시간 반올림으로 재검증된 영상: ' + (', '.join(adjusted) or '없음') + '.']
    latency = summary['request_latency_s']
    lines += ['', '## 처리 시간', '',
              f'- 요청~응답 중앙값: {latency["median"]:.2f}초',
              f'- 요청~응답 p95: {latency["p95"]:.2f}초',
              '- 네트워크 왕복 포함. 영상 수집·질문·알림 시간은 포함하지 않는다.',
              '- 적재 시간과 프레임 추출 포함 시간은 report.json의 영상별 기록에 별도 보관.', '',
              '## 해석할 때 주의할 점', '',
              '- 12장 이미지 입력이다. 장면 사이의 빠른 동작은 빠질 수 있다.',
              '- 다른 사람에 대한 판정을 정답으로 잘못 세지 않았는지 영상별 검토가 추가로 필요하다.',
              '- YOLO가 놓친 영상을 직접 주어 얻은 결과는 자동 호출 경로의 해결을 뜻하지 않는다.',
              '- 모델 하나의 최초 비교다. 다른 모델, 실영상, 로봇에서의 성능은 미검증이다.', '']
    return '\n'.join(lines)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = load_rows(args.frozen, args.run)
    summary = summarize(rows)
    args.output.mkdir(mode=0o700, exist_ok=False)
    save(args.output / 'report.json', dict(
        summary=summary, rows=rows, completed_sha256=sha(args.run / 'completed.json'),
        scoring_script_sha256=sha(Path(__file__))))
    with (args.output / 'report.md').open('x', encoding='utf-8') as stream:
        stream.write(markdown(rows, summary))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
