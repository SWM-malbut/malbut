"""Versioned offline VLM observation contract and auditable decision policy.

The observations are *model claims*, not independently validated ground truth.
No case IDs, labels, prior VLM labels or detector results enter the prompt.
"""
import copy
import json
import re

from fall_evaluation_v2 import LABELS, SYSTEM_PROMPT as BASE_SYSTEM
from replay_vlm_frames import USER_TEMPLATE, canonical, digest


VERSION = 'fall-observed-evidence-v1'
KINDS = (
    'foot_slip', 'support_lost', 'failed_bracing', 'uncontrolled_descent',
    'controlled_lowering', 'ordinary_activity', 'resting_context', 'lying_state',
    'motion_speed', 'no_person', 'other',
)
FALL_KINDS = frozenset(KINDS[:4])
SYSTEM = BASE_SYSTEM + (
    '\n먼저 보이는 사실을 observations에 쓰고 해석과 label을 구분하세요. '
    '각 사실에는 실제 제공된 이미지의 1부터 시작하는 순서 번호를 붙이세요. '
    'first_image/last_image는 영상 프레임 번호나 초가 아닙니다. '
    '관찰한 사실을 최대 4개만 짧게 적으세요. 관찰할 수 없는 장면은 만들지 마세요. '
    '특정 행동이 이미지 사이에 일어났다고 추측한 것은 clarity=unclear입니다. '
    'motion_speed(빠른 움직임)와 lying_state(누운 자세)만으로 '
    'uncontrolled_descent(몸을 제어하지 못하며 내려감)로 바꾸어 쓰지 마세요. '
    '몸을 제어하지 못한 모습이 보인다면 어떤 신체 부위나 지지 관계가 어떻게 '
    '변했는지 fact_ko에 쓰세요. 빠르다는 말이나 낙상이라는 결론을 반복하지 마세요. '
    '발이 실제로 미끄러지는 모습, 몸을 받치던 곳에서 벗어나 떨어짐, '
    '손으로 받치려다 버티지 못하는 모습 등 보이는 근거를 사용하세요. '
    '이 예시 중 특정 동작이나 여러 개의 단서를 반드시 요구하는 것은 아닙니다. '
    '이미지 간격 때문에 제어 여부를 알 수 없다면 loss_of_control=unclear입니다. '
    '자발적으로 눕거나 앉는 해석도 그럴듯하면 voluntary_action=plausible로 쓰고 '
    '낙상 의심으로 분류하세요. 의도가 보이지 않으면 추측하지 마세요. '
    'voluntary_action=not_supported는 의도적으로 한 행동이라는 근거가 없다는 뜻이며 '
    '그 자체가 낙상 증거는 아닙니다. 이미 누운 상태도 휴식 맥락이 뚜렷하면 정상입니다. '
    '잠자는 사람이 움직이지 않는다는 이유만으로 의심하지 마세요. '
    '모든 칸을 채우려고 근거를 만들지 말고 불확실함을 그대로 기록하세요. '
    '전체 출력은 1000토큰 이내의 JSON 객체 하나만 쓰세요.'
)


def enum(values):
    return {'type': 'string', 'enum': list(values)}


SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['outcome', 'label', 'observations', 'interpretation', 'explanation_ko'],
    'properties': {
        'outcome': enum(('classified', 'unobservable')),
        'label': {'type': ['string', 'null'], 'enum': [*LABELS, None]},
        'observations': {
            'type': 'array', 'maxItems': 4, 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['first_image', 'last_image', 'kind', 'clarity', 'fact_ko'],
                'properties': {
                    'first_image': {'type': 'integer', 'minimum': 1, 'maximum': 12},
                    'last_image': {'type': 'integer', 'minimum': 1, 'maximum': 12},
                    'kind': enum(KINDS), 'clarity': enum(('clear', 'unclear')),
                    'fact_ko': {'type': 'string', 'minLength': 1, 'maxLength': 500}}}},
        'interpretation': {
            'type': 'object', 'additionalProperties': False,
            'required': ['person_present', 'fall_process', 'loss_of_control', 'voluntary_action'],
            'properties': {
                'person_present': enum(('yes', 'no', 'unclear')),
                'fall_process': enum(('clear', 'unclear', 'not_seen')),
                'loss_of_control': enum(('clear', 'unclear', 'not_seen')),
                'voluntary_action': enum(('clear', 'plausible', 'unclear', 'not_supported'))}},
        'explanation_ko': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
    },
}
USER_SUFFIX = (
    '\nobservations의 이미지 번호는 위 시각 목록 순서의 1~{count}입니다. '
    '번호가 없는 이미지나 뒤에 일어날 동작을 근거로 쓰지 마세요.'
)
PROMPT_SHA256 = digest(dict(version=VERSION, system=SYSTEM, user=USER_TEMPLATE,
                            user_suffix=USER_SUFFIX, schema=SCHEMA))


