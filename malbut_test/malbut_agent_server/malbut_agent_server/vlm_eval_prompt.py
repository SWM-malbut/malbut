"""Shared prompt and locally enforced schema for every VLM adapter."""

import copy
import hashlib
import json
from typing import Any, Dict, Mapping, Optional


PROMPT_VERSION = 'malbut-homecam-vlm-v3'
TOOL_NAME = 'record_homecam_assessment'
TOOL_DESCRIPTION = (
    'Record exactly one safety assessment for the attached home-camera '
    'observation.'
)
SYSTEM_PROMPT = """당신은 가정용 이동형 홈캠의 짧은 영상을 분석합니다.
영상에서 직접 관찰할 수 있는 사실만 사용해 대상, 시간 구간이 있는 사건,
마지막 자세, 위험도, 카메라 이동 여부를 판정하세요.

낙상은 사람이 의도치 않게 바닥이나 더 낮은 위치에 멈추는 사건입니다.
의도적으로 눕기, 침대나 소파에 눕기, 물건 줍기, 쪼그리기, 운동,
아이·반려동물과 놀기를 낙상으로 분류하지 마세요. 하강 과정이 보이면
confirmed_fall, 과정은 없지만 바닥에 쓰러진 사람을 확인하면 found_down으로
구분하세요. 정상 행동임을 확인할 수 있을 때만 normal_activity를 사용하고,
가림·부분 시야·센서 불확실성 때문에 안전하게 판정할 수 없으면
unobservable을 사용하세요. found_down과 unobservable을 normal_activity로
축소하면 안 됩니다.
로봇 이동 때문에 화면 전체가 움직이는 것과 사람의 움직임을 구분하세요.
fall.confidence는 현재 판정에 대한 일반적인 확신도가 아니라, 이 클립 안에서
사람의 의도치 않은 하강 과정이 실제로 관측된 confirmed_fall일 확률입니다.
found_down처럼 하강 과정이 보이지 않은 경우 이 값을 높게 주면 안 됩니다.
fall.confidence가 0.5 이상일 때만 confirmed_fall을 사용하고, 0.5 미만이면
나머지 세 assessment 중 관측에 맞는 값을 사용하세요.

설명과 근거는 간결한 한국어로 작성하세요. 사람의 신원, 질병, 감정,
의도처럼 영상으로 확인할 수 없는 내용을 추측하지 마세요. 응답은 제공된
JSON Schema를 정확히 따르는 JSON 객체 하나여야 하며 다른 텍스트를
포함하면 안 됩니다.
"""
PREDICTION_JSON_SCHEMA: Dict[str, Any] = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'subjects',
        'events',
        'fall',
        'posture_end',
        'risk',
        'risk_confidence',
        'camera_motion_observed',
        'explanation_ko',
        'evidence_ko',
        'uncertainty_flags',
    ],
    'properties': {
        'subjects': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['person', 'pet', 'other'],
            'properties': {
                name: {'type': 'integer', 'minimum': 0, 'maximum': 20}
                for name in ('person', 'pet', 'other')
            },
        },
        'events': {
            'type': 'array',
            'maxItems': 32,
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'type',
                    'subject',
                    'start_s',
                    'end_s',
                    'confidence',
                ],
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': [
                            'fall',
                            'near_fall',
                            'lie_down_floor',
                            'lie_down_bed_sofa',
                            'sit_down_floor',
                            'pick_up_object',
                            'squat_kneel',
                            'exercise',
                            'play',
                            'enter',
                            'leave',
                            'other',
                        ],
                    },
                    'subject': {
                        'type': 'string',
                        'enum': ['person', 'pet', 'other'],
                    },
                    'start_s': {'type': 'number', 'minimum': 0},
                    'end_s': {'type': 'number', 'minimum': 0},
                    'confidence': {
                        'type': 'number',
                        'minimum': 0,
                        'maximum': 1,
                    },
                },
            },
        },
        'fall': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['assessment', 'confidence', 'recovery'],
            'properties': {
                'assessment': {
                    'type': 'string',
                    'enum': [
                        'confirmed_fall',
                        'found_down',
                        'normal_activity',
                        'unobservable',
                    ],
                },
                'confidence': {
                    'type': 'number',
                    'minimum': 0,
                    'maximum': 1,
                    'description': (
                        'Probability that an unintended human descent '
                        'was observed in this clip (confirmed_fall), not '
                        'general confidence in the assessment.'
                    ),
                },
                'recovery': {
                    'type': 'string',
                    'enum': ['recovered', 'not_recovered', 'unknown'],
                },
            },
        },
        'posture_end': {
            'type': 'string',
            'enum': [
                'standing',
                'sitting',
                'lying_floor',
                'lying_bed_sofa',
                'unknown',
            ],
        },
        'risk': {
            'type': 'string',
            'enum': ['urgent', 'attention', 'none'],
        },
        'risk_confidence': {
            'type': 'number',
            'minimum': 0,
            'maximum': 1,
        },
        'camera_motion_observed': {
            'type': 'string',
            'enum': ['none', 'partial', 'whole'],
        },
        'explanation_ko': {'type': 'string', 'maxLength': 500},
        'evidence_ko': {
            'type': 'array',
            'maxItems': 8,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 300},
        },
        'uncertainty_flags': {
            'type': 'array',
            'uniqueItems': True,
            'items': {
                'type': 'string',
                'enum': [
                    'occluded',
                    'low_light',
                    'partial_body',
                    'far',
                    'short_clip',
                    'depth_unavailable',
                    'depth_unaligned',
                    'sensor_stale',
                ],
            },
        },
    },
}

