"""Construct provider, storage, and safety services."""

from malbut_agent_server.application.front_routing import (
    FrontRoutingService,
)
from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import (
    CapabilityRegistry,
    production_registry,
    simulation_registry,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.providers.routed import RoutedAgentProvider
from malbut_agent_server.ports.front_router import FrontRouterPort
from malbut_agent_server.rai_sidecar_client import (
    RaiSidecarClient,
    RaiSidecarProvider,
    SubprocessRaiSidecarTransport,
)
from malbut_agent_server.robot_state_source import RobotStateSource
from malbut_agent_server.safety import SafetyPolicy


RAI_SIDECAR_MODULE = 'malbut_agent_server.rai_sidecar_runtime'


def _openai_adapter(
    settings: Settings,
    model: str,
    *,
    include_reasoning: bool = True,
) -> OpenAIResponsesProvider:
    """Build one official-origin OpenAI model adapter."""
    return OpenAIResponsesProvider(
        api_key=settings.openai_api_key,
        model=model,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.request_timeout_seconds,
        max_model_input_chars=settings.max_model_input_chars,
        max_output_tokens=settings.openai_max_output_tokens,
        reasoning_effort=settings.openai_reasoning_effort,
        include_reasoning=include_reasoning,
    )


def _reliable_openai_provider(
    settings: Settings,
    primary_model: str,
    *,
    fallback_model: str = '',
    include_reasoning: bool = True,
) -> ReliableProvider:
    """Build one reliability boundary for an explicit model role."""
    providers = [_openai_adapter(
        settings,
        primary_model,
        include_reasoning=include_reasoning,
    )]
    if fallback_model:
        providers.append(_openai_adapter(
            settings,
            fallback_model,
            include_reasoning=include_reasoning,
        ))
    return ReliableProvider(
        providers,
        max_retries=settings.provider_max_retries,
        base_delay_seconds=(
            settings.provider_retry_base_delay_ms / 1000.0
        ),
        max_delay_seconds=(
            settings.provider_retry_max_delay_ms / 1000.0
        ),
        failure_threshold=settings.provider_failure_threshold,
        recovery_timeout_seconds=(
            settings.provider_recovery_timeout_seconds
        ),
        attempt_timeout_seconds=settings.request_timeout_seconds,
        total_timeout_seconds=(
            settings.provider_total_timeout_seconds
        ),
    )


def build_provider(settings: Settings) -> AgentProvider:
    """Build the selected provider without making a network request."""
    if settings.provider == 'mock':
        return MockProvider(
            max_model_input_chars=settings.max_model_input_chars,
        )
    if settings.provider == 'rai-sidecar':
        settings.validate_rai_sidecar()
        environment = {}
        if settings.openai_api_key:
            environment['OPENAI_API_KEY'] = settings.openai_api_key
        if settings.rai_model:
            environment['MALBUT_RAI_MODEL'] = settings.rai_model
        transport = SubprocessRaiSidecarTransport(
            (
                settings.rai_sidecar_python,
                '-I',
                '-m',
                RAI_SIDECAR_MODULE,
            ),
            environment=environment,
            working_directory=(
                settings.rai_sidecar_working_directory
            ),
        )
        return RaiSidecarProvider(
            RaiSidecarClient(
                transport,
                timeout_seconds=(
                    settings.rai_sidecar_timeout_seconds
                ),
            ),
            max_model_input_chars=settings.max_model_input_chars,
        )
    if settings.provider != 'openai':
        raise ValueError('MALBUT_AGENT_PROVIDER is unsupported')
    if not settings.openai_api_key:
        raise ValueError('OPENAI_API_KEY is required')
    return _reliable_openai_provider(
        settings,
        settings.openai_model,
        fallback_model=settings.openai_fallback_model,
    )


def _openai_role_providers(
    settings: Settings,
    fallback_provider: AgentProvider,
) -> tuple[AgentProvider, AgentProvider]:
    """Return isolated Chat and Planner roles only when configured."""
    role_models_configured = bool(
        settings.openai_general_model
        or settings.openai_robot_planner_model
    )
    if settings.provider != 'openai' or not role_models_configured:
        return fallback_provider, fallback_provider

    def build_role_provider(model: str) -> ReliableProvider:
        if model:
            return _reliable_openai_provider(
                settings,
                model,
                include_reasoning=False,
            )
        return _reliable_openai_provider(
            settings,
            settings.openai_model,
            fallback_model=settings.openai_fallback_model,
        )

    return (
        build_role_provider(settings.openai_general_model),
        build_role_provider(settings.openai_robot_planner_model),
    )


def build_capability_registry(
    settings: Settings,
) -> CapabilityRegistry:
    """Build Tool policy independently from the selected LLM provider."""
    if settings.tool_mode == 'proposal':
        return production_registry()
    if settings.tool_mode == 'simulation':
        return simulation_registry()
    raise ValueError('MALBUT_AGENT_TOOL_MODE is unsupported')


def build_orchestrator(
    settings: Settings,
    *,
    robot_state_source: RobotStateSource | None = None,
    front_router: FrontRouterPort | None = None,
) -> AgentOrchestrator:
    """Build one runtime while keeping model output non-actuating."""
    memory_store = SQLiteMemoryStore(settings.database_path)
    conversation_store = None
    try:
        conversation_store = SQLiteConversationStore(
            settings.database_path,
            ttl_seconds=settings.conversation_ttl_seconds,
            history_limit=settings.conversation_history_limit,
            max_sessions_per_user=(
                settings.max_conversation_sessions
            ),
            max_turns_per_session=settings.max_conversation_turns,
            summary_max_chars=(
                settings.conversation_summary_max_chars
            ),
        )
        provider = build_provider(settings)
        if front_router is not None:
            routing_service = FrontRoutingService(front_router)
            general_provider, planner_provider = (
                _openai_role_providers(settings, provider)
            )
            provider = RoutedAgentProvider(
                routing_service,
                general_provider=general_provider,
                robot_planner_provider=planner_provider,
                fallback_provider=provider,
            )
        return AgentOrchestrator(
            provider=provider,
            memory_store=memory_store,
            conversation_store=conversation_store,
            safety_policy=SafetyPolicy(),
            memory_limit=settings.memory_limit,
            trusted_robot_state=False,
            capability_registry=build_capability_registry(settings),
            robot_state_source=robot_state_source,
        )
    except Exception:
        if conversation_store is not None:
            conversation_store.close()
        memory_store.close()
        raise
