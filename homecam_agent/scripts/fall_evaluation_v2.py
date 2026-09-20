"""Three-class evaluation contract and pure scoring. No model or network calls.

v1 responses are deliberately not converted: v2 is a separate run contract.
"""
from collections import Counter
import json
import math
from pathlib import Path
import statistics

VERSION = 'v2'
CRITERIA = Path(__file__).resolve().parents[1] / 'evaluations/synthetic_fall_v1/scoring_criteria_v2.json'
CRITERIA_AMENDMENT = CRITERIA.with_name('scoring_criteria_v2_r1.json')
CRITERIA_VERSION = 'fall-evaluation-v2-20260912-r1'
LABELS = ('observed_fall', 'suspected_fall', 'normal_activity')
NAMES = dict(zip(LABELS, ('낙상', '낙상 의심', '정상 행동')))
SYSTEM_PROMPT = (
    '가정용 낮은 위치 카메라의 RGB 영상 프레임을 보고 영상 단위로 판단하세요. '
    'observed_fall: 넘어지는 과정과 균형 상실 또는 의도치 않은 떨어짐이 뚜렷함. '
    'suspected_fall: 넘어지는 듯한 동작이 애매하거나 처음부터 쓰러진 듯한 상태여서 '
    '사고인지 휴식인지 구분하기 어려움. normal_activity: 자발적인 자세 변경·휴식·'
    '일상 활동이 명확함. 사람이 없는 영상도 정상 행동임. '
    '과정을 못 봤다는 이유만으로 의심하지 마세요. 느리면 정상, 침구가 없으면 낙상처럼 '
    '단서 하나를 자동 판정 규칙으로 쓰지 마세요. 필요한 단서 개수는 정해져 있지 않습니다. '
    '보이는 동작으로 판단하고 근거가 제한적이면 설명에 남기세요. '
    '넘어진 뒤 회복해도 목격한 낙상은 유지하세요. 부상·의식·무응답을 추측하지 마세요. '
    '의심은 정상적인 분류 결과입니다. 이미지 자체를 읽을 수 없어 관찰할 수 없는 경우만 '
    'outcome=unobservable, label=null로 응답하세요. 그 외에는 outcome=classified와 '
    '세 라벨 중 하나를 반환하세요. 한국어 explanation_ko에는 보이는 근거와 한계를 간단히 적으세요. '
    '지정된 JSON 객체 하나만 반환하세요.'
)
PREDICTION_JSON_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['outcome', 'label', 'explanation_ko'],
    'properties': {
        'outcome': {'type': 'string', 'enum': ['classified', 'unobservable']},
        'label': {'type': ['string', 'null'], 'enum': [*LABELS, None]},
        'explanation_ko': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
    },
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_prediction(value):
    errors = []
    if not isinstance(value, dict) or set(value) != {'outcome', 'label', 'explanation_ko'}:
        return ['invalid_prediction_fields']
    outcome, label, explanation = value['outcome'], value['label'], value['explanation_ko']
    if outcome not in ('classified', 'unobservable'):
        errors.append('invalid_outcome')
    if label is not None and (not isinstance(label, str) or label not in LABELS):
        errors.append('invalid_label')
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 4000:
        errors.append('invalid_explanation')
    if (outcome == 'classified' and label not in LABELS
            or outcome == 'unobservable' and label is not None):
        errors.append('outcome_label_mismatch')
    return errors


def assess_response(response, duration=None):
    errors, value = [], None
    if not isinstance(response, dict):
        errors.append('response_not_object')
    else:
        if response.get('done') is not True or response.get('done_reason') != 'stop':
            errors.append('incomplete_response')
        message = response.get('message')
        text = message.get('content') if isinstance(message, dict) else None

        def unique(pairs):
            result = {}
            for key, item in pairs:
                require(key not in result, 'duplicate_json_key')
                result[key] = item
            return result

        def reject_constant(value):
            raise ValueError('non_finite_json')

        try:
            require(isinstance(text, str), 'missing_final_answer')
            value = json.loads(text, object_pairs_hook=unique, parse_constant=reject_constant)
            errors.extend(validate_prediction(value))
        except (ValueError, TypeError, RecursionError):
            errors.append('invalid_final_json')
    return dict(prediction=value if isinstance(value, dict) else None,
                schema_errors=errors, semantic_errors=[], valid=not errors)