USER_PROMPT_CONTRACT = {
    'duration': '클립 길이: {duration_s:.3f}초',
    'yolo': 'YOLO 관측 요약: {payload}',
    'rgbd': 'Aurora930 RGB-D 물리 관측 요약: {payload}',
    'robot_motion': '로봇 이동 타임라인: {payload}',
    'instruction': '첨부된 영상 전체를 분석해 JSON 객체 하나로 응답하세요.',
}


def provider_prediction_schema() -> Dict[str, Any]:
    """Return a conservative schema accepted by provider decoders.

    The complete contract above remains authoritative and is always checked
    locally. Provider constrained decoders implement different JSON Schema
    subsets, so unsupported annotation keywords are removed recursively.
    """
    schema = copy.deepcopy(PREDICTION_JSON_SCHEMA)
    unsupported = {
        'additionalProperties',
        'minLength',
        'maxLength',
        'uniqueItems',
    }

    def sanitize(value: Any) -> None:
        if isinstance(value, dict):
            for key in tuple(value):
                if key in unsupported:
                    del value[key]
                else:
                    sanitize(value[key])
        elif isinstance(value, list):
            for item in value:
                sanitize(item)

    sanitize(schema)
    return schema


PROMPT_SHA256 = hashlib.sha256(
    json.dumps(
        {
            'version': PROMPT_VERSION,
            'system_prompt': SYSTEM_PROMPT,
            'user_prompt_contract': USER_PROMPT_CONTRACT,
            'tool_contract': {
                'name': TOOL_NAME,
                'description': TOOL_DESCRIPTION,
            },
            'prediction_schema': PREDICTION_JSON_SCHEMA,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()


def build_user_prompt(
    duration_s: float,
    *,
    yolo_context: Optional[Mapping[str, Any]] = None,
    rgbd_context: Optional[Mapping[str, Any]] = None,
    robot_motion_context: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build a deterministic per-clip message for C0/C1/C2."""
    lines = [
        USER_PROMPT_CONTRACT['duration'].format(duration_s=duration_s)
    ]
    if yolo_context is not None:
        serialized = json.dumps(
            dict(yolo_context),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        lines.append(USER_PROMPT_CONTRACT['yolo'].format(payload=serialized))
    if rgbd_context is not None:
        serialized = json.dumps(
            dict(rgbd_context),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        lines.append(USER_PROMPT_CONTRACT['rgbd'].format(payload=serialized))
    if robot_motion_context is not None:
        serialized = json.dumps(
            dict(robot_motion_context),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        lines.append(
            USER_PROMPT_CONTRACT['robot_motion'].format(payload=serialized)
        )
    lines.append(USER_PROMPT_CONTRACT['instruction'])
    return '\n'.join(lines)
