"""Offline tests for replaceable VLM adapters and fall safety policy."""

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from malbut_agent_server.adapters.outbound.bedrock_nova_vlm import (
    BedrockNovaVlmProvider,
)
from malbut_agent_server.adapters.outbound.gemini_vlm import GeminiVlmProvider
from malbut_agent_server.adapters.outbound.openai_compatible_vlm import (
    OpenAiCompatibleVlmProvider,
)
from malbut_agent_server.application.vlm_analysis import FallVlmAnalysisService
from malbut_agent_server.domain.vlm import (
    AuroraDepthEvidence,
    FallAssessment,
    FallCandidateKind,
    PhysicalFallEvidence,
    ResponseState,
    VlmAnalysisRequest,
    VlmMedia,
    VlmProviderResult,
)
from malbut_agent_server.ports.vlm_provider import VlmProvider
from malbut_agent_server.ports.vlm_provider import VlmProviderError
from malbut_agent_server.vlm_factory import (
    VlmSettings,
    create_vlm_provider,
)
from malbut_agent_server.vlm_eval_prompt import provider_prediction_schema
from malbut_agent_server.vlm_inference_runner import (
    generate_prediction_records,
    load_observations,
    private_jsonl_journal,
    write_private_jsonl,
)
from malbut_agent_server.vlm_eval_schema import (
    load_prediction_records,
    load_vlm_cases,
)


def _prediction(assessment: str = 'confirmed_fall') -> Dict[str, Any]:
    fall_event = assessment == 'confirmed_fall'
    person = assessment in {'confirmed_fall', 'found_down', 'normal_activity'}
    return {
        'subjects': {'person': int(person), 'pet': 0, 'other': 0},
        'events': (
            [
                {
                    'type': 'fall',
                    'subject': 'person',
                    'start_s': 1.0,
                    'end_s': 2.0,
                    'confidence': 0.9,
                }
            ]
            if fall_event
            else []
        ),
        'fall': {
            'assessment': assessment,
            'confidence': 0.9 if assessment == 'confirmed_fall' else 0.1,
            'recovery': (
                'not_recovered'
                if assessment in {'confirmed_fall', 'found_down'}
                else 'unknown'
            ),
        },
        'posture_end': (
            'lying_floor'
            if assessment in {'confirmed_fall', 'found_down'}
            else 'standing'
        ),
        'risk': (
            'urgent'
            if assessment in {'confirmed_fall', 'found_down'}
            else 'none'
        ),
        'risk_confidence': 0.9,
        'camera_motion_observed': 'none',
        'explanation_ko': '카메라 관측 결과입니다.',
        'evidence_ko': ['관측 근거'],
        'uncertainty_flags': (
            ['partial_body'] if assessment == 'unobservable' else []
        ),
    }


def _request(
    tmp_path: Path,
    kind=FallCandidateKind.OBSERVED_FALL,
    context_variant='C2',
):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'offline-video-fixture')
    evidence = PhysicalFallEvidence(
        candidate_kind=kind,
        descent_score=0.9 if kind is FallCandidateKind.OBSERVED_FALL else 0.0,
        floor_proximity_score=0.9,
        body_visibility='partial',
        robot_motion='none',
        depth=AuroraDepthEvidence(
            aligned_to_rgb=True,
            stale=False,
            valid_torso_ratio=0.8,
            torso_floor_distance_m=0.08,
            person_distance_m=1.1,
            sampled_torso_points=4,
        ),
        pose_summary={'visibleKeypoints': 8},
        yolo_summary={'person': 0.92},
    )
    return VlmAnalysisRequest(
        request_id='request-1',
        incident_id='incident-1',
        duration_s=10.0,
        media=VlmMedia(video_format='mp4', local_path=str(clip)),
        evidence=evidence,
        context_variant=context_variant,
    )


class _StaticProvider(VlmProvider):
    def __init__(self, prediction):
        self.prediction = prediction

    def analyze(self, request):
        del request
        return VlmProviderResult(
            prediction=self.prediction,
            provider='fixture',
            model_id='fixture-vlm',
            model_version='1',
            region='local',
            latency_ms=1.0,
        )