def fraction(n, d):
    return dict(numerator=n, denominator=d, rate=n / d if d else None)


def judgment_metadata(annotation):
    """Human annotation only; validate before inference, display only after scoring."""
    limited = annotation.get('judgment_evidence_limited', False)
    note = annotation.get('judgment_note', '')
    require(type(limited) is bool and isinstance(note, str) and len(note) <= 4000,
            'invalid human judgment note')
    require(not limited or bool(note.strip()), 'limited evidence requires a human note')
    return dict(judgment_evidence_limited=limited, judgment_note=note)


def score(rows, labels, mode):
    require(mode in ('full', 'gated'), 'unknown v2 mode')
    require(bool(labels), 'empty evaluation list')
    require(all(r['label'] in LABELS for r in labels.values()),
            'v2 requires reviewed three-class labels; no automatic found_down conversion')
    ids = [r['case_id'] for r in rows]
    require(len(ids) == len(set(ids)), 'duplicate result case')
    require(set(ids) == set(labels), 'incomplete/unknown cases; do not report a full score')
    resolved = []
    for row in rows:
        annotation = labels[row['case_id']]
        truth = annotation['label']
        evidence = judgment_metadata(annotation)
        status = row['status']
        require(status in ('responded', 'request_failed', 'timeout', 'not_triggered'),
                'unknown result status')
        require(type(row.get('valid')) is bool, 'valid must be a boolean')
        skipped = status == 'not_triggered'
        require(not skipped or mode == 'gated', 'full mode cannot skip a case')
        if skipped or status != 'responded':
            require(row['valid'] is False and row.get('prediction') is None,
                    'failed/skipped case contains a successful prediction')
        value = row.get('prediction')
        errors = validate_prediction(value) if status == 'responded' else []
        valid = bool(status == 'responded' and row['valid'] and not errors
                     and not row.get('schema_errors') and not row.get('semantic_errors'))
        pred = value['label'] if valid else None
        if skipped:
            outcome = 'not_called'
        elif status == 'timeout' or (status == 'request_failed'
                                    and row.get('error_type') in ('TimeoutError', 'timeout')):
            outcome = 'timeout'
        elif status == 'request_failed':
            outcome = 'request_failed'
        elif not valid:
            outcome = 'invalid_response'
        elif value['outcome'] == 'unobservable':
            outcome = 'unobservable'
        else:
            outcome = 'classified'
        needs_check = truth != 'normal_activity'
        caught = bool(outcome == 'classified' and pred != 'normal_activity')
        correct = bool(outcome == 'classified' and pred == truth)
        resolved.append(dict(row, label=truth, assessment=pred, valid=valid, outcome=outcome,
                             correct=correct, needs_check=needs_check, check_predicted=caught,
                             **evidence))
    invoked = [r for r in resolved if r['outcome'] != 'not_called']
    positive = [r for r in resolved if r['needs_check']]
    negative = [r for r in resolved if not r['needs_check']]
    caught = sum(r['check_predicted'] for r in positive)
    missed = sum(r['outcome'] == 'classified' and r['assessment'] == 'normal_activity'
                 for r in positive)
    unresolved = Counter(r['outcome'] for r in positive if r['outcome'] != 'classified')
    require(caught + missed + sum(unresolved.values()) == len(positive), 'outcomes do not partition')
    seconds = [r['request_s'] for r in rows if r.get('request_s') is not None]
    require(all(type(s) in (int, float) and math.isfinite(s) and s >= 0 for s in seconds),
            'invalid request latency')
    denominator = len(rows) if mode == 'full' else len(invoked)
    correct = sum(r['correct'] for r in resolved)
    return dict(
        evaluation_version=VERSION, mode=mode, rows=resolved, cases=len(rows),
        label_counts=dict(Counter(r['label'] for r in resolved)), eligible=len(rows),
        invocations=len(invoked), invoked_eligible=len(invoked),
        valid=sum(r['valid'] for r in resolved), valid_correct=correct,
        invoked_correct=correct, pipeline_correct=None,
        classification=fraction(correct, denominator),
        classification_scope='all_videos' if mode == 'full' else 'selected_for_call_only',
        whole_path_three_class_accuracy=None,
        checking=dict(
            caught=fraction(caught, len(positive)),
            missed_as_normal=fraction(missed, len(positive)),
            unresolved=fraction(sum(unresolved.values()), len(positive)),
            unresolved_by_reason=dict(unresolved),
            unnecessary=fraction(sum(r['check_predicted'] for r in negative), len(negative)),
            normal_unresolved=sum(r['outcome'] != 'classified' for r in negative)),
        failure_counts=dict(Counter(r['outcome'] for r in resolved if r['outcome'] != 'classified')),
        confusion={label: dict(Counter(
            r['assessment'] if r['outcome'] == 'classified' else r['outcome']
            for r in resolved if r['label'] == label)) for label in LABELS},
        groups={label: dict(
            count=sum(r['label'] == label for r in resolved),
            correct=sum(r['label'] == label and r['correct'] for r in resolved),
            not_triggered=sum(r['label'] == label and r['outcome'] == 'not_called' for r in resolved),
            accepted=dict(Counter(r['assessment'] for r in resolved
                                  if r['label'] == label and r['outcome'] == 'classified')))
                for label in LABELS},
        yolo_candidates=({label: fraction(
            sum(r['label'] == label and r['outcome'] != 'not_called' for r in resolved),
            sum(r['label'] == label for r in resolved)) for label in LABELS}
            if mode == 'gated' else None),
        yolo_metric_scope='candidate_anywhere_in_video_not_correct_person_recall',
        latency_s=dict(count=len(seconds), median=statistics.median(seconds) if seconds else None,
                       p95=sorted(seconds)[math.ceil(.95*len(seconds))-1] if seconds else None))


