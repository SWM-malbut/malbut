"""Offline regression tests: no keys, network, model calls, or response repair."""

import base64
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import evaluate_paid_vlm as cli  # noqa: E402
from paid_vlm import inputs, metrics, prompts, providers  # noqa: E402


@pytest.mark.parametrize('profile,expected', [
    ('runtime_crosscheck_with_findings',
     '50b013dd71b6a836c89641c231028ef0301e69efe93bbd526314e42996e277cd'),
    ('explicit_json_v2',
     'a1e39ae63d34ded281f70f75b34af3333cd12cc6eef2c6d24b6b32ec9a9f5f37'),
])
def test_completed_experiment_prompts_are_unchanged(profile, expected):
    assert hashlib.sha256(inputs.system_prompt(profile).encode()).hexdigest() == expected


def test_v3_only_appends_coordinate_instructions():
    old = inputs.system_prompt('explicit_json_v2')
    new = inputs.system_prompt('normalized_boxes_v3')
    assert new == old + prompts.NORMALIZED_BOX_CHECKLIST
    for field in ('box[0] = left', 'box[1] = top', 'box[2] = right', 'box[3] = bottom'):
        assert field in new
    assert '0.0 <= left < right <= 1.0 AND 0.0 <= top < bottom <= 1.0' in new
    assert 'regions: []' in new and 'keep that person\'s assessment and kind' in new
    with pytest.raises(ValueError, match='unknown prompt'):
        inputs.system_prompt('normalized_boxes_typo')


@pytest.mark.parametrize('mid,system_field', [
    ('gemma4:31b', 'messages'), ('gemini-3.8-flash', 'systemInstruction'),
])
def test_v3_wire_changes_only_system_text(mid, system_field):
    common = dict(system=inputs.system_prompt('explicit_json_v2'),
                  text='same neutral metadata', images=['aGVsbG8=', 'd29ybGQ='])
    old = providers.payload(providers.MODELS[mid], common, 'low_reasoning')
    new = providers.payload(providers.MODELS[mid],
                            dict(common, system=inputs.system_prompt('normalized_boxes_v3')),
                            'low_reasoning')
    if system_field == 'messages':
        assert new['messages'][0]['content'] == inputs.system_prompt('normalized_boxes_v3')
        new['messages'][0]['content'] = old['messages'][0]['content']
    else:
        assert new['systemInstruction']['parts'][0]['text'] == inputs.system_prompt('normalized_boxes_v3')
        new['systemInstruction'] = old['systemInstruction']
    assert new == old  # Images, temperature/reasoning, limits and provider options unchanged.


def response(regions):
    return json.dumps(dict(assessment='suspected_fall', explanation='바닥에 누운 모습만 보임',
                           findings=[dict(assessment='suspected_fall', kind='already_down',
                                          regions=regions)]))


@pytest.mark.parametrize('box,code', [
    ([0.44, 145, 0.58, 482], 'invalid_box_range'),
    ([604, 336, 658, 423], 'invalid_box_range'),
    ([0.198, 0.000049, 0.672, 6.42], 'invalid_box_range'),
    ([0.7, 0.32, 0.54, 0.55], 'invalid_box_extent'),
    ([0.395, 0.82, 0.603, 0.536], 'invalid_box_extent'),
    ([0.1, 0.2, 0.1, 0.8], 'invalid_box_extent'),
    ([-0.01, 0.2, 0.6, 0.8], 'invalid_box_range'),
    ([0.1, 0.2, 1.01, 0.8], 'invalid_box_range'),
    ([True, 0.2, 0.6, 0.8], 'invalid_box_range'),
])
def test_coordinate_failures_are_not_repaired(box, code):
    original = response([dict(frame_index=0, box=box)])
    checked = inputs.assess(original, 12)
    assert checked['outcome'] == 'invalid_response' and checked['label'] is None
    assert checked['reported_assessment'] == 'suspected_fall'
    assert checked['response_issue_codes'] == [code]
    assert json.loads(original)['findings'][0]['regions'][0]['box'] == box


def scored(text, cid):
    return dict(inputs.assess(text, 12), case_id=cid, elapsed_s=1, cost_estimate_usd='0')


def test_empty_regions_are_visible_without_changing_scores():
    rows = [scored(response([]), 'empty'),
            scored(response([dict(frame_index=0, box=[0.1, 0.2, 0.6, 0.8]),
                             dict(frame_index=11, box=[0.2, 0.3, 0.7, 0.9])]), 'located'),
            scored(json.dumps(dict(assessment='normal_activity', explanation='일상 행동',
                                   findings=[])), 'normal'),
            scored('bad JSON', 'invalid')]
    labels = dict(empty='suspected_fall', located='suspected_fall', normal='normal_activity',
                  invalid='suspected_fall')
    stats = metrics.summarize(rows, labels)
    assert stats['accuracy']['correct'] == stats['video_label_accuracy']['correct'] == 3
    assert stats['localization_counts'] == dict(
        responses_with_counts=3, responses_without_counts=1, responses_with_empty_regions=1,
        positive_responses_without_regions=1, findings=2, findings_with_empty_regions=1,
        findings_with_regions=1, regions=2)
    assert rows[0]['label'] == 'suspected_fall'  # Empty does not mean normal.


