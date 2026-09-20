"""Amazon Nova 2 Lite adapter for the common home-camera VLM contract."""

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

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
    TOOL_DESCRIPTION,
    TOOL_NAME,
    build_user_prompt,
    provider_prediction_schema,
)


MAX_INLINE_VIDEO_BYTES = 25 * 1024 * 1024


def _tool_schema() -> Dict[str, Any]:
    """Return Nova's top-level ToolInputSchema-compatible contract."""
    return provider_prediction_schema()


class BedrockNovaVlmProvider(VlmProvider):
    """Call Nova through Bedrock Converse without storing credentials."""

    name = 'amazon-bedrock'

    def __init__(
        self,
        *,
        model_id: str = 'global.amazon.nova-2-lite-v1:0',
        region: str = 'ap-northeast-2',
        client: Optional[Any] = None,
        timeout_seconds: int = 60,
        max_output_tokens: int = 1200,
    ) -> None:
        if not model_id.strip() or len(model_id) > 2048:
            raise ValueError('model_id is invalid')
        if not region.strip() or len(region) > 64:
            raise ValueError('region is invalid')
        if not 1 <= timeout_seconds <= 300:
            raise ValueError('timeout_seconds must be between 1 and 300')
        if not 128 <= max_output_tokens <= 5000:
            raise ValueError('max_output_tokens must be between 128 and 5000')
        self.model_id = model_id.strip()
        self.model_version = 'nova-2-lite-v1:0'
        self.region = region.strip()
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self._client = client

    def __repr__(self) -> str:
        return (
            'BedrockNovaVlmProvider('
            f'model_id={self.model_id!r}, region={self.region!r})'
        )

    def analyze(self, request: VlmAnalysisRequest) -> VlmProviderResult:
        """Call Converse once and return only validated-shape raw data."""
        payload = self.build_payload(request)
        started = time.perf_counter()
        try:
            response = self._runtime_client().converse(**payload)
        except Exception as error:
            raise VlmProviderError(
                'bedrock_nova_request_failed'
            ) from error
        latency_ms = (time.perf_counter() - started) * 1000
        prediction = self._parse_prediction(response)
        usage = response.get('usage', {})
        metadata = response.get('ResponseMetadata', {})
        if not isinstance(usage, Mapping):
            usage = {}
        return VlmProviderResult(
            prediction=prediction,
            provider=self.name,
            model_id=self.model_id,
            model_version=self.model_version,
            region=self.region,
            latency_ms=latency_ms,
            input_tokens=self._optional_nonnegative_int(
                usage.get('inputTokens')
            ),
            output_tokens=self._optional_nonnegative_int(
                usage.get('outputTokens')
            ),
            response_id=(
                str(metadata['RequestId'])
                if isinstance(metadata, Mapping)
                and isinstance(metadata.get('RequestId'), str)
                else None
            ),
        )

    def build_payload(self, request: VlmAnalysisRequest) -> Dict[str, Any]:
        """Build a deterministic Converse request for tests and auditing."""
        context = request.prompt_contexts()
        prompt = build_user_prompt(
            request.duration_s,
            yolo_context=context['yolo'],
            rgbd_context=context['rgbd'],
            robot_motion_context=context['robot_motion'],
        )
        return {
            'modelId': self.model_id,
            'system': [{'text': SYSTEM_PROMPT}],
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {'video': self._video_block(request)},
                        {'text': prompt},
                    ],
                }
            ],
            'inferenceConfig': {
                'maxTokens': self.max_output_tokens,
                'temperature': 0.00001,
            },
            'toolConfig': {
                'tools': [
                    {
                        'toolSpec': {
                            'name': TOOL_NAME,
                            'description': TOOL_DESCRIPTION,
                            'inputSchema': {'json': _tool_schema()},
                        }
                    }
                ],
                'toolChoice': {'tool': {'name': TOOL_NAME}},
            },
        }

    def _runtime_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as error:
                raise VlmProviderError('boto3_not_installed') from error
            self._client = boto3.client(
                'bedrock-runtime',
                region_name=self.region,
                config=Config(
                    connect_timeout=min(10, self.timeout_seconds),
                    read_timeout=self.timeout_seconds,
                    retries={'max_attempts': 0},
                ),
            )
        return self._client

    @staticmethod
    def _video_block(request: VlmAnalysisRequest) -> Dict[str, Any]:
        media = request.media
        if media.s3_uri is not None:
            source: Dict[str, Any] = {
                's3Location': {'uri': media.s3_uri}
            }
        else:
            path = Path(str(media.local_path))
            size = path.stat().st_size
            if size < 1 or size > MAX_INLINE_VIDEO_BYTES:
                raise VlmProviderError('inline_video_size_invalid')
            payload = path.read_bytes()
            if media.sha256 is not None and (
                hashlib.sha256(payload).hexdigest() != media.sha256
            ):
                raise VlmProviderError('inline_video_hash_mismatch')
            source = {'bytes': payload}
        return {
            'format': media.video_format,
            'source': source,
        }

    @staticmethod
    def _parse_prediction(response: Mapping[str, Any]) -> Mapping[str, Any]:
        stop_reason = response.get('stopReason')
        if stop_reason not in {None, 'tool_use'}:
            raise VlmProviderError('bedrock_nova_incomplete_response')
        try:
            content = response['output']['message']['content']
        except (KeyError, TypeError) as error:
            raise VlmProviderError('bedrock_nova_response_invalid') from error
        if not isinstance(content, list):
            raise VlmProviderError('bedrock_nova_response_invalid')
        calls = [
            block['toolUse']
            for block in content
            if isinstance(block, Mapping)
            and isinstance(block.get('toolUse'), Mapping)
            and block['toolUse'].get('name') == TOOL_NAME
        ]
        if len(calls) != 1 or not isinstance(calls[0].get('input'), Mapping):
            raise VlmProviderError('bedrock_nova_tool_output_invalid')
        return dict(calls[0]['input'])

    @staticmethod
    def _optional_nonnegative_int(value: Any) -> Optional[int]:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value