def report_lines(measurements):
    """Small common v2 report block used by both suite report commands."""
    lines = ['## v2 · 3분류 및 확인 대상 평가', '',
             '의심 영상과 실패를 분모에 포함한다. 미호출을 정상 예측으로 바꾸지 않는다.',
             '모든 영상에 같은 채점을 적용한다. 판단 근거의 한계 메모는 점수·분모를 바꾸지 않는다.',
             'gated 분류 정확도는 호출 대상 영상만, 확인 대상 포착률은 전체 확인 대상 기준이다.', '',
             '| 모델 | 방식 | 분류 정답 | 확인 대상 포착 | 정상으로 놓침 | 미판정 | 정상의 불필요한 확인 |',
             '|---|---|---|---|---|---|---|']

    def fmt(value):
        n, d = value['numerator'], value['denominator']
        return f'{n}/{d} ({100*n/d:.1f}%)' if d else '대상 없음'

    notes = []

    def plain(value):
        return (str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('|', '\\|').replace('\n', ' ').replace('\r', ' '))

    for model, mode, result in measurements:
        check = result['checking']
        # Escape model names, which may originate outside this repository.
        model = plain(model)
        values = [model, mode, fmt(result['classification']), fmt(check['caught']),
                  fmt(check['missed_as_normal']), fmt(check['unresolved']), fmt(check['unnecessary'])]
        lines.append('| ' + ' | '.join(values) + ' |')
        for row in result['rows']:
            if row.get('judgment_note'):
                actual = NAMES.get(row['assessment'], row['outcome'])
                verdict = ('미호출(분류 채점 대상 아님)' if row['outcome'] == 'not_called'
                           else '정답' if row['correct'] else '오답')
                kind = '근거 제한' if row.get('judgment_evidence_limited') else '판단 메모'
                notes.append(f'- {model} / {plain(mode)} / {plain(row["case_id"])}: '
                             f'정답 {NAMES[row["label"]]} → 예측 {actual} · {verdict}. '
                             f'{kind}: {plain(row["judgment_note"])}')
    if notes:
        lines += ['', '### 사람이 기록한 판단 근거', '',
                  '메모가 없는 영상의 근거가 모두 확실하다는 뜻은 아니다.', '', *notes]
    return lines + ['', '모델 순위·가중 종합 점수는 계산하지 않는다. 입력 관측 길이가 달라 full/gated 차이를 모델 개선으로 단정하지 않는다.', '']