def test_strong_local_evidence_cannot_be_cleared_by_one_model_call(
    tmp_path: Path,
) -> None:
    service = FallVlmAnalysisService(
        _StaticProvider(_prediction('normal_activity'))
    )

    decision = service.analyze(_request(tmp_path))

    assert decision.assessment is FallAssessment.UNOBSERVABLE
    assert decision.notify_guardian is True
    assert (
        'model_cannot_clear_strong_local_evidence'
        in decision.policy_reasons
    )


def test_unresponsive_found_down_cannot_be_cleared_below_score_threshold(
    tmp_path: Path,
) -> None:
    service = FallVlmAnalysisService(
        _StaticProvider(_prediction('normal_activity'))
    )
    request = _request(tmp_path, FallCandidateKind.FOUND_DOWN)
    request = replace(
        request,
        evidence=replace(
            request.evidence,
            floor_proximity_score=0.69,
            response=ResponseState.UNRESPONSIVE,
            depth=None,
        ),
    )

    decision = service.analyze(request)

    assert decision.assessment is FallAssessment.FOUND_DOWN
    assert decision.risk == 'urgent'
    assert decision.notify_guardian is True
    assert (
        'unresponsive_found_down_cannot_be_cleared'
        in decision.policy_reasons
    )


def test_empty_general_event_does_not_alert_when_visibility_is_none(
    tmp_path: Path,
) -> None:
    service = FallVlmAnalysisService(
        _StaticProvider(_prediction('normal_activity'))
    )
    request = _request(tmp_path, FallCandidateKind.GENERAL_EVENT)
    request = replace(
        request,
        evidence=replace(
            request.evidence,
            floor_proximity_score=0.0,
            body_visibility='none',
            depth=None,
            pose_summary={},
            yolo_summary={},
        ),
    )

    decision = service.analyze(request)

    assert decision.assessment is FallAssessment.NORMAL_ACTIVITY
    assert decision.risk == 'none'
    assert decision.notify_guardian is False


def test_lower_body_view_is_not_discarded_before_vlm_decision(
    tmp_path: Path,
) -> None:
    service = FallVlmAnalysisService(
        _StaticProvider(_prediction('confirmed_fall'))
    )
    request = _request(tmp_path)
    request = replace(
        request,
        evidence=replace(
            request.evidence,
            body_visibility='lower_only',
            depth=None,
        ),
    )

    decision = service.analyze(request)

    assert decision.assessment is FallAssessment.CONFIRMED_FALL
    assert 'local_observation_insufficient' not in decision.policy_reasons


def test_unexpected_provider_contract_error_fails_closed(
    tmp_path: Path,
) -> None:
    class BrokenProvider(VlmProvider):
        def analyze(self, request):
            del request
            raise AttributeError('malformed SDK response')

    decision = FallVlmAnalysisService(BrokenProvider()).analyze(
        _request(tmp_path)
    )

    assert decision.assessment is FallAssessment.UNOBSERVABLE
    assert decision.notify_guardian is True
    assert decision.policy_reasons == ('provider_contract_failure',)


def test_patrol_discovery_cannot_be_rewritten_as_witnessed_fall(
    tmp_path: Path,
) -> None:
    service = FallVlmAnalysisService(
        _StaticProvider(_prediction('confirmed_fall'))
    )

    decision = service.analyze(
        _request(tmp_path, FallCandidateKind.FOUND_DOWN)
    )

    assert decision.assessment is FallAssessment.FOUND_DOWN
    assert decision.risk == 'urgent'
    assert 'descent_not_observed' in decision.policy_reasons


def test_invalid_provider_output_fails_closed(tmp_path: Path) -> None:
    service = FallVlmAnalysisService(_StaticProvider({'invalid': True}))

    decision = service.analyze(_request(tmp_path))

    assert decision.assessment is FallAssessment.UNOBSERVABLE
    assert decision.policy_reasons == ('invalid_provider_output',)


