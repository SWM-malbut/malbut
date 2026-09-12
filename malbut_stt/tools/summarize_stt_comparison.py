"""Summarize recorded STT comparisons using only the Python standard library."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import unicodedata


ENGINES = ('local_base', 'local_hint', 'openai')
LIMITATIONS = (
    'Yuna 한 화자의 합성 음성 20개를 각 엔진에서 한 번씩 측정했습니다. 정답은 TTS 입력 문장입니다.',
    '유한한 WAV의 끝을 알고 처리하며 VAD, 실시간 호출어 대기, 마이크·거리·로봇 소음은 평가하지 않습니다.',
    '로컬 모델 로드와 워밍업은 시간에서 제외했고 API 시간에는 네트워크가 포함됩니다.',
    'local_base와 OpenAI가 본 비교이며 local_hint는 기존 이름 힌트 설정을 사용한 보조 결과입니다.',
    'CER는 NFC 변환 후 공백과 Unicode P 범주 문장부호를 제거합니다. 숫자 표현은 정규화하지 않아 '
    '십오→15 같은 표기 차이도 오류로 계산됩니다.',
    'CER는 성공한 항목만 집계하므로 평가 수와 예정 수를 함께 확인해야 합니다. 실패·미완료는 별도 표시합니다.',
    '호출어 정답은 제이크야 단독 3개입니다. 문장 안의 제이크야는 이번 판정에서 비호출어로 처리합니다.',
)


def normalize(text):
    """Ignore only Unicode punctuation and whitespace after NFC normalization."""
    return ''.join(c for c in unicodedata.normalize('NFC', text)
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def edit_distance(reference, hypothesis):
    """Count character insertions, deletions, and substitutions."""
    previous = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        current = [i]
        for j, right in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def aggregate(rows):
    """Keep missing results visible alongside successful character-error totals."""
    ok = [row for row in rows if row['status'] == 'ok']
    error_chars = sum(row['error_chars'] for row in ok)
    ref_chars = sum(len(row['normalized_reference']) for row in ok)
    return {'denominator': len(rows), 'evaluated_count': len(ok),
            'failed_count': sum(row['status'] == 'error' for row in rows),
            'pending_count': sum(row['status'] == 'pending' for row in rows),
            'exact_count': sum(row['error_chars'] == 0 for row in ok),
            'error_chars': error_chars, 'ref_chars': ref_chars,
            'planned_ref_chars': sum(len(row['normalized_reference']) for row in rows),
            'cer': error_chars / ref_chars if ref_chars else None}


def summarize(data_dir):
    """Validate recorded result identities and calculate each condition's metrics."""
    sources = {name: (data_dir / name).read_bytes()
               for name in ('experiment.json', 'results.jsonl', 'attempts.jsonl')}
    experiment = json.loads(sources['experiment.json'])
    cases = {case['id']: case for case in experiment['cases']}
    if len(cases) != len(experiment['cases']):
        raise ValueError('duplicate corpus id')
    records = {}
    for filename in ('attempts.jsonl', 'results.jsonl'):
        records[filename] = {}
        for line in sources[filename].splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (row['id'], row['engine'])
            if key in records[filename] or key[0] not in cases or key[1] not in ENGINES:
                raise ValueError(f'duplicate or unknown record in {filename}: {key}')
            if row['pcm_sha256'] != cases[key[0]]['pcm_sha256']:
                raise ValueError(f'PCM provenance mismatch: {key}')
            records[filename][key] = row
    attempts, results = records['attempts.jsonl'], records['results.jsonl']
    if not results.keys() <= attempts.keys():
        raise ValueError('result without recorded attempt')
    summary = {'generated_at': datetime.now(timezone.utc).isoformat(),
               'sources': {name: {'path': str(data_dir / name),
                                  'sha256': hashlib.sha256(raw).hexdigest()}
                           for name, raw in sources.items()},
               'experiment': experiment, 'limitations': LIMITATIONS, 'engines': {}}
    for engine in ENGINES:
        rows = []
        for case_id, case in cases.items():
            key = (case_id, engine)
            row = dict(case, **results.get(key, {'status': 'pending'}))
            row.update(reference=case['text'], normalized_reference=normalize(case['text']),
                       attempted=key in attempts, wav=str(data_dir / case['wav']))
            if row['status'] not in ('ok', 'error', 'pending'):
                raise ValueError(f'unknown result status: {key}')
            if row['status'] == 'ok':
                if (not isinstance(row['wake_detected'], bool)
                        or not math.isfinite(row['elapsed_s']) or row['elapsed_s'] < 0):
                    raise ValueError(f'invalid successful result: {key}')
                row['normalized_text'] = normalize(row['text'])
                row['error_chars'] = edit_distance(row['normalized_reference'], row['normalized_text'])
                row['wake_pass'] = row['wake_detected'] == case['expected_wake']
            else:
                row.pop('text', None)
            rows.append(row)
        ok = [row for row in rows if row['status'] == 'ok']
        times = sorted(row['elapsed_s'] for row in ok)
        elapsed, duration = sum(times), sum(row['audio_s'] for row in ok)
        wake = {}
        for label, expected in (('positive', True), ('negative', False)):
            selected = [row for row in rows if row['expected_wake'] == expected]
            wake[label] = {'denominator': len(selected),
                           **{state: sum(row['status'] == state for row in selected)
                              for state in ('ok', 'error', 'pending')},
                           'pass': sum(row.get('wake_pass') is True for row in selected),
                           'fail': sum(row.get('wake_pass') is False for row in selected)}
        summary['engines'][engine] = {
            'status': {state: sum(row['status'] == state for row in rows)
                       for state in ('ok', 'error', 'pending')},
            'attempted_count': sum(row['attempted'] for row in rows),
            'body': aggregate([row for row in rows if row['category'] in ('command', 'daily')]),
            'speech': aggregate([row for row in rows if row['category'] != 'silence']),
            'timing': {'denominator': len(rows), 'success_count': len(ok),
                       'median_s': statistics.median(times) if times else None,
                       'max_s': max(times) if times else None,
                       'p95_s': times[math.ceil(.95 * len(times)) - 1] if times else None,
                       'p95_method': 'nearest rank', 'elapsed_s': elapsed, 'audio_s': duration,
                       'aggregate_rtf': elapsed / duration if duration else None},
            'wake': wake, 'silence': [dict(id=row['id'], status=row['status'],
                                         text=row.get('text'), hallucination=bool(row['text'].strip())
                                         if row['status'] == 'ok' else None)
                                    for row in rows if row['category'] == 'silence'], 'cases': rows}
    return summary


