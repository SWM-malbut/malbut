"""Google Gemini adapter for the common home-camera VLM contract."""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

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
# GenerateContent inline requests include base64 (+33%), prompts, and schema in
# the HTTP body. Keep raw video below 15 MiB to remain below the documented
# 20 MB total-request guidance; larger clips require a future Files API path.
MAX_INLINE_VIDEO_BYTES = 14 * 1024 * 1024
OFFICIAL_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'
VIDEO_MIME_TYPES = {
    'avi': 'video/avi',
    'flv': 'video/x-flv',
    'mov': 'video/mov',
    'mp4': 'video/mp4',
    'mpeg': 'video/mpeg',
    'mpg': 'video/mpg',
    'three_gp': 'video/3gpp',
    'webm': 'video/webm',
    'wmv': 'video/wmv',
}


class GeminiVlmProvider(VlmProvider):
    """Call Gemini native video with strict local post-validation."""

    name = 'google-gemini'

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str = 'gemini-3.8-flash',
        timeout_seconds: int = 60,
        max_output_tokens: int = 1200,
        transport: Optional[Transport] = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError('api_key must not be empty')
        if not model_id.strip() or len(model_id) > 128:
            raise ValueError('model_id is invalid')
        if not 1 <= timeout_seconds <= 300:
            raise ValueError('timeout_seconds must be between 1 and 300')
        if not 128 <= max_output_tokens <= 8192:
            raise ValueError('max_output_tokens must be between 128 and 8192')
        self._api_key = api_key.strip()
        self.model_id = model_id.strip()
        self.model_version = self.model_id
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self._transport = transport or self._urllib_transport

    def __repr__(self) -> str:
        return (
            'GeminiVlmProvider('
            f'model_id={self.model_id!r}, api_key=<redacted>)'
        )

    def analyze(self, request: VlmAnalysisRequest) -> VlmProviderResult:
        payload = self.build_payload(request)
        headers = {
            'Content-Type': 'application/json',
            'x-goog-api-key': self._api_key,
            'User-Agent': 'malbut-vlm/0.1',
        }
        started = time.perf_counter()
        try:
            response = self._transport(
                f'{OFFICIAL_BASE_URL}/models/{self.model_id}:generateContent',
                headers,
                payload,
                self.timeout_seconds,
            )
        except Exception as error:
            raise VlmProviderError('gemini_request_failed') from error
        latency_ms = (time.perf_counter() - started) * 1000
        prediction = self._parse_prediction(response)
        usage = response.get('usageMetadata', {})
        if not isinstance(usage, Mapping):
            usage = {}
        candidate_tokens = self._optional_int(
            usage.get('candidatesTokenCount')
        )
        thought_tokens = self._optional_int(
            usage.get('thoughtsTokenCount')
        )
        billed_output_tokens = (
            None
            if candidate_tokens is None and thought_tokens is None
            else (candidate_tokens or 0) + (thought_tokens or 0)
        )
        return VlmProviderResult(
            prediction=prediction,
            provider=self.name,
            model_id=self.model_id,
            model_version=self.model_version,
            region='global',
            latency_ms=latency_ms,
            input_tokens=self._optional_int(usage.get('promptTokenCount')),
            output_tokens=billed_output_tokens,
            response_id=(
                response.get('responseId')
                if isinstance(response.get('responseId'), str)
                else None
            ),
        )

    def build_payload(self, request: VlmAnalysisRequest) -> Dict[str, Any]:
        if request.media.local_path is None:
            raise VlmProviderError('gemini_requires_local_or_uploaded_media')
        path = Path(request.media.local_path)
        size = path.stat().st_size
        if size < 1 or size > MAX_INLINE_VIDEO_BYTES:
            raise VlmProviderError('gemini_inline_video_size_invalid')
        video_bytes = path.read_bytes()
        if request.media.sha256 is not None and (
            hashlib.sha256(video_bytes).hexdigest() != request.media.sha256
        ):
            raise VlmProviderError('gemini_inline_video_hash_mismatch')
        context = request.prompt_contexts()
        prompt = build_user_prompt(
            request.duration_s,
            yolo_context=context['yolo'],
            rgbd_context=context['rgbd'],
            robot_motion_context=context['robot_motion'],
        )
        mime_type = VIDEO_MIME_TYPES.get(request.media.video_format)
        if mime_type is None:
            raise VlmProviderError('gemini_video_format_unsupported')
        return {
            'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
            'contents': [
                {
                    'role': 'user',
                    'parts': [
                        {
                            'inlineData': {
                                'mimeType': mime_type,
                                'data': base64.b64encode(video_bytes).decode(
                                    'ascii'
                                ),
                            }
                        },
                        {'text': prompt},
                    ],
                }
            ],
            'generationConfig': {
                'temperature': 0,
                'maxOutputTokens': self.max_output_tokens,
                'responseMimeType': 'application/json',
                'responseJsonSchema': provider_prediction_schema(),
                'thinkingConfig': {'thinkingLevel': 'LOW'},
            },
        }

    @staticmethod
    def _parse_prediction(response: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            candidate = response['candidates'][0]
            parts = candidate['content']['parts']
        except (KeyError, IndexError, TypeError) as error:
            raise VlmProviderError('gemini_response_invalid') from error
        finish_reason = candidate.get('finishReason')
        if finish_reason not in {None, 'STOP'}:
            raise VlmProviderError('gemini_incomplete_response')
        texts = [
            part['text']
            for part in parts
            if isinstance(part, Mapping) and isinstance(part.get('text'), str)
        ]
        if len(texts) != 1:
            raise VlmProviderError('gemini_response_invalid')
        try:
            value = json.loads(texts[0])
        except json.JSONDecodeError as error:
            raise VlmProviderError('gemini_json_invalid') from error
        if not isinstance(value, Mapping):
            raise VlmProviderError('gemini_json_invalid')
        return dict(value)

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
            raise VlmProviderError('gemini_http_failed') from error
        if len(raw) > 4 * 1024 * 1024:
            raise VlmProviderError('gemini_response_too_large')
        try:
            value = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VlmProviderError('gemini_response_invalid') from error
        if not isinstance(value, dict):
            raise VlmProviderError('gemini_response_invalid')
        return value
