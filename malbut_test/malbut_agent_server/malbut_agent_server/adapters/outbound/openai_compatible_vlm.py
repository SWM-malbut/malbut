"""Qwen and self-hosted OpenAI-compatible video VLM adapter."""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from malbut_agent_server.domain.vlm import (
    VlmAnalysisRequest,
    VlmProviderResult,
)
from malbut_agent_server.ports.vlm_provider import (
    VlmProvider,
    VlmProviderError,
)
from malbut_agent_server.vlm_eval_prompt import (
    SYSTEM_PROMPT,
    build_user_prompt,
    provider_prediction_schema,
)


Transport = Callable[
    [str, Dict[str, str], Dict[str, Any], int],
    Dict[str, Any],
]
QWEN_BASE_URL = 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1'
MAX_INLINE_VIDEO_BYTES = 100 * 1024 * 1024


class OpenAiCompatibleVlmProvider(VlmProvider):
    """Use one fixed OpenAI-compatible endpoint for Qwen or local VLMs."""

    name = 'openai-compatible-vlm'

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key: str = '',
        provider_name: str = name,
        region: str = 'local',
        timeout_seconds: int = 60,
        max_output_tokens: int = 1200,
        allowed_api_key_hosts: Sequence[str] = (
            'dashscope-intl.aliyuncs.com',
        ),
        transport: Optional[Transport] = None,
    ) -> None:
        if not model_id.strip() or len(model_id) > 256:
            raise ValueError('model_id is invalid')
        self.base_url = self._validate_endpoint(
            base_url,
            bool(api_key.strip()),
            allowed_api_key_hosts,
        )
        if not 1 <= timeout_seconds <= 300:
            raise ValueError('timeout_seconds must be between 1 and 300')
        if not 128 <= max_output_tokens <= 8192:
            raise ValueError('max_output_tokens must be between 128 and 8192')
        self.model_id = model_id.strip()
        self.model_version = self.model_id
        self.provider_name = provider_name.strip() or self.name
        self.region = region.strip() or 'unknown'
        self._api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self._transport = transport or self._urllib_transport

    def __repr__(self) -> str:
        return (
            'OpenAiCompatibleVlmProvider('
            f'model_id={self.model_id!r}, base_url={self.base_url!r}, '
            'api_key=<redacted>)'
        )

    def analyze(self, request: VlmAnalysisRequest) -> VlmProviderResult:
        headers = {'Content-Type': 'application/json'}
        if self._api_key:
            headers['Authorization'] = f'Bearer {self._api_key}'
        started = time.perf_counter()
        try:
            response = self._transport(
                f'{self.base_url}/chat/completions',
                headers,
                self.build_payload(request),
                self.timeout_seconds,
            )
        except Exception as error:
            raise VlmProviderError('compatible_vlm_request_failed') from error
        latency_ms = (time.perf_counter() - started) * 1000
        prediction = self._parse_prediction(response)
        usage = response.get('usage', {})
        if not isinstance(usage, Mapping):
            usage = {}
        return VlmProviderResult(
            prediction=prediction,
            provider=self.provider_name,
            model_id=self.model_id,
            model_version=self.model_version,
            region=self.region,
            latency_ms=latency_ms,
            input_tokens=self._optional_int(usage.get('prompt_tokens')),
            output_tokens=self._optional_int(usage.get('completion_tokens')),
            response_id=(
                response.get('id')
                if isinstance(response.get('id'), str)
                else None
            ),
        )

    def build_payload(self, request: VlmAnalysisRequest) -> Dict[str, Any]:
        context = request.prompt_contexts()
        prompt = build_user_prompt(
            request.duration_s,
            yolo_context=context['yolo'],
            rgbd_context=context['rgbd'],
            robot_motion_context=context['robot_motion'],
        )
        payload = {
            'model': self.model_id,
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {
                            'type': 'video_url',
                            'video_url': {'url': self._media_url(request)},
                        },
                    ],
                },
            ],
            'temperature': 0,
            'max_tokens': self.max_output_tokens,
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'malbut_homecam_assessment',
                    'strict': True,
                    'schema': provider_prediction_schema(),
                },
            },
        }
        if self.provider_name == 'alibaba-qwen':
            # Model Studio's Singapore endpoint supports JSON Object for
            # multimodal Qwen, but not strict JSON Schema. The common contract
            # is still enforced after the response is received.
            payload['enable_thinking'] = False
            payload['response_format'] = {'type': 'json_object'}
        return payload

    @staticmethod
    def _media_url(request: VlmAnalysisRequest) -> str:
        if request.media.s3_uri is not None:
            raise VlmProviderError(
                'compatible_vlm_requires_https_or_inline_media'
            )
        path = Path(str(request.media.local_path))
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_INLINE_VIDEO_BYTES:
            raise VlmProviderError('compatible_vlm_video_size_invalid')
        if request.media.sha256 is not None and (
            hashlib.sha256(payload).hexdigest() != request.media.sha256
        ):
            raise VlmProviderError('compatible_vlm_video_hash_mismatch')
        suffix = (
            '3gpp'
            if request.media.video_format == 'three_gp'
            else request.media.video_format
        )
        return (
            f'data:video/{suffix};base64,'
            + base64.b64encode(payload).decode('ascii')
        )

    @staticmethod
    def _parse_prediction(response: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            choice = response['choices'][0]
            content = choice['message']['content']
        except (KeyError, IndexError, TypeError) as error:
            raise VlmProviderError(
                'compatible_vlm_response_invalid'
            ) from error
        finish_reason = choice.get('finish_reason')
        if finish_reason not in {None, 'stop'}:
            raise VlmProviderError('compatible_vlm_incomplete_response')
        if not isinstance(content, str):
            raise VlmProviderError('compatible_vlm_response_invalid')
        try:
            value = json.loads(content)
        except json.JSONDecodeError as error:
            raise VlmProviderError('compatible_vlm_json_invalid') from error
        if not isinstance(value, Mapping):
            raise VlmProviderError('compatible_vlm_json_invalid')
        return dict(value)

    @staticmethod
    def _validate_endpoint(
        base_url: str,
        has_api_key: bool,
        allowed_api_key_hosts: Sequence[str],
    ) -> str:
        parsed = urllib.parse.urlsplit(base_url.strip().rstrip('/'))
        if (
            parsed.scheme not in {'http', 'https'}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('base_url is invalid')
        loopback = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        if parsed.scheme == 'http' and not loopback:
            raise ValueError('plaintext VLM endpoint must be loopback')
        allowed = {host.lower() for host in allowed_api_key_hosts}
        if has_api_key and parsed.hostname.lower() not in allowed:
            raise ValueError('api_key endpoint host is not allowlisted')
        return urllib.parse.urlunsplit(parsed)

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _urllib_transport(
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(',', ':')).encode('utf-8'),
            method='POST',
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise VlmProviderError('compatible_vlm_http_failed') from error
        if len(raw) > 4 * 1024 * 1024:
            raise VlmProviderError('compatible_vlm_response_too_large')
        try:
            value = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VlmProviderError(
                'compatible_vlm_response_invalid'
            ) from error
        if not isinstance(value, dict):
            raise VlmProviderError('compatible_vlm_response_invalid')
        return value