def report(summary, data_dir):
    """Render the fixed comparison and every original transcript as Markdown."""
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', '<br>')

    def number(value):
        return '—' if value is None else f'{value:.4f}'

    lines = ['# 한국어 STT 합성 음성 비교', '',
             '로컬은 Whisper small / faster-whisper 1.2.1 / CPU int8 / 6 threads이며, '
             'OpenAI는 gpt-transcribe입니다. local_hint에는 “로봇 이름은 제이크입니다.”를 넣었습니다.',
             '', *('- ' + text for text in LIMITATIONS), '',
             '| 엔진 | 성공 / 실패 / 미완료 | 본문 정규화 완전일치 | 본문 CER (오류/참조 글자) | 본문 평가 수 | 음성 전체 완전일치 | 음성 전체 CER (오류/참조 글자) | 음성 평가 수 |',
             '|---|---|---|---|---|---|---|---|']
    for engine, result in summary['engines'].items():
        values = [engine, ' / '.join(str(result['status'][s]) for s in ('ok', 'error', 'pending'))]
        for subset in ('body', 'speech'):
            stat = result[subset]
            values.extend([f"{stat['exact_count']}/{stat['denominator']}",
                           f"{number(stat['cer'])} ({stat['error_chars']}/{stat['ref_chars']})",
                           f"{stat['evaluated_count']}/{stat['denominator']}"])
        lines.append('| ' + ' | '.join(values) + ' |')
    lines += ['', '본문은 command+daily 11개, 음성 전체는 무음을 제외한 19개입니다. CER는 비율이며 0.1은 10%입니다.', '',
              '| 엔진 | 시간 성공/예정 | 중앙값(초) | p95(초, nearest rank) | 총 처리/음성(초) | 합산 RTF | 호출 양성 P/F (평가/예정) | 호출 음성 P/F (평가/예정) | 무음 출력 |',
              '|---|---|---|---|---|---|---|---|---|']
    for engine, result in summary['engines'].items():
        timing = result['timing']
        values = [engine, f"{timing['success_count']}/{timing['denominator']}",
                  number(timing['median_s']), number(timing['p95_s']),
                  f"{number(timing['elapsed_s'])}/{number(timing['audio_s'])}", number(timing['aggregate_rtf'])]
        for label in ('positive', 'negative'):
            wake = result['wake'][label]
            values.append(f"{wake['pass']}/{wake['fail']} ({wake['ok']}/{wake['denominator']})")
        values.append('; '.join(cell(row['text']) if row['text'] else ('빈 문자열' if row['status'] == 'ok' else row['status'])
                                for row in result['silence']))
        lines.append('| ' + ' | '.join(values) + ' |')
    lines += ['', 'P/F는 기대한 단독 호출 판정의 통과/실패입니다. 성공 결과의 무음 출력이 비어 있지 않으면 환각으로 집계합니다.', '',
              '관측 최대 처리 시간: ' + ', '.join(
                  f"{engine} {number(result['timing']['max_s'])}초"
                  for engine, result in summary['engines'].items()) + '. '
              '20개 단회 측정의 p95는 가장 느린 1개를 제외하므로 최대값도 함께 확인합니다.', '',
              '| 음성 파일 | 분류 | 정답 원문 | local_base (본 비교) | local_hint (보조) | OpenAI (본 비교) |',
              '|---|---|---|---|---|---|']
    for index, case in enumerate(summary['experiment']['cases']):
        values = [f"[{case['id']}](<{data_dir / case['wav']}>)", case['category'], cell(case['text']) or '(무음)']
        for engine in ENGINES:
            row = summary['engines'][engine]['cases'][index]
            values.append(cell(row['text']) or '(빈 문자열)' if row['status'] == 'ok'
                          else f"{row['status']}: {cell(row.get('error', '시도 후 대기' if row['attempted'] else '미시도'))}")
        lines.append('| ' + ' | '.join(values) + ' |')
    lines += ['', *[f'- [{name}](<{data_dir / name}>)' for name in
                     ('experiment.json', 'results.jsonl', 'attempts.jsonl', 'summary.json',
                      'model_provenance.json')], '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('data_dir', type=Path)
    directory = parser.parse_args().data_dir.resolve()
    output = summarize(directory)
    (directory / 'summary.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (directory / 'report.md').write_text(report(output, directory), encoding='utf-8')
    print(directory / 'report.md')
