"""Evidence assertions are validated, not silently repaired or treated as GT."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import fall_evidence_decision as d  # noqa: E402


def value(label='observed_fall'):
    return dict(
        outcome='classified', label=label,
        observations=[dict(first_image=2, last_image=4, kind='support_lost',
                           clarity='clear', fact_ko='몸이 침대 가장자리를 벗어나 아래로 내려간다.')],
        interpretation=dict(person_present='yes', fall_process='clear',
                            loss_of_control='clear', voluntary_action='not_supported'),
        explanation_ko='떨어지는 과정이 보임.')


def response(v=None):
    return dict(done=True, done_reason='stop', message=dict(content=json.dumps(
        value() if v is None else v, ensure_ascii=False)))


def apply(v):
    return d.project(d.parse(response(v), 12), apply_policy=True)


def test_clear_fall_from_lying_survives_without_standing_or_multiple_cues():
    v = value()
    before = copy.deepcopy(v)
    out = apply(v)
    assert v == before and out['valid']
    assert out['prediction']['label'] == 'observed_fall'
    assert out['policy_reasons'] == []


@pytest.mark.parametrize('field,val', [
    ('person_present', 'unclear'), ('fall_process', 'unclear'),
    ('fall_process', 'not_seen'), ('loss_of_control', 'unclear'),
    ('loss_of_control', 'not_seen'), ('voluntary_action', 'clear'),
    ('voluntary_action', 'plausible'), ('voluntary_action', 'unclear'),
])
def test_unsupported_fall_stays_suspected(field, val):
    v = value()
    v['interpretation'][field] = val
    p = d.parse(response(v), 12)
    assert d.project(p)['prediction']['label'] == 'observed_fall'
    after = d.project(p, apply_policy=True)
    assert after['prediction']['label'] == 'suspected_fall'
    assert after['policy_reasons']


@pytest.mark.parametrize('kind', ['motion_speed', 'lying_state', 'other', 'resting_context'])
def test_fast_or_lying_alone_is_not_confirmed_fall(kind):
    v = value()
    v['observations'][0]['kind'] = kind
    assert apply(v)['prediction']['label'] == 'suspected_fall'


@pytest.mark.parametrize('change', ['single_image', 'unclear', 'no_observations'])
def test_interpolated_or_missing_motion_not_clear(change):
    v = value()
    if change == 'single_image':
        v['observations'][0]['first_image'] = 4
    elif change == 'unclear':
        v['observations'][0]['clarity'] = 'unclear'
    else:
        v['observations'] = []
    assert apply(v)['prediction']['label'] == 'suspected_fall'


def test_never_promotes_suspected_to_confirmed_fall():
    assert apply(value('suspected_fall'))['prediction']['label'] == 'suspected_fall'


def test_normal_conflicting_with_clear_fall_does_not_fail_open():
    assert apply(value('normal_activity'))['prediction']['label'] == 'suspected_fall'


@pytest.mark.parametrize('kind,present,voluntary', [
    ('controlled_lowering', 'yes', 'clear'), ('resting_context', 'yes', 'clear'),
    ('ordinary_activity', 'yes', 'clear'), ('no_person', 'no', 'not_supported'),
])
def test_normal_sleep_controlled_motion_and_no_people_remain_normal(kind, present, voluntary):
    v = value('normal_activity')
    v['observations'][0]['kind'] = kind
    v['interpretation'] = dict(person_present=present, fall_process='not_seen',
                               loss_of_control='not_seen', voluntary_action=voluntary)
    assert apply(v)['prediction']['label'] == 'normal_activity'


def test_unobservable_is_not_normal_or_suspected():
    v = value()
    v.update(outcome='unobservable', label=None, observations=[])
    out = apply(v)
    assert out['valid'] and out['prediction']['label'] is None
    assert out['prediction']['outcome'] == 'unobservable'


@pytest.mark.parametrize('change', [
    'future', 'reverse', 'float', 'bool', 'unknown_key', 'unknown_kind', 'empty_fact',
    'empty_explanation', 'too_many', 'bad_label', 'outcome_label', 'wrong_type',
])
def test_invalid_structure_never_produces_normal_or_suspected(change):
    v = value()
    o = v['observations'][0]
    if change == 'future':
        o['last_image'] = 8
    elif change == 'reverse':
        o['first_image'] = 5
    elif change == 'float':
        o['first_image'] = 2.0
    elif change == 'bool':
        o['first_image'] = True
    elif change == 'unknown_key':
        v['extra'] = 'x'
    elif change == 'unknown_kind':
        o['kind'] = 'made_up'
    elif change == 'empty_fact':
        o['fact_ko'] = ' '
    elif change == 'empty_explanation':
        v['explanation_ko'] = ' '
    elif change == 'too_many':
        v['observations'] = [o]*5
    elif change == 'bad_label':
        v['label'] = 'confirmed_fall'
    elif change == 'outcome_label':
        v['outcome'] = 'unobservable'
    else:
        v['interpretation'] = []
    out = d.project(d.parse(response(v), 6), apply_policy=True)
    assert not out['valid'] and out['prediction'] is None


@pytest.mark.parametrize('raw', [None, [], {}, {'message': []}, {'message': None},
                                 {'message': {'content': None}}])
def test_malformed_envelope_does_not_raise(raw):
    assert not d.parse(raw, 12, remove_outer_fence=True)['valid']


def test_only_complete_outer_fence_is_allowed_as_separate_score():
    raw = response()
    raw['message']['content'] = '```json\n'+raw['message']['content']+'\n```'
    assert not d.parse(raw, 12)['valid']
    assert d.parse(raw, 12, remove_outer_fence=True)['valid']
    raw['message']['content'] = 'Here is the result:\n'+raw['message']['content']
    assert not d.parse(raw, 12, remove_outer_fence=True)['valid']


@pytest.mark.parametrize('change', ['duplicate', 'truncated', 'nan'])
def test_json_not_repaired(change):
    raw = response()
    if change == 'duplicate':
        raw['message']['content'] = raw['message']['content'].replace(
            '"outcome":', '"outcome":"classified", "outcome":', 1)
    elif change == 'truncated':
        raw['done_reason'] = 'length'
    else:
        raw['message']['content'] = raw['message']['content'].replace('"first_image": 2',
                                                                      '"first_image": NaN')
    assert not d.parse(raw, 12)['valid']


def test_prompt_has_no_prior_answers_case_ids_or_detector_evidence():
    frames = [dict(timestamp_s=x) for x in (0, .5, 1)]
    out = d.payload('gemma4:31b-cloud', ['A', 'B', 'C'], frames, 1.1,
                    dict(temperature=0), 'disabled')
    assert out['think'] is False and out['messages'][1]['images'] == ['A', 'B', 'C']
    assert set(out) == {'model', 'stream', 'options', 'messages', 'think'}
    assert '1~3' in out['messages'][1]['content']
    assert 'SYN064' not in json.dumps(out)
    assert d.PROMPT_SHA256 == d.digest(dict(
        version=d.VERSION, system=d.SYSTEM, user=d.USER_TEMPLATE,
        user_suffix=d.USER_SUFFIX, schema=d.SCHEMA))


def test_scoring_does_not_use_full_clip_or_best_answer_to_repair_gated(monkeypatch):
    import replay_fall_evidence_decision as replay
    labels = {'C': dict(label='observed_fall'), 'D': dict(label='normal_activity')}

    def old_read(path):
        if path.name.endswith('.result.json'):
            return dict(request_s=1.0)
        return dict(done=True, done_reason='stop', message=dict(content=json.dumps(dict(
            outcome='classified', label='normal_activity', explanation_ko='old'))))

    monkeypatch.setattr(replay, 'read', old_read)
    items, results = [], {}
    for mode, cid, sequence, prediction in (
            ('gated', 'C', 1, 'observed_fall'), ('gated', 'C', 2, 'normal_activity'),
            ('full', 'C', 1, 'observed_fall'), ('full', 'D', 1, 'normal_activity')):
        key = f'{mode}-{cid}-{sequence}'
        items.append(dict(condition_id=key, mode=mode, case_id=cid,
                          incident_id=cid, sequence=sequence, input_id=key,
                          old_root='/unused', duration_s=1))
        assessed = dict(valid=True, schema_errors=[], semantic_errors=[], prediction=dict(
            outcome='classified', label=prediction, explanation_ko='new'))
        results[key] = dict(status='responded', request_s=1.0,
                            **{name: dict(model=assessed, policy=assessed)
                               for name in ('strict', 'outer_fence_only')})
    out = replay.score_conditions(items, results, labels)
    gated = out['gated']['outer_fence_only']['model']
    full = out['full']['outer_fence_only']['model']
    assert gated['classification'] == dict(numerator=0, denominator=1, rate=0.0)
    assert full['classification'] == dict(numerator=2, denominator=2, rate=1.0)
    assert gated['confusion']['normal_activity'] == {'not_called': 1}


def test_posthoc_identical_duplicate_diagnostic_does_not_change_primary_parser():
    from diagnose_fall_evidence_format import parse_format_only
    raw = response()
    raw['message']['content'] = raw['message']['content'].replace(
        '"label":', '"label":"observed_fall", "label":', 1)
    assert not d.parse(raw, 12)['valid']
    assessed, operations = parse_format_only(raw, 12)
    assert assessed['valid'] and operations == ['identical_duplicate:label']
    assert not d.parse(raw, 12)['valid']


def test_posthoc_conflicting_duplicate_is_rejected():
    from diagnose_fall_evidence_format import parse_format_only
    raw = response()
    raw['message']['content'] = raw['message']['content'].replace(
        '"label":', '"label":"normal_activity", "label":', 1)
    with pytest.raises(ValueError, match='conflicting duplicate'):
        parse_format_only(raw, 12)


def test_posthoc_diagnostic_never_discards_arbitrary_metadata():
    from diagnose_fall_evidence_format import parse_format_only
    v = value()
    v['anything'] = 'unexpected'
    assessed, _ = parse_format_only(response(v), 12)
    assert not assessed['valid']
    del v['anything']
    v['observations_count'] = 200
    with pytest.raises(ValueError, match='incorrect count'):
        parse_format_only(response(v), 12)