def payload(model, images, frames, duration, options, thinking):
    if not 1 <= len(frames) == len(images) <= 12:
        raise ValueError('invalid image count')
    text = USER_TEMPLATE.format(
        count=len(frames), duration=duration,
        timestamps=', '.join(f'{f["timestamp_s"]:.3f}' for f in frames))
    text += USER_SUFFIX.format(count=len(frames))
    text += '\n출력 형식(JSON Schema):\n'+canonical(SCHEMA).decode()
    result = dict(model=model, stream=False, options=copy.deepcopy(options), messages=[
        dict(role='system', content=SYSTEM), dict(role='user', content=text, images=images)])
    if thinking == 'disabled':
        result['think'] = False
    return result


def schema_errors(value, schema, path='$'):
    """Validate only the bounded keywords used in the local schema, with stdlib."""
    types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    names = {dict: 'object', list: 'array', str: 'string', int: 'integer', type(None): 'null'}
    if names.get(type(value)) not in types:
        return [path+': type']
    if 'enum' in schema and value not in schema['enum']:
        return [path+': enum']
    errors = []
    if isinstance(value, dict):
        if set(value) != set(schema['required']):
            return [path+': fields']
        for k, v in value.items():
            errors.extend(schema_errors(v, schema['properties'][k], path+'.'+k))
    elif isinstance(value, list):
        if len(value) > schema['maxItems']:
            errors.append(path+': maxItems')
        for i, v in enumerate(value):
            errors.extend(schema_errors(v, schema['items'], f'{path}[{i}]'))
    elif isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 10000):
            errors.append(path+': length')
    elif type(value) is int and not schema['minimum'] <= value <= schema['maximum']:
        errors.append(path+': bounds')
    return errors


def validate(value, image_count):
    """Strict types/keys, bounded references, no autocorrection of model content."""
    errors = schema_errors(value, SCHEMA)
    if errors:
        return errors
    if (value['outcome'] == 'classified') != (value['label'] in LABELS):
        errors.append('outcome_label_mismatch')
    if not value['explanation_ko'].strip():
        errors.append('empty_explanation')
    for o in value['observations']:
        if (type(o['first_image']) is not int or type(o['last_image']) is not int
                or not 1 <= o['first_image'] <= o['last_image'] <= image_count):
            errors.append('observation_references_unavailable_images')
        if not o['fact_ko'].strip():
            errors.append('empty_observation')
    return errors


def parse(response, image_count, *, remove_outer_fence=False):
    message = response.get('message') if isinstance(response, dict) else None
    text = message.get('content') if isinstance(message, dict) else None
    errors, value, removed = [], None, False
    if not isinstance(response, dict) or response.get('done') is not True or response.get(
            'done_reason') != 'stop':
        errors.append('incomplete_response')
    if isinstance(text, str) and remove_outer_fence:
        m = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', text.strip())
        if m:
            text, removed = m.group(1), True

    def unique(pairs):
        result = {}
        for key, val in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = val
        return result

    def constant(v):
        raise ValueError('nonfinite JSON')

    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=constant)
        errors.extend(validate(value, image_count))
    except (ValueError, TypeError, RecursionError):
        errors.append('invalid_final_json')
    return dict(valid=not errors, evidence=value, schema_errors=errors,
                semantic_errors=[], removed_outer_fence=removed)


def project(assessment, *, apply_policy=False):
    """Project into unchanged v2 scoring; never use ground truth or prior labels.

    The policy cannot promote a model label to observed_fall. It keeps unsupported
    or contradictory fall/normal assertions in the suspected_fall category.
    """
    errors = assessment['schema_errors']
    if not assessment['valid']:
        return dict(valid=False, prediction=None, schema_errors=errors,
                    semantic_errors=[], policy_reasons=[])
    v = assessment['evidence']
    label, reasons = v['label'], []
    if apply_policy and v['outcome'] == 'classified':
        i = v['interpretation']
        specific = [o for o in v['observations']
                    if o['kind'] in FALL_KINDS and o['clarity'] == 'clear'
                    and o['first_image'] < o['last_image']]
        if label == 'observed_fall':
            if i['person_present'] != 'yes':
                reasons.append('person_not_clearly_observed')
            if i['fall_process'] != 'clear':
                reasons.append('fall_process_not_clear')
            if i['loss_of_control'] != 'clear':
                reasons.append('loss_of_control_not_clear')
            if i['voluntary_action'] != 'not_supported':
                reasons.append('voluntary_action_not_ruled_out_by_model')
            if not specific:
                reasons.append('no_clear_specific_motion_evidence')
            if reasons:
                label = 'suspected_fall'
        elif label == 'normal_activity' and (
                specific or i['fall_process'] == 'clear' or i['loss_of_control'] == 'clear'):
            reasons.append('normal_label_conflicts_with_claimed_fall_evidence')
            label = 'suspected_fall'
    return dict(valid=True, prediction=dict(outcome=v['outcome'], label=label,
                                            explanation_ko=v['explanation_ko']),
                schema_errors=[], semantic_errors=[], policy_reasons=reasons,
                original_model_label=v['label'])
