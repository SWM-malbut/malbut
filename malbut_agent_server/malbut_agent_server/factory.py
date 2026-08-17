"""Construct provider, storage, and safety services."""

import hashlib
import threading
import unicodedata
from typing import Any, Optional, Tuple
import weakref

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.gateway import (
    CapabilityRegistry,
    production_registry,
    simulation_registry,
)
from malbut_agent_server.homecam_semantic import (
    AuthenticatedHomecamSemanticResolver,
    HomecamSemanticConfig,
    HomecamSemanticTransport,
)
from malbut_agent_server.gazebo_execution_outbox import (
    GazeboSimulationExecutionPolicy,
)
from malbut_agent_server.gazebo_prepare_dispatcher import (
    GazeboPrepareClient,
    GazeboPrepareDispatcher,
)
from malbut_agent_server.gazebo_simulation_authority import (
    ServerGazeboSimulationApprovalConsumer,
    ServerGazeboSimulationExecutionVerifier,
)
from malbut_agent_server.gazebo_simulation_execution import (
    GazeboSimulationExecutionError,
    GazeboSimulationExecutionSeam,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.providers.openai_responses import (
    OpenAIResponsesProvider,
)
from malbut_agent_server.providers.reliable import ReliableProvider
from malbut_agent_server.robot_state import (
    TrustedRobotStateSource,
    UnixSocketTrustedRobotStateSource,
)
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.speech import SpeechConversationCoordinator


CONFIRMATION_SWEEP_BATCH_SIZE = 1000
MAX_CONFIRMATION_SWEEP_BATCHES = 1000
_GAZEBO_RUNTIME_BINDING_LOCK = threading.RLock()
_GAZEBO_RUNTIME_BINDINGS: (
    'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]'
) = weakref.WeakKeyDictionary()


def get_gazebo_simulation_execution_seam(
    orchestrator: AgentOrchestrator,
) -> Optional[GazeboSimulationExecutionSeam]:
    """Return the factory-sealed seam after exact runtime attestation."""
    expected = None
    current = None
    try:
        with _GAZEBO_RUNTIME_BINDING_LOCK:
            expected = _GAZEBO_RUNTIME_BINDINGS.get(orchestrator)
        current = (
            object.__getattribute__(
                orchestrator,
                'gazebo_simulation_execution_seam',
            ),
            object.__getattribute__(orchestrator, 'conversation_store'),
        )
    except Exception:
        expected = None
        current = None
    if (
        type(orchestrator) is not AgentOrchestrator
        or expected is None
        or current is None
        or len(expected) != 3
        or current[0] is not expected[0]
        or current[1] is not expected[1]
    ):
        raise GazeboSimulationExecutionError(
            'gazebo_simulation_configuration_changed'
        )
    seam, store, user_id = expected
    if seam is not None and not (
        GazeboSimulationExecutionSeam.matches_runtime(
            seam,
            store,
            user_id,
        )
    ):
        raise GazeboSimulationExecutionError(
            'gazebo_simulation_configuration_changed'
        )
    return seam


def _openai_adapter(
    settings: Settings,
    model: str,
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
    )


def build_provider(settings: Settings) -> AgentProvider:
    """Build the selected provider without making a network request."""
    if settings.provider == 'mock':
        return MockProvider(
            max_model_input_chars=settings.max_model_input_chars,
        )
    if settings.provider != 'openai':
        raise ValueError('MALBUT_AGENT_PROVIDER is unsupported')
    if not settings.openai_api_key:
        raise ValueError('OPENAI_API_KEY is required')
    providers = [_openai_adapter(settings, settings.openai_model)]
    if settings.openai_fallback_model:
        providers.append(
            _openai_adapter(
                settings,
                settings.openai_fallback_model,
            )
        )
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


def build_capability_registry(
    settings: Settings,
) -> CapabilityRegistry:
    """Build Tool policy independently from the selected LLM provider."""
    if settings.tool_mode == 'proposal':
        return production_registry()
    if settings.tool_mode == 'simulation':
        return simulation_registry()
    raise ValueError('MALBUT_AGENT_TOOL_MODE is unsupported')


def build_monitor_room_target_resolver(
    settings: Settings,
    transport: Optional[HomecamSemanticTransport] = None,
) -> Optional[AuthenticatedHomecamSemanticResolver]:
    """Build the fixed Homecam binding or keep monitor_room fail-closed."""
    configured = (
        settings.homecam_origin,
        settings.homecam_agent_token,
        settings.homecam_signing_secret,
        settings.homecam_principal_subject_digest,
        settings.homecam_device_id,
    )
    if not any(configured):
        return None
    if not all(configured):
        raise ValueError('Homecam semantic configuration is incomplete')
    config = HomecamSemanticConfig(
        origin=settings.homecam_origin,
        service_token=settings.homecam_agent_token,
        envelope_signing_secret=settings.homecam_signing_secret,
        agent_user_id=settings.user_id,
        principal_subject_digest=(
            settings.homecam_principal_subject_digest
        ),
        device_id=settings.homecam_device_id,
        timeout_seconds=settings.homecam_timeout_seconds,
    )
    return AuthenticatedHomecamSemanticResolver(
        config,
        transport=transport,
    )


def _robot_state_binding_configured(settings: Settings) -> bool:
    """Validate the all-or-nothing fixed local state-source binding."""
    path = settings.robot_state_socket_path
    uid = settings.robot_state_expected_uid
    device_id = settings.robot_state_device_id
    configured = bool(path) or uid is not None or bool(device_id)
    complete = bool(path) and uid is not None and bool(device_id)
    if configured and not complete:
        raise ValueError(
            'trusted RobotState source configuration is incomplete'
        )
    return complete


def build_trusted_robot_state_source(
    settings: Settings,
) -> Optional[UnixSocketTrustedRobotStateSource]:
    """Build the fixed UDS state reader or leave state trust disabled."""
    if not _robot_state_binding_configured(settings):
        return None
    return UnixSocketTrustedRobotStateSource(
        socket_path=settings.robot_state_socket_path,
        expected_uid=settings.robot_state_expected_uid,
        expected_device_id=settings.robot_state_device_id,
        timeout_seconds=settings.robot_state_timeout_seconds,
    )


def _build_safety_policy(
    settings: Settings,
    state_source: Optional[TrustedRobotStateSource],
    resolver: Optional[AuthenticatedHomecamSemanticResolver] = None,
) -> SafetyPolicy:
    """Enable only explicitly configured, end-to-end bound room labels."""
    rooms = settings.monitorable_rooms
    if not isinstance(rooms, tuple):
        raise ValueError('monitorable room allowlist must be a tuple')
    if len(rooms) > 32:
        raise ValueError('monitorable room allowlist has too many items')
    if not all(
        isinstance(room, str)
        and room
        and len(room) <= 80
        and unicodedata.normalize('NFKC', room) == room
        and ' '.join(room.split()) == room
        and not any(
            unicodedata.category(character).startswith('C')
            for character in room
        )
        for room in rooms
    ):
        raise ValueError('monitorable room allowlist is invalid')
    if len({room.casefold() for room in rooms}) != len(rooms):
        raise ValueError('monitorable room allowlist contains duplicates')
    if rooms:
        if state_source is None:
            raise ValueError(
                'monitorable rooms require a trusted RobotState source'
            )
        if settings.robot_state_device_id != settings.homecam_device_id:
            raise ValueError(
                'Homecam and RobotState device IDs must match'
            )
        # Constructing the resolver validates the complete signed semantic
        # trust root without contacting Homecam.
        configured_resolver = (
            resolver
            if resolver is not None
            else build_monitor_room_target_resolver(settings)
        )
        if configured_resolver is None:
            raise ValueError(
                'monitorable rooms require a Homecam semantic resolver'
            )
    return SafetyPolicy(monitorable_locations=rooms)


def build_orchestrator(
    settings: Settings,
    trusted_robot_state_source: Optional[
        TrustedRobotStateSource
    ] = None,
    monitor_room_target_resolver: Optional[
        AuthenticatedHomecamSemanticResolver
    ] = None,
) -> AgentOrchestrator:
    """Build one runtime while keeping model output non-actuating."""
    if settings.enable_gazebo_simulation_execution:
        settings.validate_for_server()
    resolver = (
        monitor_room_target_resolver
        if monitor_room_target_resolver is not None
        else build_monitor_room_target_resolver(settings)
    )
    if (
        resolver is not None
        and type(resolver) is not AuthenticatedHomecamSemanticResolver
    ):
        raise TypeError(
            'monitor room target resolver must be authenticated'
        )
    state_source = (
        trusted_robot_state_source
        if trusted_robot_state_source is not None
        else build_trusted_robot_state_source(settings)
    )
    if (
        state_source is not None
        and not callable(getattr(state_source, 'read', None))
    ):
        raise TypeError('trusted RobotState source must provide read()')
    safety_policy = _build_safety_policy(
        settings,
        state_source,
        resolver,
    )
    verifier = None
    gazebo_policy = None
    if settings.enable_gazebo_simulation_execution:
        if resolver is None or state_source is None:
            raise ValueError(
                'Gazebo simulation execution trust roots are incomplete'
            )
        capability = hashlib.sha256(
            b'malbut-gazebo-simulation-authority-v1\0'
            + settings.gazebo_simulation_authority_secret.encode('ascii')
        ).digest()
        verifier = ServerGazeboSimulationExecutionVerifier(
            capability,
            user_id=settings.user_id,
            semantic_evidence_source=resolver,
        )
        gazebo_policy = GazeboSimulationExecutionPolicy(
            robot_id=settings.homecam_device_id,
            expected_device_id=settings.robot_state_device_id,
            semantic_evidence_source=resolver,
            robot_state_source=state_source,
        )
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
            simulation_execution_verifier=verifier,
            gazebo_execution_policy=gazebo_policy,
        )
        for _batch in range(MAX_CONFIRMATION_SWEEP_BATCHES):
            expired = (
                conversation_store.expire_due_confirmation_intents(
                    limit=CONFIRMATION_SWEEP_BATCH_SIZE,
                )
            )
            if len(expired) < CONFIRMATION_SWEEP_BATCH_SIZE:
                break
        else:
            raise RuntimeError(
                'confirmation expiry backlog exceeds startup limit'
            )
        orchestrator = AgentOrchestrator(
            provider=build_provider(settings),
            memory_store=memory_store,
            conversation_store=conversation_store,
            safety_policy=safety_policy,
            memory_limit=settings.memory_limit,
            trusted_robot_state_source=state_source,
            capability_registry=build_capability_registry(settings),
        )
        execution_seam = None
        if settings.enable_gazebo_simulation_execution:
            assert verifier is not None
            assert resolver is not None
            consumer = ServerGazeboSimulationApprovalConsumer(
                conversation_store,
                verifier,
                user_id=settings.user_id,
                semantic_evidence_source=resolver,
            )
            client = GazeboPrepareClient(
                settings.gazebo_prepare_socket_path,
                expected_gazebo_uid=(
                    settings.gazebo_prepare_expected_uid
                ),
                timeout_seconds=(
                    settings.gazebo_prepare_timeout_seconds
                ),
            )
            dispatcher = GazeboPrepareDispatcher(
                conversation_store,
                client,
                lease_seconds=settings.gazebo_prepare_lease_seconds,
            )
            execution_seam = GazeboSimulationExecutionSeam(
                consumer,
                dispatcher,
                user_id=settings.user_id,
            )
        orchestrator.gazebo_simulation_execution_seam = execution_seam
        with _GAZEBO_RUNTIME_BINDING_LOCK:
            _GAZEBO_RUNTIME_BINDINGS[orchestrator] = (
                execution_seam,
                conversation_store,
                settings.user_id,
            )
        return orchestrator
    except Exception:
        if conversation_store is not None:
            conversation_store.close()
        memory_store.close()
        raise


def build_speech_coordinator(
    settings: Settings,
    transport: Optional[HomecamSemanticTransport] = None,
    trusted_robot_state_source: Optional[
        TrustedRobotStateSource
    ] = None,
) -> SpeechConversationCoordinator:
    """Compose scripted speech with the authenticated semantic adapter."""
    # Validate and construct every side-effect-free remote binding before
    # opening SQLite or sweeping durable confirmation rows.  A malformed
    # Homecam origin or credential tuple must fail startup without creating
    # or mutating the configured database.
    resolver = build_monitor_room_target_resolver(
        settings,
        transport=transport,
    )
    state_source = (
        trusted_robot_state_source
        if trusted_robot_state_source is not None
        else build_trusted_robot_state_source(settings)
    )
    orchestrator = build_orchestrator(
        settings,
        trusted_robot_state_source=state_source,
        monitor_room_target_resolver=resolver,
    )
    try:
        return SpeechConversationCoordinator(
            orchestrator,
            monitor_room_target_resolver=resolver,
        )
    except Exception:
        orchestrator.conversation_store.close()
        orchestrator.memory_store.close()
        raise
