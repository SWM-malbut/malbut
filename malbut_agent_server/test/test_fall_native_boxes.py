"""Live-provider native box contract without credentials or remote inference."""

import asyncio
from copy import deepcopy
import json

import pytest

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (
    CROSSCHECK_NATIVE_SYSTEM_PROMPT, LEGACY_BOX_FORMAT, NATIVE_BOX_FORMAT,
    OllamaCloudFallProvider, build_payload, parse_reply,
)
from malbut_agent_server.domain.fall_monitoring import VideoAssessment
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError
from test_fall_cloud_association import cross_request
from test_ollama_cloud_fall import request, response


def native_scene():
    return dict(assessment='suspected_fall', explanation='바닥에 있는 사람', findings=[
        dict(assessment='suspected_fall', kind='already_down', regions=[
            dict(frame_index=0, box_2d=[200, 100, 800, 600]),
            dict(frame_index=1, box_2d=[200, 100, 800, 600])])])


def parse(value):
    return parse_reply(response(json.dumps(value)), cross_request(),
                       box_format=NATIVE_BOX_FORMAT)


def test_live_provider_requests_native_and_returns_existing_domain_contract():
    async def run():
        provider = OllamaCloudFallProvider(model='gemma4:31b', api_key='test-only')
        sent = []

        async def post(body):
            sent.append(json.loads(body))
            return response(json.dumps(native_scene()))

        provider._post = post
        result = await provider.analyze(cross_request())
        assert sent[0]['messages'][0]['content'] == CROSSCHECK_NATIVE_SYSTEM_PROMPT
        assert result.findings[0].regions[0].box == (.1, .2, .6, .8)
        assert not result.localization_failed
        assert 'box_2d' not in repr(result)
        assert len(sent[0]['messages'][1]['images']) == 2
        assert sent[0]['options']['num_predict'] == 2048
        assert sent[0]['model'] == 'gemma4:31b'
        assert provider.timeout_s == 20

    asyncio.run(run())


@pytest.mark.parametrize('bad', [
    [-1, 100, 800, 600], [200, 100, 1001, 600], [800, 100, 200, 600],
    [200, 600, 800, 100], [200, 100, 200, 600], [200, 100, 800],
    [200.0, 100, 800, 600], [.2, .1, .8, .6], [True, 100, 800, 600],
    ['200', 100, 800, 600], [None, 100, 800, 600], {'top': 200},
])
def test_bad_box_keeps_suspicion_but_discards_all_locations(bad):
    value = native_scene()
    value['findings'][0]['regions'][1]['box_2d'] = bad
    before = deepcopy(value)
    result = parse(value)
    assert result.assessment is VideoAssessment.SUSPECTED_FALL
    assert result.findings == () and result.localization_failed
    assert value == before  # No clamping, axis swapping or response mutation.


@pytest.mark.parametrize('index', [2, -1, True, 0, .5, '1'])
def test_frame_contract_is_still_checked(index):
    value = native_scene()
    value['findings'][0]['regions'][1]['frame_index'] = index
    assert parse(value).localization_failed


def test_mixed_wire_formats_are_not_auto_detected():
    value = native_scene()
    value['findings'][0]['regions'][1] = dict(frame_index=1, box=[.1, .2, .6, .8])
    assert parse(value).localization_failed
    value = native_scene()
    value['findings'][0]['regions'][1]['box'] = [.1, .2, .6, .8]
    assert parse(value).localization_failed
    # Legacy decoder does not reinterpret native output either.
    assert parse_reply(response(json.dumps(native_scene())),
                       cross_request()).localization_failed


@pytest.mark.parametrize('box,expected', [
    ([0, 0, 1000, 1000], (0., 0., 1., 1.)),
    ([0, 0, 1, 1], (0., 0., .001, .001)),
])
def test_integer_grid_is_not_scale_guessed(box, expected):
    value = native_scene()
    for region in value['findings'][0]['regions']:
        region['box_2d'] = box
    assert parse(value).findings[0].regions[0].box == expected


def test_normal_with_positive_findings_is_not_accepted():
    value = native_scene()
    value['assessment'] = 'normal_activity'
    with pytest.raises(CloudFallProviderError, match='cloud_invalid_response'):
        parse(value)
    value['findings'] = []
    assert parse(value).assessment is VideoAssessment.NORMAL_ACTIVITY


def test_localization_unavailable_does_not_erase_positive_evidence():
    value = native_scene()
    value['findings'][0]['regions'] = []
    assert parse(value).findings[0].assessment is VideoAssessment.SUSPECTED_FALL


def test_malformed_json_is_not_repaired_and_single_outer_fence_still_works():
    text = json.dumps(native_scene())
    for bad in (text + text, text + '\nCorrection: ' + text,
                text.replace('200', 'NaN'), text.replace('200', 'Infinity'),
                text.replace('"frame_index": 0', '"frame_index": 0, "frame_index": 1')):
        with pytest.raises(CloudFallProviderError, match='cloud_invalid_response'):
            parse_reply(response(bad), cross_request(), box_format=NATIVE_BOX_FORMAT)
    result = parse_reply(response('```json\n' + text + '\n```'), cross_request(),
                         box_format=NATIVE_BOX_FORMAT)
    assert result.findings[0].regions[0].box == (.1, .2, .6, .8)


def test_incident_payload_and_legacy_replay_defaults_are_unchanged():
    for req in (request(),):
        assert build_payload(req, model='gemma4:31b', box_format=NATIVE_BOX_FORMAT) == (
            build_payload(req, model='gemma4:31b', box_format=LEGACY_BOX_FORMAT))
    old = json.loads(build_payload(cross_request(), model='gemma4:31b'))
    assert 'and box (normalized left,top,right,bottom).' in old['messages'][0]['content']
    with pytest.raises(ValueError, match='unsupported box format'):
        OllamaCloudFallProvider(model='gemma4:31b', api_key='test-only', box_format='auto')
