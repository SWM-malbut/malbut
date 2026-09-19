"""Supplemental output-format diagnosis cannot rewrite evidence or decisions."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from diagnose_fall84_json_fences import diagnose
from replay_vlm_frames import assess_response

PRED = dict(outcome='classified', label='suspected_fall', explanation_ko='과정을 볼 수 없습니다.')


def raw(text):
    return dict(done=True, done_reason='stop', message=dict(content=text))


@pytest.mark.parametrize('opening', ['```json', '```'])
def test_exact_outer_fence_only(opening):
    response = raw(opening + '\n' + json.dumps(PRED) + '\n```')
    before = copy.deepcopy(response)
    assert not assess_response(response, 1., 'v2')['valid']
    result = diagnose(response, 1.)
    assert result['removed_outer_fence'] and result['assessment']['valid']
    assert result['assessment']['prediction'] == PRED
    assert response == before


@pytest.mark.parametrize('text', [
    'Answer:\n```json\n{}\n```', '```json\n{}\n```\nmore text',
    '```json\n{}\n', '```json\n{}\n```\n```json\n{}\n```',
    '```json\n{"label": "fall"}\n```',
])
def test_no_prose_extraction_or_repair(text):
    assert not diagnose(raw(text), 1.)['assessment']['valid']


def test_truncated_done_cannot_be_fixed():
    response = raw('```json\n' + json.dumps(PRED) + '\n```')
    response['done_reason'] = 'length'
    assert not diagnose(response, 1.)['assessment']['valid']


def test_valid_raw_unchanged():
    response = raw(json.dumps(PRED))
    result = diagnose(response, 1.)
    assert not result['removed_outer_fence']
    assert result['assessment'] == assess_response(response, 1., 'v2')
