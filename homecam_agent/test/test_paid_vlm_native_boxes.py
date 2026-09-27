"""Offline tests for the explicit native coordinate experiment. No API calls."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from paid_vlm import inputs, native_boxes, providers, runner
from test_paid_vlm_box_prompt import parent_bundle

PROFILE = native_boxes.PROFILE


def reply(box=None, indices=(0, 11)):
    if box is None:
        box = [200, 100, 800, 600]
    return json.dumps(dict(assessment='suspected_fall', explanation='바닥에 누워 있음',
        findings=[dict(assessment='suspected_fall', kind='already_down',
                       regions=[dict(frame_index=i, box_2d=box) for i in indices])]))


def score(text):
    return inputs.assess(text, 12, PROFILE)


def test_native_prompt_has_no_conflicting_unit_or_order():
    new = inputs.system_prompt(PROFILE)
    old = inputs.system_prompt('explicit_json_v2')
    assert new[:new.index('regions has')] == old[:old.index('regions has')]
    assert '"frame_index" and "box_2d"' in new
    assert '[ymin, xmin, ymax, xmax]' in new and 'FOUR INTEGERS' in new
    for conflicting in ('0 through 1', '[left, top, right, bottom]',
                        'normalized left,top,right,bottom', '[0.1, 0.2, 0.6, 0.8]'):
        assert conflicting not in new


def test_fixed_conversion_not_repair_or_default_behavior():
    text = reply()
    checked = score(text)
    assert checked['outcome'] == 'classified'
    assert checked['reported_assessment'] == checked['label'] == 'suspected_fall'
    assert checked['response_issue_codes'] == []
    assert checked['normalized_response']['findings'][0]['regions'][0]['box'] == [.1, .2, .6, .8]
    assert json.loads(text)['findings'][0]['regions'][0]['box_2d'] == [200, 100, 800, 600]
    assert inputs.assess(text, 12)['outcome'] == 'invalid_response'
    assert score(inputs.assess(text, 12).get('explanation') or '{}')['outcome'] == 'invalid_response'


@pytest.mark.parametrize('box,code', [
    ([.44, 145, .58, 482], 'invalid_native_box_type'),
    ([200.0, 100, 800, 600], 'invalid_native_box_type'),
    ([True, 100, 800, 600], 'invalid_native_box_type'),
    (['200', 100, 800, 600], 'invalid_native_box_type'),
    ([None, 100, 800, 600], 'invalid_native_box_type'),
    ([-1, 100, 800, 600], 'invalid_box_range'),
    ([200, 100, 1001, 600], 'invalid_box_range'),
    ([800, 100, 200, 600], 'invalid_box_extent'),
    ([200, 600, 800, 100], 'invalid_box_extent'),
    ([200, 100, 200, 600], 'invalid_box_extent'),
    ([200, 100, 800], 'invalid_box_shape'),
    ({'top': 200}, 'invalid_box_shape'),
])
def test_invalid_native_values_never_repaired(box, code):
    result = score(reply(box))
    assert result['outcome'] == 'invalid_response'
    assert result['reported_assessment'] == 'suspected_fall'
    assert result['normalized_response'] is None
    assert code in result['response_issue_codes']


@pytest.mark.parametrize('box', [[0, 0, 1000, 1000], [0, 0, 1, 1]])
def test_boundaries_and_tiny_integer_boxes_are_not_scale_guessed(box):
    result = score(reply(box))
    assert result['outcome'] == 'classified'
    assert result['normalized_response']['findings'][0]['regions'][0]['box'] == [
        box[1]/1000, box[0]/1000, box[3]/1000, box[2]/1000]


@pytest.mark.parametrize('indices', [(0, 12), (0, -1), (1, 1), (11, 0), (True, 11)])
def test_legacy_index_checks_still_apply(indices):
    assert score(reply(indices=indices))['outcome'] == 'invalid_response'


def test_malformed_json_is_not_extracted_or_repaired():
    for text in (reply() + '\nCorrection:\n' + reply(), reply() + reply(),
                 reply().replace('200', 'NaN'), reply().replace('200', 'Infinity'),
                 reply().replace('"frame_index": 0', '"frame_index": 0, "frame_index": 1')):
        result = score(text)
        assert result['outcome'] == 'invalid_response'
        assert result['normalized_response'] is None
        assert result['reported_assessment'] is None
    assert score('```json\n' + reply() + '\n```')['outcome'] == 'classified'


def test_wrong_keys_and_partial_failure_never_salvaged():
    obj = json.loads(reply())
    obj['findings'][0]['regions'][1]['box_2d'] = [.44, 145, .58, 482]
    assert score(json.dumps(obj))['normalized_response'] is None
    obj = json.loads(reply())
    obj['findings'][0]['regions'][1]['box'] = [.1, .2, .6, .8]
    assert score(json.dumps(obj))['outcome'] == 'invalid_response'
    assert score(reply().replace('box_2d', 'box'))['outcome'] == 'invalid_response'


def test_empty_regions_remain_suspect_and_are_counted():
    result = score(reply(indices=()))
    assert result['outcome'] == 'classified' and result['label'] == 'suspected_fall'
    assert result['localization_counts']['findings_with_empty_regions'] == 1
    assert result['localization_counts']['regions'] == 0


def test_scene_label_kind_and_other_schema_checks_still_apply():
    for text in (reply().replace('"assessment": "suspected_fall"',
                                 '"assessment": "normal_activity"', 1),
                 reply().replace('suspected_fall', 'observed_fall'),
                 reply().replace('"kind": "already_down"', '"kind": "invalid"'),
                 reply().replace('"explanation":', '"extra": 1, "explanation":')):
        assert score(text)['outcome'] == 'invalid_response'
    result = score(json.dumps(dict(assessment='normal_activity', explanation='일상 행동', findings=[])))
    assert result['outcome'] == 'classified' and result['label'] == 'normal_activity'


def test_derive_and_wire_keep_media_and_old_inputs(tmp_path):
    source = tmp_path/'source'
    original, common = parent_bundle(source)
    before = {p.name: inputs.sha(p) for p in source.iterdir()}
    manifest, data = inputs.derive_bundle(source, tmp_path/'native', PROFILE)
    assert manifest['condition']['prompt'] == PROFILE
    old = providers.payload(providers.MODELS['gemma4:31b'], common, 'low_reasoning')
    new = providers.payload(providers.MODELS['gemma4:31b'], data['C000'], 'low_reasoning')
    new['messages'][0]['content'] = old['messages'][0]['content']
    assert old == new
    assert {p.name: inputs.sha(p) for p in source.iterdir()} == before


def test_runner_dispatches_native_contract_and_keeps_original(tmp_path):
    source = tmp_path/'source'
    parent_bundle(source, 1)
    manifest, data = inputs.derive_bundle(source, tmp_path/'native', PROFILE)
    text = reply(indices=(0, 1))
    async def transport(*args):
        return 200, json.dumps(dict(done=True, done_reason='stop', model='gemma4:31b',
            message=dict(role='assistant', content=text),
            prompt_eval_count=100, eval_count=50)).encode()
    rate = dict(currency='USD', source='https://example.test', checked_on='2026-09-26',
                per_million_tokens={k: '1' for k in ('input', 'cached_input', 'cache_write_5m',
                                                     'cache_write_1h', 'output')})
    plan = runner.make_plan(manifest, data, ['gemma4:31b'])
    report = asyncio.run(runner.execute(plan, manifest, data, tmp_path/'run', {'gemma4:31b': rate},
        approved_upload=True, budget_usd='0.1', request_reserve_usd='0.01', max_calls=1,
        credentials={providers.MODELS['gemma4:31b'].key_env: 'fixture'}, transport=transport))
    assert report['completed']
    row = json.loads((tmp_path/'run/00001.result.json').read_text())
    assert row['outcome'] == 'classified' and row['text'] == text
    assert row['normalized_response']['findings'][0]['regions'][0]['box'] == [.1, .2, .6, .8]
    with pytest.raises(ValueError, match='profile mismatch'):
        asyncio.run(runner.invoke(providers.MODELS['gemma4:31b'], data['C000'], 'fixture', rate,
                                  transport=transport))