def test_nova_builds_forced_tool_request_and_parses_result(
    tmp_path: Path,
) -> None:
    captured = {}

    class Client:
        def converse(self, **payload):
            captured.update(payload)
            return {
                'output': {
                    'message': {
                        'content': [
                            {
                                'toolUse': {
                                    'name': 'record_homecam_assessment',
                                    'input': _prediction(),
                                }
                            }
                        ]
                    }
                },
                'usage': {'inputTokens': 100, 'outputTokens': 30},
                'ResponseMetadata': {'RequestId': 'aws-request'},
            }

    provider = BedrockNovaVlmProvider(client=Client())
    result = provider.analyze(_request(tmp_path))

    assert captured['toolConfig']['toolChoice'] == {
        'tool': {'name': 'record_homecam_assessment'}
    }
    schema = captured['toolConfig']['tools'][0]['toolSpec'][
        'inputSchema'
    ]['json']
    assert 'additionalProperties' not in schema
    assert captured['messages'][0]['content'][0]['video']['source']['bytes']
    assert 'Aurora930 RGB-D' in captured['messages'][0]['content'][1]['text']
    assert result.prediction['fall']['assessment'] == 'confirmed_fall'
    assert result.input_tokens == 100


def test_provider_schema_uses_only_conservative_decoder_keywords() -> None:
    serialized = json.dumps(provider_prediction_schema(), sort_keys=True)

    assert 'additionalProperties' not in serialized
    assert 'minLength' not in serialized
    assert 'maxLength' not in serialized
    assert 'uniqueItems' not in serialized


def test_context_ablation_never_leaks_local_evidence_into_c0(
    tmp_path: Path,
) -> None:
    provider = BedrockNovaVlmProvider(client=object())

    payload = provider.build_payload(_request(tmp_path, context_variant='C0'))
    prompt = payload['messages'][0]['content'][1]['text']

    assert 'YOLO' not in prompt
    assert 'Aurora930' not in prompt
    assert '로봇 이동 타임라인' not in prompt


def test_gemini_adapter_uses_same_schema_and_redacts_key(
    tmp_path: Path,
) -> None:
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(
            url=url,
            headers=headers,
            payload=payload,
            timeout=timeout,
        )
        import json

        return {
            'candidates': [
                {'content': {'parts': [{'text': json.dumps(_prediction())}]}}
            ],
            'usageMetadata': {
                'promptTokenCount': 90,
                'candidatesTokenCount': 25,
                'thoughtsTokenCount': 5,
            },
        }

    provider = GeminiVlmProvider(
        api_key='secret-test-key',
        transport=transport,
    )
    result = provider.analyze(_request(tmp_path))

    assert captured['url'].startswith(
        'https://generativelanguage.googleapis.com/'
    )
    assert captured['payload']['generationConfig']['responseJsonSchema']
    assert 'secret-test-key' not in repr(provider)
    assert result.output_tokens == 30


