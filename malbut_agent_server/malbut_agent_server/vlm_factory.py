"""Environment-driven construction of interchangeable VLM adapters."""

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from malbut_agent_server.adapters.outbound.bedrock_nova_vlm import (
    BedrockNovaVlmProvider,
)
from malbut_agent_server.adapters.outbound.gemini_vlm import GeminiVlmProvider
from malbut_agent_server.adapters.outbound.openai_compatible_vlm import (
    QWEN_BASE_URL,
    OpenAiCompatibleVlmProvider,
)
from malbut_agent_server.ports.vlm_provider import VlmProvider


SUPPORTED_VLM_PROVIDERS = frozenset(
    {'nova', 'gemini', 'qwen', 'openai_compatible'}
)


@dataclass(frozen=True)
class VlmSettings:
    """Minimal configuration whose repr never exposes provider secrets."""

    provider: str = 'nova'
    model_id: str = 'global.amazon.nova-2-lite-v1:0'
    region: str = 'ap-northeast-2'
    api_key: str = ''
    base_url: str = ''
    timeout_seconds: int = 60
    max_output_tokens: int = 1200

    def __repr__(self) -> str:
        return (
            'VlmSettings('
            f'provider={self.provider!r}, model_id={self.model_id!r}, '
            f'region={self.region!r}, base_url={self.base_url!r}, '
            f'timeout_seconds={self.timeout_seconds!r}, '
            f'max_output_tokens={self.max_output_tokens!r}, '
            'api_key=<redacted>)'
        )

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> 'VlmSettings':
        source = environ if environ is not None else os.environ
        provider = source.get('MALBUT_VLM_PROVIDER', 'nova').strip().lower()
        if provider not in SUPPORTED_VLM_PROVIDERS:
            raise ValueError('MALBUT_VLM_PROVIDER is unsupported')
        defaults = {
            'nova': 'global.amazon.nova-2-lite-v1:0',
            'gemini': 'gemini-3.8-flash',
            'qwen': 'qwen3-vl-flash',
            'openai_compatible': 'openbmb/MiniCPM-V-4.6',
        }
        region_defaults = {
            'nova': 'ap-northeast-2',
            'gemini': 'global',
            'qwen': 'ap-southeast-1',
            'openai_compatible': 'local',
        }
        try:
            timeout = int(source.get('MALBUT_VLM_TIMEOUT_SECONDS', '60'))
            max_tokens = int(
                source.get('MALBUT_VLM_MAX_OUTPUT_TOKENS', '1200')
            )
        except ValueError as error:
            raise ValueError(
                'VLM numeric settings must be integers'
            ) from error
        if not 1 <= timeout <= 300:
            raise ValueError('MALBUT_VLM_TIMEOUT_SECONDS must be 1..300')
        if not 128 <= max_tokens <= 5000:
            raise ValueError('MALBUT_VLM_MAX_OUTPUT_TOKENS must be 128..5000')
        return cls(
            provider=provider,
            model_id=(
                source.get('MALBUT_VLM_MODEL', '').strip()
                or defaults[provider]
            ),
            region=(
                source.get('MALBUT_VLM_REGION', '').strip()
                or region_defaults[provider]
            ),
            api_key=source.get('MALBUT_VLM_API_KEY', '').strip(),
            base_url=source.get('MALBUT_VLM_BASE_URL', '').strip(),
            timeout_seconds=timeout,
            max_output_tokens=max_tokens,
        )


def create_vlm_provider(
    settings: VlmSettings,
    *,
    nova_client: Optional[Any] = None,
    transport: Optional[Any] = None,
) -> VlmProvider:
    """Construct exactly one adapter; selection never changes the service."""
    if settings.provider == 'nova':
        return BedrockNovaVlmProvider(
            model_id=settings.model_id,
            region=settings.region,
            client=nova_client,
            timeout_seconds=settings.timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
    if settings.provider == 'gemini':
        return GeminiVlmProvider(
            api_key=settings.api_key,
            model_id=settings.model_id,
            timeout_seconds=settings.timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
            transport=transport,
        )
    if settings.provider == 'qwen':
        return OpenAiCompatibleVlmProvider(
            model_id=settings.model_id,
            base_url=QWEN_BASE_URL,
            api_key=settings.api_key,
            provider_name='alibaba-qwen',
            region=settings.region,
            timeout_seconds=settings.timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
            transport=transport,
        )
    if settings.provider == 'openai_compatible':
        if not settings.base_url:
            raise ValueError(
                'MALBUT_VLM_BASE_URL is required for openai_compatible'
            )
        return OpenAiCompatibleVlmProvider(
            model_id=settings.model_id,
            base_url=settings.base_url,
            api_key=settings.api_key,
            provider_name='self-hosted-vlm',
            region=settings.region,
            timeout_seconds=settings.timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
            transport=transport,
        )
    raise ValueError('unsupported VLM provider')