def test_invalid_location_still_counts_as_proposed_not_verified():
    row = scored(response([dict(frame_index=0, box=[0.44, 145, 0.58, 482])]), 'bad')
    stats = metrics.summarize([row], {'bad': 'suspected_fall'})
    assert stats['localization_counts']['regions'] == 1
    assert stats['accuracy']['correct'] == 0 and stats['video_label_accuracy']['correct'] == 1
    assert stats['response_issue_counts'] == {'invalid_box_range': 1}


@pytest.mark.parametrize('findings', [None, {}, [{'regions': None}], [{}]])
def test_missing_or_malformed_locations_are_not_counted_as_empty(findings):
    text = json.dumps(dict(assessment='suspected_fall', explanation='불확실', findings=findings))
    checked = inputs.assess(text, 12)
    assert checked['outcome'] == 'invalid_response'
    assert checked['localization_counts'] is None


def parent_bundle(directory, count=2):
    directory.mkdir()
    ids = [f'C{i:03d}' for i in range(count)]
    common = dict(system=inputs.system_prompt('explicit_json_v2'),
                  text='neutral metadata', images=['aGVsbG8=', 'd29ybGQ='])
    evidence = dict(window_s=5.0, jpeg_sha256=[hashlib.sha256(base64.b64decode(im)).hexdigest()
                                             for im in common['images']])
    for cid in ids:
        inputs.save(directory/f'{cid}.input.json', dict(common=common, evidence=evidence))
    manifest = dict(version=inputs.VERSION, mode='standalone',
                    scope='full84' if count == 84 else 'pilot', case_ids=ids,
                    labels={cid: 'normal_activity' for cid in ids}, sources={'old': 'unchanged'},
                    condition={'prompt': 'explicit_json_v2'},
                    files={f'{cid}.input.json': inputs.sha(directory/f'{cid}.input.json') for cid in ids})
    inputs.save(directory/'manifest.json', manifest)
    inputs.save(directory/'manifest_hash.json', dict(sha256=inputs.digest(manifest)))
    return manifest, common


def test_derive_subset_preserves_images_labels_and_parent(tmp_path):
    source = tmp_path/'source'
    original, common = parent_bundle(source, count=84)
    before = {p.name: inputs.sha(p) for p in source.iterdir()}
    manifest, data = inputs.derive_bundle(source, tmp_path/'v3', 'normalized_boxes_v3', ['C001'])
    assert manifest['scope'] == 'pilot' and manifest['case_ids'] == ['C001']
    assert manifest['labels'] == {'C001': original['labels']['C001']}
    assert manifest['parent_manifest_sha256'] == inputs.digest(original)
    assert manifest['parent_sources'] == original['sources']
    assert data['C001']['images'] == common['images'] and data['C001']['text'] == common['text']
    assert data['C001']['system'] == inputs.system_prompt('normalized_boxes_v3')
    assert {p.name: inputs.sha(p) for p in source.iterdir()} == before
    with pytest.raises(FileExistsError):
        inputs.derive_bundle(source, tmp_path/'v3', 'normalized_boxes_v3', ['C001'])
    with pytest.raises(ValueError, match='changed parent input'):
        (source/'C001.input.json').write_text('{}')
        inputs.derive_bundle(source, tmp_path/'corrupt', 'normalized_boxes_v3', ['C001'])
    assert not (tmp_path/'corrupt').exists()


def test_cli_derive_and_dry_run_never_execute(tmp_path, monkeypatch, capsys):
    source, output = tmp_path/'source', tmp_path/'derived'
    parent_bundle(source)
    def forbidden(*args, **kwargs):
        pytest.fail('offline command attempted live execution')
    monkeypatch.setattr(cli, 'execute', forbidden)
    monkeypatch.setattr(providers, 'headers', forbidden)  # No API credential validation needed.
    monkeypatch.setattr(sys, 'argv', ['evaluate_paid_vlm.py', 'derive', '--inputs', str(source),
        '--output', str(output), '--prompt', 'normalized_boxes_v3', '--cases', 'C001'])
    cli.main()
    assert json.loads(capsys.readouterr().out) == dict(
        status='DERIVED_OFFLINE', scope='pilot', cases=1, prompt='normalized_boxes_v3', uploads=0)
    monkeypatch.setattr(sys, 'argv', ['evaluate_paid_vlm.py', 'run', '--inputs', str(output),
        '--models', 'gemma4:31b', 'gemini-3.8-flash', '--model-profile', 'low_reasoning'])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'DRY_RUN' and report['uploads'] == 0
    assert report['planned_calls'] == 2 and report['model_profile'] == 'low_reasoning'
    assert report['condition']['prompt'] == 'normalized_boxes_v3'