def test_gemini_rejects_unsupported_video_mime(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request = replace(
        request,
        media=VlmMedia(
            video_format='mkv',
            local_path=str(tmp_path / 'clip.mp4'),
        ),
    )
    provider = GeminiVlmProvider(
        api_key='secret-test-key',
        transport=lambda *args: {},
    )

    with pytest.raises(VlmProviderError, match='format_unsupported'):
        provider.build_payload(request)


def test_openai_compatible_adapter_supports_local_candidate(
    tmp_path: Path,
) -> None:
    captured = {}

    def transport(url, headers, payload, timeout):
        captured.update(
            url=url,
            headers=headers,
            payload=payload,
            timeout=timeout,
        )
        import json

        return {
            'id': 'local-request',
            'choices': [
                {
                    'message': {
                        'content': json.dumps(_prediction('found_down'))
                    }
                }
            ],
            'usage': {'prompt_tokens': 80, 'completion_tokens': 20},
        }

    provider = OpenAiCompatibleVlmProvider(
        model_id='openbmb/MiniCPM-V-4.6',
        base_url='http://127.0.0.1:8000/v1',
        transport=transport,
    )
    result = provider.analyze(_request(tmp_path))

    assert captured['url'].endswith('/v1/chat/completions')
    assert captured['payload']['response_format']['json_schema']['strict']
    assert captured['payload']['messages'][1]['content'][1][
        'video_url'
    ]['url'].startswith('data:video/mp4;base64,')
    assert result.prediction['fall']['assessment'] == 'found_down'
    assert result.input_tokens == 80


def test_qwen_singapore_uses_json_object_not_unsupported_schema(
    tmp_path: Path,
) -> None:
    provider = OpenAiCompatibleVlmProvider(
        model_id='qwen3-vl-flash',
        base_url='https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
        api_key='secret-test-key',
        provider_name='alibaba-qwen',
    )

    payload = provider.build_payload(_request(tmp_path))

    assert payload['response_format'] == {'type': 'json_object'}
    assert payload['enable_thinking'] is False


def test_factory_switches_provider_without_changing_service(
    tmp_path: Path,
) -> None:
    settings = VlmSettings.from_env(
        {
            'MALBUT_VLM_PROVIDER': 'nova',
            'MALBUT_VLM_MODEL': 'global.amazon.nova-2-lite-v1:0',
        }
    )
    provider = create_vlm_provider(settings, nova_client=object())

    assert isinstance(provider, BedrockNovaVlmProvider)
    assert 'api_key=<redacted>' in repr(settings)


def test_blank_shared_model_settings_use_provider_specific_defaults() -> None:
    settings = VlmSettings.from_env(
        {
            'MALBUT_VLM_PROVIDER': 'gemini',
            'MALBUT_VLM_MODEL': '',
            'MALBUT_VLM_REGION': '',
        }
    )

    assert settings.model_id == 'gemini-3.8-flash'
    assert settings.region == 'global'


def test_inference_runner_uses_sidecar_and_writes_private_jsonl(
    tmp_path: Path,
) -> None:
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'offline-video-fixture')
    import hashlib

    clip_sha256 = hashlib.sha256(clip.read_bytes()).hexdigest()
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text(
        '{"schema_version":2,"case_id":"C1","clip":'
        '{"duration_s":3,"path":"clip.mp4","sha256":"'
        + clip_sha256
        + '"},"robot_motion":'
        '{"summary":"none","timeline":[]},"subjects_present":'
        '["person"],"counts":{"n_person":1,"n_pet":0},"events":[], '
        '"fall_assessment_gt":"found_down","posture_end":'
        '"lying_floor","risk_gt":"urgent","traffic_class":'
        '"found_down"}\n',
        encoding='utf-8',
    )
    observations_path = tmp_path / 'observations.jsonl'
    observations_path.write_text(
        '{"schema_version":2,"case_id":"C1","candidate_kind":'
        '"found_down_candidate","descent_score":0,'
        '"floor_proximity_score":0.9,"body_visibility":"partial",'
        '"robot_motion":"none","pose":{},"yolo":{"person":0.9},'
        '"depth":null,"source":{"producer":"fixture-detector",'
        '"version":"1","config_sha256":"'
        + ('c' * 64)
        + '","artifact_sha256":"'
        + ('d' * 64)
        + '"}}\n',
        encoding='utf-8',
    )
    cases = load_vlm_cases(manifest, require_media=True)
    observations = load_observations(observations_path, cases)
    settings = VlmSettings(provider='nova', model_id='fixture')
    records = generate_prediction_records(
        manifest=manifest,
        cases=cases,
        observations=observations,
        provider=_StaticProvider(_prediction('found_down')),
        settings=settings,
        repetitions=2,
        input_spec={
            'sampling': 'fixture',
            'effective_fps': 1,
            'frame_count': 3,
            'resolution': '640x400',
            'audio_included': False,
            'preprocessing_sha256': None,
            'structured_output_mode': 'tool_use',
            'decoding': {},
        },
        context_variant='C2',
    )
    output = tmp_path / 'predictions.jsonl'
    write_private_jsonl(output, records)

    assert len(records) == 2
    assert all(row['case_id'] == 'C1' for row in records)
    assert all(
        row['prediction']['fall']['assessment'] == 'found_down'
        for row in records
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert len(load_prediction_records(output, cases)) == 2


def test_private_journal_preserves_completed_rows_on_interruption(
    tmp_path: Path,
) -> None:
    output = tmp_path / 'partial.jsonl'

    with pytest.raises(RuntimeError, match='interrupted'):
        with private_jsonl_journal(output) as append:
            append({'case_id': 'C1', 'request_succeeded': True})
            raise RuntimeError('interrupted')

    assert json.loads(output.read_text(encoding='utf-8')) == {
        'case_id': 'C1',
        'request_succeeded': True,
    }
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        with private_jsonl_journal(output):
            pass
