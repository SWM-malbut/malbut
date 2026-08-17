"""SQLite-backed, user-isolated short-term conversation sessions."""

import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from malbut_agent_server.execution_ledger import (
    DurableSimulationExecution,
    SimulationConsumeRequest,
    SimulationExecutionTrustVerifier,
    VerifiedSimulationApproval,
    _consume_approved_monitor_room_simulation_locked,
    _mark_confirmation_simulation_eligible_locked,
    prepare_simulation_schema_locked,
)
from malbut_agent_server.gazebo_execution_outbox import (
    GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES,
    GazeboExecutionAcknowledgement,
    GazeboExecutionClaim,
    GazeboPreparedExecutionAuthority,
    GazeboSimulationConsumeResult,
    GazeboSimulationExecutionPolicy,
    acknowledge_gazebo_execution_locked,
    claim_gazebo_execution_locked,
    get_gazebo_execution_enqueue_for_receipt_locked,
    prepare_gazebo_execution_outbox_schema_locked,
    record_gazebo_execution_outbox_locked,
    resolve_prepared_gazebo_execution_locked,
)
from malbut_agent_server.monitor_room_target import Effects, TargetBinding
from malbut_agent_server.schemas import (
    MAX_UTTERANCE_LENGTH,
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)
from malbut_agent_server.summarization import (
    ExtractiveConversationSummarizer,
    SummaryResult,
    SummarySourceTurn,
)
from malbut_agent_server.trusted_results import (
    TrustedToolResult,
    list_trusted_results_locked,
    prepare_trusted_result_schema_locked,
    record_or_verify_trusted_result_locked,
)
from malbut_agent_server.trusted_result_tts import (
    TrustedResultTTSClaim,
    TrustedResultTTSEvent,
    acknowledge_trusted_result_tts_locked,
    cancel_trusted_result_tts_locked,
    claim_trusted_result_tts_locked,
    prepare_trusted_result_tts_schema_locked,
    record_or_verify_trusted_result_tts_locked,
)


MAX_RESPONSE_JSON_LENGTH = 65536
DEFAULT_SUMMARY_MAX_CHARS = 2000
SUMMARY_UPDATE_BATCH_SIZE = 128
CONFIRMATION_SERVER_RESPONSE_ID_PREFIX = 'confirmation-expiry-'
CONFIRMATION_REQUEST_SCHEMA_VERSION = 3
LEGACY_CONFIRMATION_REQUEST_SCHEMA_VERSION = 2
LEGACY_BOUND_CONFIRMATION_STORAGE_SCHEMA_VERSION = 2
CONFIRMATION_STORAGE_SCHEMA_VERSION = 3
WAL_INITIALIZATION_TIMEOUT_SECONDS = 5.0
WAL_INITIALIZATION_RETRY_SECONDS = 0.01
_COMMAND_DURABILITY_SEAL_LOCK = threading.RLock()
_COMMAND_DURABILITY_SEALS: (
    'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]'
) = weakref.WeakKeyDictionary()


LEGACY_CONFIRMATION_INTENTS_TABLE_SQL = '''
CREATE TABLE confirmation_intents (
    schema_version INTEGER NOT NULL,
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    agent_request_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    speech_session_id TEXT NOT NULL,
    source_utterance_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    turn_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL UNIQUE,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    risk_level TEXT NOT NULL,
    state TEXT NOT NULL,
    disposition TEXT,
    requested_disposition TEXT,
    result_code TEXT,
    confirmation_result_id TEXT UNIQUE,
    response_id TEXT,
    response_fingerprint TEXT,
    response_channel TEXT,
    assurance_level TEXT,
    provenance_ref TEXT,
    verifier_ref TEXT,
    resolved_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    authority_kind TEXT NOT NULL DEFAULT 'none',
    eligible_for_execution INTEGER NOT NULL DEFAULT 0,
    execution_authorized INTEGER NOT NULL DEFAULT 0,
    CHECK (schema_version = 2),
    CHECK (generation >= 1),
    CHECK (revision >= 1),
    CHECK (ordinal >= 1),
    CHECK (expires_at > issued_at),
    CHECK (risk_level = 'L3'),
    CHECK (state IN (
        'pending', 'resolved', 'invalidated'
    )),
    CHECK (
        disposition IS NULL OR disposition IN (
            'approve', 'deny', 'cancel', 'expired'
        )
    ),
    CHECK (
        requested_disposition IS NULL
        OR requested_disposition IN ('approve', 'deny', 'cancel')
    ),
    CHECK (
        response_channel IS NULL OR response_channel IN (
            'voice', 'ui_in_process', 'server_expiry'
        )
    ),
    CHECK (
        assurance_level IS NULL OR assurance_level IN (
            'local_speech_binding',
            'unverified_in_process_ui',
            'server_clock'
        )
    ),
    CHECK (authority_kind = 'none'),
    CHECK (eligible_for_execution = 0),
    CHECK (execution_authorized = 0),
    CHECK (
        (state = 'pending'
         AND disposition IS NULL
         AND requested_disposition IS NULL
         AND result_code IS NULL
         AND confirmation_result_id IS NULL
         AND response_id IS NULL
         AND response_fingerprint IS NULL
         AND response_channel IS NULL
         AND assurance_level IS NULL
         AND provenance_ref IS NULL
         AND verifier_ref IS NULL
         AND resolved_at IS NULL)
        OR
        (state = 'resolved'
         AND disposition IS NOT NULL
         AND requested_disposition IS NOT NULL
         AND result_code IS NOT NULL
         AND confirmation_result_id IS NOT NULL
         AND response_id IS NOT NULL
         AND response_fingerprint IS NOT NULL
         AND response_channel IS NOT NULL
         AND assurance_level IS NOT NULL
         AND provenance_ref IS NOT NULL
         AND resolved_at IS NOT NULL)
        OR
        (state = 'invalidated'
         AND disposition IS NULL
         AND result_code IS NOT NULL
         AND confirmation_result_id IS NULL
         AND (
            (response_id IS NULL
             AND requested_disposition IS NULL
             AND response_fingerprint IS NULL
             AND response_channel IS NULL
             AND assurance_level IS NULL
             AND provenance_ref IS NULL
             AND verifier_ref IS NULL)
            OR
            (response_id IS NOT NULL
             AND requested_disposition IS NOT NULL
             AND response_fingerprint IS NOT NULL
             AND response_channel IS NOT NULL
             AND assurance_level IS NOT NULL
             AND provenance_ref IS NOT NULL)
         )
         AND resolved_at IS NOT NULL)
    )
)
'''

CONFIRMATION_RESPONSE_OWNER_INDEX_SQL = '''
CREATE UNIQUE INDEX confirmation_response_owner_idx
ON confirmation_intents (user_id, response_id)
WHERE response_id IS NOT NULL
'''

CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL = '''
CREATE UNIQUE INDEX confirmation_one_pending_session_idx
ON confirmation_intents (
    user_id,
    conversation_id,
    session_instance_id
)
WHERE state = 'pending'
'''

LEGACY_CONFIRMATION_SCHEMA_METADATA_TABLE_SQL = '''
CREATE TABLE confirmation_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY
        CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
        CHECK (schema_version = 1)
)
'''


CONFIRMATION_INTENTS_TABLE_STORAGE_V2_SQL = '''
CREATE TABLE confirmation_intents (
    schema_version INTEGER NOT NULL,
    confirmation_request_id TEXT NOT NULL PRIMARY KEY,
    agent_request_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    speech_session_id TEXT NOT NULL,
    source_utterance_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    turn_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    proposal_fingerprint TEXT NOT NULL UNIQUE,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    risk_level TEXT NOT NULL,
    confirmation_message TEXT,
    target_binding_schema_version INTEGER,
    target_device_id TEXT,
    target_device_binding_revision TEXT,
    target_source_revision TEXT,
    target_map_id TEXT,
    target_map_revision TEXT,
    target_semantic_revision TEXT,
    target_frame_id TEXT,
    target_room_id TEXT,
    target_room_name TEXT,
    target_room_category TEXT,
    target_geometry_json TEXT,
    target_geometry_digest TEXT,
    target_representative_x REAL,
    target_representative_y REAL,
    target_clearance_m REAL,
    target_area_m2 REAL,
    target_source_arguments_digest TEXT,
    target_binding_digest TEXT,
    effects_schema_version INTEGER,
    effect_physical_navigation INTEGER,
    effect_camera_capture INTEGER,
    effect_external_video_stream INTEGER,
    effect_video_recording INTEGER,
    effect_audio_capture INTEGER,
    effect_coverage_mode TEXT,
    effect_viewer_scope TEXT,
    effect_talkback_allowed INTEGER,
    effect_max_duration_seconds INTEGER,
    effects_digest TEXT,
    state TEXT NOT NULL,
    disposition TEXT,
    requested_disposition TEXT,
    result_code TEXT,
    confirmation_result_id TEXT UNIQUE,
    response_id TEXT,
    response_fingerprint TEXT,
    response_channel TEXT,
    assurance_level TEXT,
    provenance_ref TEXT,
    verifier_ref TEXT,
    resolved_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    authority_kind TEXT NOT NULL DEFAULT 'none',
    eligible_for_execution INTEGER NOT NULL DEFAULT 0,
    execution_authorized INTEGER NOT NULL DEFAULT 0,
    CHECK (schema_version IN (2, 3)),
    CHECK (generation >= 1),
    CHECK (revision >= 1),
    CHECK (ordinal >= 1),
    CHECK (expires_at > issued_at),
    CHECK (risk_level = 'L3'),
    CHECK (
        (schema_version = 2
         AND confirmation_message IS NULL
         AND target_binding_schema_version IS NULL
         AND target_device_id IS NULL
         AND target_device_binding_revision IS NULL
         AND target_source_revision IS NULL
         AND target_map_id IS NULL
         AND target_map_revision IS NULL
         AND target_semantic_revision IS NULL
         AND target_frame_id IS NULL
         AND target_room_id IS NULL
         AND target_room_name IS NULL
         AND target_room_category IS NULL
         AND target_geometry_json IS NULL
         AND target_geometry_digest IS NULL
         AND target_representative_x IS NULL
         AND target_representative_y IS NULL
         AND target_clearance_m IS NULL
         AND target_area_m2 IS NULL
         AND target_source_arguments_digest IS NULL
         AND target_binding_digest IS NULL
         AND effects_schema_version IS NULL
         AND effect_physical_navigation IS NULL
         AND effect_camera_capture IS NULL
         AND effect_external_video_stream IS NULL
         AND effect_video_recording IS NULL
         AND effect_audio_capture IS NULL
         AND effect_coverage_mode IS NULL
         AND effect_viewer_scope IS NULL
         AND effect_talkback_allowed IS NULL
         AND effect_max_duration_seconds IS NULL
         AND effects_digest IS NULL)
        OR
        (schema_version = 3
         AND tool_name = 'monitor_room'
         AND confirmation_message IS NOT NULL
         AND target_binding_schema_version = 1
         AND target_device_id IS NOT NULL
         AND target_device_binding_revision IS NOT NULL
         AND target_source_revision IS NOT NULL
         AND target_map_id IS NOT NULL
         AND target_map_revision IS NOT NULL
         AND target_semantic_revision IS NOT NULL
         AND target_frame_id = 'map'
         AND target_room_id IS NOT NULL
         AND target_room_name IS NOT NULL
         AND target_room_category IS NOT NULL
         AND target_geometry_json IS NOT NULL
         AND length(target_geometry_digest) = 64
         AND target_geometry_digest NOT GLOB '*[^0-9a-f]*'
         AND target_representative_x IS NOT NULL
         AND target_representative_y IS NOT NULL
         AND target_clearance_m > 0
         AND target_area_m2 > 0
         AND target_source_arguments_digest = arguments_digest
         AND length(target_binding_digest) = 64
         AND target_binding_digest NOT GLOB '*[^0-9a-f]*'
         AND effects_schema_version = 1
         AND effect_physical_navigation IN (0, 1)
         AND effect_camera_capture IN (0, 1)
         AND effect_external_video_stream IN (0, 1)
         AND effect_video_recording IN (0, 1)
         AND effect_audio_capture IN (0, 1)
         AND effect_coverage_mode = 'whole_room'
         AND effect_viewer_scope = 'requesting_user'
         AND effect_talkback_allowed IN (0, 1)
         AND effect_max_duration_seconds BETWEEN 1 AND 3600
         AND length(effects_digest) = 64
         AND effects_digest NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (state IN (
        'pending', 'resolved', 'invalidated'
    )),
    CHECK (
        disposition IS NULL OR disposition IN (
            'approve', 'deny', 'cancel', 'expired'
        )
    ),
    CHECK (
        requested_disposition IS NULL
        OR requested_disposition IN ('approve', 'deny', 'cancel')
    ),
    CHECK (
        response_channel IS NULL OR response_channel IN (
            'voice', 'ui_in_process', 'server_expiry'
        )
    ),
    CHECK (
        assurance_level IS NULL OR assurance_level IN (
            'local_speech_binding',
            'unverified_in_process_ui',
            'server_clock'
        )
    ),
    CHECK (authority_kind = 'none'),
    CHECK (eligible_for_execution = 0),
    CHECK (execution_authorized = 0),
    CHECK (
        (state = 'pending'
         AND disposition IS NULL
         AND requested_disposition IS NULL
         AND result_code IS NULL
         AND confirmation_result_id IS NULL
         AND response_id IS NULL
         AND response_fingerprint IS NULL
         AND response_channel IS NULL
         AND assurance_level IS NULL
         AND provenance_ref IS NULL
         AND verifier_ref IS NULL
         AND resolved_at IS NULL)
        OR
        (state = 'resolved'
         AND disposition IS NOT NULL
         AND requested_disposition IS NOT NULL
         AND result_code IS NOT NULL
         AND confirmation_result_id IS NOT NULL
         AND response_id IS NOT NULL
         AND response_fingerprint IS NOT NULL
         AND response_channel IS NOT NULL
         AND assurance_level IS NOT NULL
         AND provenance_ref IS NOT NULL
         AND resolved_at IS NOT NULL)
        OR
        (state = 'invalidated'
         AND disposition IS NULL
         AND result_code IS NOT NULL
         AND confirmation_result_id IS NULL
         AND (
            (response_id IS NULL
             AND requested_disposition IS NULL
             AND response_fingerprint IS NULL
             AND response_channel IS NULL
             AND assurance_level IS NULL
             AND provenance_ref IS NULL
             AND verifier_ref IS NULL)
            OR
            (response_id IS NOT NULL
             AND requested_disposition IS NOT NULL
             AND response_fingerprint IS NOT NULL
             AND response_channel IS NOT NULL
             AND assurance_level IS NOT NULL
             AND provenance_ref IS NOT NULL)
         )
         AND resolved_at IS NOT NULL)
    )
)
'''


_CONFIRMATION_STORAGE_V2_EFFECTS_CHECK = '''         AND effects_schema_version = 1
         AND effect_physical_navigation IN (0, 1)
         AND effect_camera_capture IN (0, 1)
         AND effect_external_video_stream IN (0, 1)
         AND effect_video_recording IN (0, 1)
         AND effect_audio_capture IN (0, 1)
         AND effect_coverage_mode = 'whole_room'
         AND effect_viewer_scope = 'requesting_user'
         AND effect_talkback_allowed IN (0, 1)'''
_CONFIRMATION_STORAGE_V3_EFFECTS_CHECK = '''         AND effects_schema_version IN (1, 2)
         AND effect_physical_navigation IN (0, 1)
         AND effect_camera_capture IN (0, 1)
         AND effect_external_video_stream IN (0, 1)
         AND effect_video_recording IN (0, 1)
         AND effect_audio_capture IN (0, 1)
         AND effect_coverage_mode = 'whole_room'
         AND effect_viewer_scope = 'requesting_user'
         AND effect_talkback_allowed IN (0, 1)
         AND (
             effects_schema_version = 1
             OR (
                 effects_schema_version = 2
                 AND effect_physical_navigation = 0
                 AND effect_camera_capture = 0
                 AND effect_external_video_stream = 0
                 AND effect_video_recording = 0
                 AND effect_audio_capture = 0
                 AND effect_talkback_allowed = 0
             )
         )'''
CONFIRMATION_INTENTS_TABLE_SQL = (
    CONFIRMATION_INTENTS_TABLE_STORAGE_V2_SQL.replace(
        _CONFIRMATION_STORAGE_V2_EFFECTS_CHECK,
        _CONFIRMATION_STORAGE_V3_EFFECTS_CHECK,
    )
)
if (
    CONFIRMATION_INTENTS_TABLE_SQL
    == CONFIRMATION_INTENTS_TABLE_STORAGE_V2_SQL
):
    raise RuntimeError('confirmation storage schema replacement failed')


CONFIRMATION_SCHEMA_METADATA_STORAGE_V2_TABLE_SQL = '''
CREATE TABLE confirmation_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY
        CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
        CHECK (schema_version = 2)
)
'''


CONFIRMATION_SCHEMA_METADATA_TABLE_SQL = '''
CREATE TABLE confirmation_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY
        CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
        CHECK (schema_version = 3)
)
'''


class ConversationNotFoundError(ValidationError):
    """Raised when a user-scoped conversation does not exist."""


class ConversationStateError(ValidationError):
    """Raised when a closed or expired conversation receives a turn."""


class ConversationConflictError(ValidationError):
    """Raised when a request or turn identifier is reused differently."""


class ConversationChangedError(ValidationError):
    """Raised when a session changes while model inference is running."""


class ConversationClockError(RuntimeError):
    """Raised when the server clock cannot provide a finite timestamp."""


class ConversationDurabilityError(RuntimeError):
    """Raised when command execution loses its exact SQLite disk binding."""

    def __init__(self) -> None:
        super().__init__('conversation command durability is unavailable')

    def __getattribute__(self, name: str) -> Any:
        """Do not expose filesystem or SQLite details through exceptions."""
        if name in {'__cause__', '__context__', '__traceback__'}:
            return None
        return super().__getattribute__(name)


class ConfirmationSchemaError(RuntimeError):
    """Raised when the durable confirmation schema is incompatible."""


class ConfirmationIntentConflictError(ValidationError):
    """Raised when a durable confirmation identifier is reused differently."""


class ConfirmationIntentNotFoundError(ValidationError):
    """Raised when a durable confirmation request does not exist."""


class ConfirmationIntentAlreadyTerminalError(ValidationError):
    """Raised when a different response lost the terminal race."""


class ConfirmationReservedResponseIdError(ValidationError):
    """Raised when a user channel claims a server-owned identifier."""


@dataclass(frozen=True)
class ConversationSession:
    """One user-scoped conversation lifecycle record."""

    conversation_id: str
    user_id: str
    session_instance_id: str
    status: str
    generation: int
    revision: int
    created_at: float
    updated_at: float
    expires_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe session summary."""
        return {
            'conversation_id': self.conversation_id,
            'user_id': self.user_id,
            'session_instance_id': self.session_instance_id,
            'status': self.status,
            'generation': self.generation,
            'revision': self.revision,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'expires_at': self.expires_at,
        }


@dataclass(frozen=True)
class ConversationTurn:
    """One committed user and assistant exchange."""

    conversation_id: str
    user_id: str
    session_instance_id: str
    turn_id: str
    request_id: str
    request_fingerprint: str
    generation: int
    ordinal: int
    user_content: str
    assistant_content: str
    response: Dict[str, Any]
    created_at: float
    completed_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe turn without raw provider proposals."""
        return {
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'turn_id': self.turn_id,
            'request_id': self.request_id,
            'generation': self.generation,
            'ordinal': self.ordinal,
            'user': self.user_content,
            'assistant': self.assistant_content,
            'decision': dict(
                self.response.get('decision', {})
            ),
            'created_at': self.created_at,
            'completed_at': self.completed_at,
        }

    def to_messages(self) -> List[Dict[str, Any]]:
        """Return ordered user and assistant message records."""
        return [
            {
                'sequence': self.ordinal * 2 - 1,
                'turn_id': self.turn_id,
                'role': 'user',
                'content': self.user_content,
                'created_at': self.created_at,
            },
            {
                'sequence': self.ordinal * 2,
                'turn_id': self.turn_id,
                'role': 'assistant',
                'content': self.assistant_content,
                'created_at': self.completed_at,
            },
        ]


@dataclass(frozen=True)
class ConversationSummary:
    """Bounded derived context for turns older than the raw window."""

    summary_id: str
    user_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    summary_revision: int
    content: str
    source_start_ordinal: int
    source_end_ordinal: int
    source_turn_count: int
    source_digest: str
    summarizer: str
    fallback_used: bool
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return summary content with complete source provenance."""
        return {
            'summary_id': self.summary_id,
            'user_id': self.user_id,
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'generation': self.generation,
            'summary_revision': self.summary_revision,
            'content': self.content,
            'source_start_ordinal': self.source_start_ordinal,
            'source_end_ordinal': self.source_end_ordinal,
            'source_turn_count': self.source_turn_count,
            'source_digest': self.source_digest,
            'summarizer': self.summarizer,
            'fallback_used': self.fallback_used,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass(frozen=True)
class BeginTurnToken:
    """Immutable compare-and-swap token for one in-flight turn."""

    user_id: str
    conversation_id: str
    session_instance_id: str
    turn_id: str
    request_id: str
    request_fingerprint: str
    generation: int
    revision: int
    ordinal: int


@dataclass(frozen=True)
class BeginTurnResult:
    """Either a new in-flight token or a durable cached response."""

    session: ConversationSession
    history: Tuple[ConversationTurn, ...]
    summary: Optional[ConversationSummary]
    token: Optional[BeginTurnToken]
    cached_response: Optional[Dict[str, Any]]
    trusted_results: Tuple[TrustedToolResult, ...] = ()


@dataclass(frozen=True)
class ConversationSnapshot:
    """One generation-consistent session, history, and summary view."""

    session: ConversationSession
    turns: Tuple[ConversationTurn, ...]
    summary: Optional[ConversationSummary]
    trusted_results: Tuple[TrustedToolResult, ...] = ()


@dataclass(frozen=True)
class ConfirmationIntentDraft:
    """Content-minimized confirmation request for atomic persistence."""

    schema_version: int
    confirmation_request_id: str
    agent_request_id: str
    user_id: str
    speech_session_id: str
    source_utterance_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    revision: int
    ordinal: int
    turn_id: str
    decision_id: str
    tool_name: str
    arguments_digest: str
    proposal_fingerprint: str
    issued_at: float
    expires_at: float
    risk_level: str
    confirmation_message: Optional[str] = None
    target_binding_schema_version: Optional[int] = None
    target_device_id: Optional[str] = None
    target_device_binding_revision: Optional[str] = None
    target_source_revision: Optional[str] = None
    target_map_id: Optional[str] = None
    target_map_revision: Optional[str] = None
    target_semantic_revision: Optional[str] = None
    target_frame_id: Optional[str] = None
    target_room_id: Optional[str] = None
    target_room_name: Optional[str] = None
    target_room_category: Optional[str] = None
    target_geometry_json: Optional[str] = None
    target_geometry_digest: Optional[str] = None
    target_representative_x: Optional[float] = None
    target_representative_y: Optional[float] = None
    target_clearance_m: Optional[float] = None
    target_area_m2: Optional[float] = None
    target_source_arguments_digest: Optional[str] = None
    target_binding_digest: Optional[str] = None
    effects_schema_version: Optional[int] = None
    effect_physical_navigation: Optional[bool] = None
    effect_camera_capture: Optional[bool] = None
    effect_external_video_stream: Optional[bool] = None
    effect_video_recording: Optional[bool] = None
    effect_audio_capture: Optional[bool] = None
    effect_coverage_mode: Optional[str] = None
    effect_viewer_scope: Optional[str] = None
    effect_talkback_allowed: Optional[bool] = None
    effect_max_duration_seconds: Optional[int] = None
    effects_digest: Optional[str] = None


@dataclass(frozen=True)
class DurableConfirmationIntent:
    """Durable non-authorizing request and its optional terminal intent."""

    schema_version: int
    confirmation_request_id: str
    agent_request_id: str
    user_id: str
    speech_session_id: str
    source_utterance_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    revision: int
    ordinal: int
    turn_id: str
    decision_id: str
    tool_name: str
    arguments_digest: str
    proposal_fingerprint: str
    issued_at: float
    expires_at: float
    risk_level: str
    confirmation_message: Optional[str]
    target_binding_schema_version: Optional[int]
    target_device_id: Optional[str]
    target_device_binding_revision: Optional[str]
    target_source_revision: Optional[str]
    target_map_id: Optional[str]
    target_map_revision: Optional[str]
    target_semantic_revision: Optional[str]
    target_frame_id: Optional[str]
    target_room_id: Optional[str]
    target_room_name: Optional[str]
    target_room_category: Optional[str]
    target_geometry_json: Optional[str]
    target_geometry_digest: Optional[str]
    target_representative_x: Optional[float]
    target_representative_y: Optional[float]
    target_clearance_m: Optional[float]
    target_area_m2: Optional[float]
    target_source_arguments_digest: Optional[str]
    target_binding_digest: Optional[str]
    effects_schema_version: Optional[int]
    effect_physical_navigation: Optional[bool]
    effect_camera_capture: Optional[bool]
    effect_external_video_stream: Optional[bool]
    effect_video_recording: Optional[bool]
    effect_audio_capture: Optional[bool]
    effect_coverage_mode: Optional[str]
    effect_viewer_scope: Optional[str]
    effect_talkback_allowed: Optional[bool]
    effect_max_duration_seconds: Optional[int]
    effects_digest: Optional[str]
    state: str
    disposition: Optional[str]
    requested_disposition: Optional[str]
    result_code: Optional[str]
    confirmation_result_id: Optional[str]
    response_id: Optional[str]
    response_fingerprint: Optional[str]
    response_channel: Optional[str]
    assurance_level: Optional[str]
    provenance_ref: Optional[str]
    verifier_ref: Optional[str]
    resolved_at: Optional[float]
    created_at: float
    updated_at: float

    def to_public_dict(self) -> Dict[str, Any]:
        """Return a content-free record with authority fixed to none."""
        return {
            'schema_version': self.schema_version,
            'confirmation_request_id': self.confirmation_request_id,
            'confirmation_result_id': self.confirmation_result_id,
            'state': self.state,
            'disposition': self.disposition,
            'code': self.result_code,
            'resolved_at': self.resolved_at,
            'response_channel': self.response_channel,
            'target_binding': {
                'bound': self.schema_version == 3,
                'binding_digest': self.target_binding_digest,
                'effects_digest': self.effects_digest,
            },
            'authority': {
                'kind': 'none',
                'eligible_for_execution': False,
                'execution_authorized': False,
                'consume_once': False,
                'tool_call_id': None,
                'mission_id': None,
            },
        }

    def reconstruct_target_binding(self) -> TargetBinding:
        """Rebuild the persisted target without granting any authority."""
        if self.schema_version != CONFIRMATION_REQUEST_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation target binding is unavailable'
            )
        effects = Effects(
            schema_version=self.effects_schema_version,
            physical_navigation=self.effect_physical_navigation,
            camera_capture=self.effect_camera_capture,
            external_video_stream=self.effect_external_video_stream,
            video_recording=self.effect_video_recording,
            audio_capture=self.effect_audio_capture,
            max_duration_seconds=self.effect_max_duration_seconds,
            coverage_mode=self.effect_coverage_mode,
            viewer_scope=self.effect_viewer_scope,
            talkback_allowed=self.effect_talkback_allowed,
        )
        target = TargetBinding(
            schema_version=self.target_binding_schema_version,
            device_id=self.target_device_id,
            device_binding_revision=(
                self.target_device_binding_revision
            ),
            source_revision=self.target_source_revision,
            map_id=self.target_map_id,
            map_revision=self.target_map_revision,
            semantic_revision=self.target_semantic_revision,
            frame_id=self.target_frame_id,
            room_id=self.target_room_id,
            room_name=self.target_room_name,
            room_category=self.target_room_category,
            source_arguments_digest=(
                self.target_source_arguments_digest
            ),
            geometry_json=self.target_geometry_json,
            geometry_digest=self.target_geometry_digest,
            representative_point=(
                self.target_representative_x,
                self.target_representative_y,
            ),
            clearance_m=self.target_clearance_m,
            area_m2=self.target_area_m2,
            effects=effects,
        )
        if (
            target.binding_digest != self.target_binding_digest
            or target.effects_digest != self.effects_digest
        ):
            raise ValidationError(
                'confirmation target binding does not match its digest'
            )
        return target


def _command_durability_failure() -> ConversationDurabilityError:
    return ConversationDurabilityError()


def _command_database_path(value: Any) -> Path:
    """Return one absolute lexical path without following the DB leaf."""
    if type(value) is not str or not value or value == ':memory:':
        raise _command_durability_failure()
    try:
        expanded = Path(value).expanduser()
        absolute = (
            expanded
            if expanded.is_absolute()
            else Path.cwd() / expanded
        )
        normalized = Path(os.path.abspath(os.fspath(absolute)))
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _command_durability_failure() from None
    if not normalized.is_absolute() or normalized.name in {'', '.', '..'}:
        raise _command_durability_failure()
    return normalized


def _command_private_file_identity(path: Path) -> Tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError:
        raise _command_durability_failure() from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise _command_durability_failure()
    return (int(metadata.st_dev), int(metadata.st_ino))


def _command_protected_directory_chain(path: Path) -> None:
    """Reject symlinked or attacker-writable components above the DB."""
    current = Path(path.anchor)
    parts = path.parts[1:-1]
    for part in parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError:
            raise _command_durability_failure() from None
        mode = stat.S_IMODE(metadata.st_mode)
        sticky_root = (
            bool(metadata.st_mode & stat.S_ISVTX)
            and metadata.st_uid == 0
        )
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or (mode & 0o022 and not sticky_root)
        ):
            raise _command_durability_failure()
    try:
        parent = os.lstat(path.parent)
    except OSError:
        raise _command_durability_failure() from None
    if (
        stat.S_ISLNK(parent.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise _command_durability_failure()


def _capture_command_database_binding(
    store: 'SQLiteConversationStore',
) -> Tuple[Any, ...]:
    """Pin the live connection, lock, canonical path, and WAL files."""
    connection = object.__getattribute__(store, '_connection')
    lock = object.__getattribute__(store, '_lock')
    database_path = object.__getattribute__(store, 'database_path')
    if database_path == ':memory:':
        return (
            connection,
            lock,
            database_path,
            None,
            None,
            None,
            None,
        )
    path = _command_database_path(database_path)
    main_identity = _command_private_file_identity(path)
    sidecars = []
    for suffix in ('-wal', '-shm'):
        candidate = Path(f'{path}{suffix}')
        sidecars.append(
            _command_private_file_identity(candidate)
            if os.path.lexists(os.fspath(candidate))
            else None
        )
    return (
        connection,
        lock,
        database_path,
        str(path),
        main_identity,
        sidecars[0],
        sidecars[1],
    )


def _attest_command_database_binding_locked(
    store: 'SQLiteConversationStore',
    binding: Tuple[Any, ...],
) -> None:
    """Fail closed if SQLite no longer names the pinned private inode."""
    if (
        type(store) is not SQLiteConversationStore
        or type(binding) is not tuple
        or len(binding) != 7
        or binding[3] is None
        or binding[4] is None
        or object.__getattribute__(store, '_connection') is not binding[0]
        or object.__getattribute__(store, '_lock') is not binding[1]
        or object.__getattribute__(store, 'database_path') != binding[2]
        or object.__getattribute__(
            store, '_command_durability_binding'
        ) is not binding
    ):
        raise _command_durability_failure()
    connection = binding[0]
    path = Path(binding[3])
    if (
        connection.row_factory is not sqlite3.Row
        or connection.text_factory is not str
        or connection.isolation_level != ''
    ):
        raise _command_durability_failure()
    _command_protected_directory_chain(path)
    if _command_private_file_identity(path) != binding[4]:
        raise _command_durability_failure()
    for suffix, expected in zip(('-wal', '-shm'), binding[5:7]):
        candidate = Path(f'{path}{suffix}')
        if expected is None:
            if os.path.lexists(os.fspath(candidate)):
                # A sidecar appearing after the store's initialization is
                # not pinned to the connection that was originally sealed.
                raise _command_durability_failure()
        elif _command_private_file_identity(candidate) != expected:
            raise _command_durability_failure()
    try:
        databases = connection.execute('PRAGMA database_list').fetchall()
        main = [row for row in databases if row[1] == 'main']
        unexpected = [
            row for row in databases
            if row[1] != 'main'
            and not (row[1] == 'temp' and row[2] == '')
        ]
        if (
            len(main) != 1
            or os.path.abspath(str(main[0][2])) != str(path)
            or unexpected
        ):
            raise _command_durability_failure()
        for pragma_name, expected in (
            ('foreign_keys', 1),
            ('query_only', 0),
            ('synchronous', 2),
        ):
            value = connection.execute(
                f'PRAGMA {pragma_name}'
            ).fetchone()
            if value is None or value[0] != expected:
                raise _command_durability_failure()
        journal = connection.execute('PRAGMA journal_mode').fetchone()
        if journal is None or str(journal[0]).lower() != 'wal':
            raise _command_durability_failure()
    except ConversationDurabilityError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise _command_durability_failure() from None


class SQLiteConversationStore:
    """Thread-safe source of truth for short-term conversation history."""

    def __init__(
        self,
        database_path: str,
        ttl_seconds: int = 1800,
        history_limit: int = 10,
        max_sessions_per_user: int = 100,
        max_turns_per_session: int = 1000,
        summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
        summarizer: Optional[
            ExtractiveConversationSummarizer
        ] = None,
        clock: Callable[[], float] = time.time,
        simulation_execution_verifier: Optional[
            SimulationExecutionTrustVerifier
        ] = None,
        gazebo_execution_policy: Optional[
            GazeboSimulationExecutionPolicy
        ] = None,
    ) -> None:
        """Open a database and initialize the conversation schema."""
        if not database_path:
            raise ValueError('database_path must not be empty')
        self.ttl_seconds = self._bounded_integer(
            ttl_seconds,
            'ttl_seconds',
            60,
            2592000,
        )
        self.history_limit = self._bounded_integer(
            history_limit,
            'history_limit',
            10,
            50,
        )
        self.max_sessions_per_user = self._bounded_integer(
            max_sessions_per_user,
            'max_sessions_per_user',
            1,
            1000,
        )
        self.max_turns_per_session = self._bounded_integer(
            max_turns_per_session,
            'max_turns_per_session',
            10,
            10000,
        )
        self.summary_max_chars = self._bounded_integer(
            summary_max_chars,
            'summary_max_chars',
            256,
            8000,
        )
        self._summarizer = (
            summarizer
            if summarizer is not None
            else ExtractiveConversationSummarizer()
        )
        self.database_path = database_path
        self._clock = clock
        if (
            simulation_execution_verifier is not None
            and (
                not callable(
                    getattr(simulation_execution_verifier, 'verify', None)
                )
                or not callable(
                    getattr(
                        simulation_execution_verifier,
                        'verify_receipt',
                        None,
                    )
                )
            )
        ):
            raise TypeError(
                'simulation_execution_verifier must implement '
                'verify and verify_receipt'
            )
        self._simulation_execution_verifier = (
            simulation_execution_verifier
        )
        if (
            gazebo_execution_policy is not None
            and type(gazebo_execution_policy)
            is not GazeboSimulationExecutionPolicy
        ):
            raise TypeError(
                'gazebo_execution_policy must be a fixed '
                'GazeboSimulationExecutionPolicy'
            )
        self._gazebo_execution_policy = gazebo_execution_policy
        if database_path != ':memory:':
            Path(database_path).expanduser().parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )
        self._connection = sqlite3.connect(
            str(Path(database_path).expanduser())
            if database_path != ':memory:'
            else database_path,
            check_same_thread=False,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._initialize()
        except Exception:
            self._connection.rollback()
            self._connection.close()
            raise
        try:
            self._secure_file_permissions()
            binding = _capture_command_database_binding(self)
            self._command_durability_binding = binding
            with _COMMAND_DURABILITY_SEAL_LOCK:
                _COMMAND_DURABILITY_SEALS[self] = binding
        except Exception:
            self._connection.close()
            raise

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute('PRAGMA busy_timeout=5000')
            if self.database_path != ':memory:':
                self._enable_wal_with_retry()
            metadata_version = None
            metadata_exists = self._connection.execute(
                '''
                SELECT 1 FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'confirmation_schema_metadata'
                '''
            ).fetchone()
            if metadata_exists is not None:
                try:
                    metadata_row = self._connection.execute(
                        '''
                        SELECT schema_version
                        FROM confirmation_schema_metadata
                        WHERE singleton = 1
                        '''
                    ).fetchone()
                    if metadata_row is not None:
                        metadata_version = metadata_row['schema_version']
                except sqlite3.Error:
                    metadata_version = None
            confirmation_schema_migration = metadata_version in {
                1,
                LEGACY_BOUND_CONFIRMATION_STORAGE_SCHEMA_VERSION,
            }
            if confirmation_schema_migration:
                # SQLite otherwise rewrites foreign keys and trigger SQL to
                # the temporary backup table during ALTER TABLE RENAME.
                # The controlled migration performs a full foreign-key check
                # before commit and restores normal enforcement afterward.
                self._connection.execute('PRAGMA foreign_keys=OFF')
                self._connection.execute('PRAGMA legacy_alter_table=ON')
            else:
                self._connection.execute('PRAGMA foreign_keys=ON')
            self._connection.execute('BEGIN IMMEDIATE')
            fresh_confirmation_schema = (
                self._prepare_confirmation_schema_locked()
            )
            self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (user_id, conversation_id),
                    CHECK (status IN ('active', 'closed', 'expired'))
                )
                '''
            )
            self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    user_content TEXT NOT NULL,
                    assistant_content TEXT,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    PRIMARY KEY (
                        user_id,
                        conversation_id,
                        generation,
                        turn_id
                    ),
                    UNIQUE (user_id, request_id),
                    UNIQUE (
                        user_id,
                        conversation_id,
                        generation,
                        ordinal
                    ),
                    FOREIGN KEY (user_id, conversation_id)
                        REFERENCES conversation_sessions (
                            user_id,
                            conversation_id
                        )
                        ON DELETE CASCADE,
                    CHECK (status IN ('pending', 'completed'))
                )
                '''
            )
            self._ensure_session_instance_columns()
            self._connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    session_instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    summary_id TEXT NOT NULL,
                    summary_revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    source_start_ordinal INTEGER NOT NULL,
                    source_end_ordinal INTEGER NOT NULL,
                    source_turn_count INTEGER NOT NULL,
                    source_digest TEXT NOT NULL,
                    summarizer TEXT NOT NULL,
                    fallback_used INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (
                        user_id,
                        conversation_id,
                        session_instance_id,
                        generation
                    ),
                    FOREIGN KEY (user_id, conversation_id)
                        REFERENCES conversation_sessions (
                            user_id,
                            conversation_id
                        )
                        ON DELETE CASCADE
                )
                '''
            )
            if fresh_confirmation_schema:
                self._connection.execute(
                    CONFIRMATION_INTENTS_TABLE_SQL
                )
            self._connection.execute(
                '''
                CREATE INDEX IF NOT EXISTS conversation_turns_order_idx
                ON conversation_turns (
                    user_id,
                    conversation_id,
                    generation,
                    ordinal DESC
                )
                '''
            )
            self._connection.execute(
                '''
                CREATE UNIQUE INDEX IF NOT EXISTS
                    conversation_one_pending_idx
                ON conversation_turns (user_id, conversation_id)
                WHERE status = 'pending'
                '''
            )
            if fresh_confirmation_schema:
                self._connection.execute(
                    CONFIRMATION_RESPONSE_OWNER_INDEX_SQL
                )
                self._connection.execute(
                    CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL
                )
                self._connection.execute(
                    CONFIRMATION_SCHEMA_METADATA_TABLE_SQL
                )
                self._connection.execute(
                    '''
                    INSERT INTO confirmation_schema_metadata (
                        singleton, schema_version
                    ) VALUES (1, ?)
                    ''',
                    (CONFIRMATION_STORAGE_SCHEMA_VERSION,),
                )
            self._validate_confirmation_schema_locked()
            try:
                simulation_activated_at = float(self._clock())
            except (OverflowError, TypeError, ValueError):
                simulation_activated_at = 0.0
            if (
                not math.isfinite(simulation_activated_at)
                or simulation_activated_at < 0
            ):
                # Preserve the conversation store's established contract:
                # invalid application clocks fail on the first lifecycle
                # operation, not while merely opening durable storage.  The
                # rowid cutoff and random epoch still prevent legacy upgrade.
                simulation_activated_at = 0.0
            prepare_simulation_schema_locked(
                self._connection,
                activated_at=simulation_activated_at,
            )
            prepare_gazebo_execution_outbox_schema_locked(
                self._connection,
                activated_at=simulation_activated_at,
            )
            prepare_trusted_result_schema_locked(
                self._connection,
                activated_at=simulation_activated_at,
            )
            prepare_trusted_result_tts_schema_locked(
                self._connection,
                activated_at=simulation_activated_at,
            )
            if confirmation_schema_migration:
                violations = self._connection.execute(
                    'PRAGMA foreign_key_check'
                ).fetchall()
                if violations:
                    raise ConfirmationSchemaError(
                        'confirmation migration broke foreign keys'
                    )
            self._connection.commit()
            if confirmation_schema_migration:
                self._connection.execute('PRAGMA legacy_alter_table=OFF')
                self._connection.execute('PRAGMA foreign_keys=ON')

    def _enable_wal_with_retry(self) -> None:
        """Converge concurrent file initializers on WAL mode."""
        deadline = (
            time.monotonic() + WAL_INITIALIZATION_TIMEOUT_SECONDS
        )
        while True:
            try:
                current = self._connection.execute(
                    'PRAGMA journal_mode'
                ).fetchone()
                if current is not None and str(current[0]).lower() == 'wal':
                    return
                selected = self._connection.execute(
                    'PRAGMA journal_mode=WAL'
                ).fetchone()
                if (
                    selected is not None
                    and str(selected[0]).lower() == 'wal'
                ):
                    return
                raise sqlite3.OperationalError(
                    'database did not enter WAL mode'
                )
            except sqlite3.OperationalError as error:
                if str(error).lower() not in {
                    'database is locked',
                    'database table is locked',
                }:
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(WAL_INITIALIZATION_RETRY_SECONDS)

    def _prepare_confirmation_schema_locked(self) -> bool:
        """Classify a legacy database before any confirmation DDL runs."""
        rows = self._connection.execute(
            '''
            SELECT type, name
            FROM sqlite_master
            WHERE name IN (
                'confirmation_intents',
                'confirmation_schema_metadata'
            )
            '''
        ).fetchall()
        objects = {
            str(row['name']): str(row['type'])
            for row in rows
        }
        if not objects:
            return True
        expected = {
            'confirmation_intents': 'table',
            'confirmation_schema_metadata': 'table',
        }
        if objects != expected:
            raise ConfirmationSchemaError(
                'confirmation schema is incomplete'
            )
        schema_version = self._confirmation_metadata_version_locked()
        if schema_version == 1:
            self._validate_legacy_confirmation_schema_locked()
            self._migrate_confirmation_schema_v1_locked()
        elif (
            schema_version
            == LEGACY_BOUND_CONFIRMATION_STORAGE_SCHEMA_VERSION
        ):
            self._validate_confirmation_storage_v2_locked()
            self._migrate_confirmation_schema_v2_locked()
        elif schema_version == CONFIRMATION_STORAGE_SCHEMA_VERSION:
            self._validate_confirmation_schema_locked()
        else:
            raise ConfirmationSchemaError(
                'confirmation schema metadata is incompatible'
            )
        return False

    def _confirmation_metadata_version_locked(self) -> int:
        """Read one exact integer schema marker without coercion."""
        try:
            rows = self._connection.execute(
                '''
                SELECT singleton,
                       typeof(singleton) AS singleton_type,
                       schema_version,
                       typeof(schema_version) AS schema_version_type
                FROM confirmation_schema_metadata
                ORDER BY singleton
                '''
            ).fetchall()
        except sqlite3.Error:
            raise ConfirmationSchemaError(
                'confirmation schema metadata is incompatible'
            ) from None
        if (
            len(rows) != 1
            or rows[0]['singleton'] != 1
            or rows[0]['singleton_type'] != 'integer'
            or rows[0]['schema_version_type'] != 'integer'
            or isinstance(rows[0]['schema_version'], bool)
            or not isinstance(rows[0]['schema_version'], int)
        ):
            raise ConfirmationSchemaError(
                'confirmation schema metadata is incompatible'
            )
        return rows[0]['schema_version']

    def _validate_legacy_confirmation_schema_locked(self) -> None:
        """Accept only the exact v1 storage schema before migrating it."""
        try:
            table_row = self._connection.execute(
                '''
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'confirmation_intents'
                '''
            ).fetchone()
            metadata_row = self._connection.execute(
                '''
                SELECT sql FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'confirmation_schema_metadata'
                '''
            ).fetchone()
            if (
                self._strict_schema_sql(table_row['sql'])
                != LEGACY_CONFIRMATION_INTENTS_TABLE_SQL.strip()
                or self._strict_schema_sql(metadata_row['sql'])
                != LEGACY_CONFIRMATION_SCHEMA_METADATA_TABLE_SQL.strip()
            ):
                raise ConfirmationSchemaError(
                    'legacy confirmation schema is incompatible'
                )
            self._validate_confirmation_indexes_locked()
            if self._connection.execute(
                'PRAGMA foreign_key_list(confirmation_intents)'
            ).fetchall():
                raise ConfirmationSchemaError(
                    'legacy confirmation schema has unexpected foreign keys'
                )
            trigger = self._connection.execute(
                '''
                SELECT 1 FROM sqlite_master
                WHERE type = 'trigger'
                  AND tbl_name IN (
                    'confirmation_intents',
                    'confirmation_schema_metadata'
                  )
                LIMIT 1
                '''
            ).fetchone()
            unsafe = self._connection.execute(
                '''
                SELECT 1 FROM confirmation_intents
                WHERE schema_version != 2
                   OR authority_kind != 'none'
                   OR eligible_for_execution != 0
                   OR execution_authorized != 0
                LIMIT 1
                '''
            ).fetchone()
            if trigger is not None or unsafe is not None:
                raise ConfirmationSchemaError(
                    'legacy confirmation schema is unsafe'
                )
        except ConfirmationSchemaError:
            raise
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            raise ConfirmationSchemaError(
                'legacy confirmation schema is incompatible'
            ) from None

    def _migrate_confirmation_schema_v1_locked(self) -> None:
        """Rebuild v1 storage while preserving terminal audit rows."""
        now = self._now()
        self._connection.execute(
            '''
            UPDATE confirmation_intents
            SET state = 'invalidated',
                result_code = 'confirmation_binding_upgrade_required',
                resolved_at = ?,
                updated_at = ?
            WHERE schema_version = 2 AND state = 'pending'
            ''',
            (now, now),
        )
        self._connection.execute(
            '''
            ALTER TABLE confirmation_intents
            RENAME TO confirmation_intents_v1_backup
            '''
        )
        self._connection.execute(CONFIRMATION_INTENTS_TABLE_SQL)
        legacy_columns = (
            'schema_version',
            'confirmation_request_id',
            'agent_request_id',
            'user_id',
            'speech_session_id',
            'source_utterance_id',
            'conversation_id',
            'session_instance_id',
            'generation',
            'revision',
            'ordinal',
            'turn_id',
            'decision_id',
            'tool_name',
            'arguments_digest',
            'proposal_fingerprint',
            'issued_at',
            'expires_at',
            'risk_level',
            'state',
            'disposition',
            'requested_disposition',
            'result_code',
            'confirmation_result_id',
            'response_id',
            'response_fingerprint',
            'response_channel',
            'assurance_level',
            'provenance_ref',
            'verifier_ref',
            'resolved_at',
            'created_at',
            'updated_at',
            'authority_kind',
            'eligible_for_execution',
            'execution_authorized',
        )
        joined = ', '.join(legacy_columns)
        self._connection.execute(
            f'''
            INSERT INTO confirmation_intents ({joined})
            SELECT {joined} FROM confirmation_intents_v1_backup
            '''
        )
        self._connection.execute(
            'DROP TABLE confirmation_intents_v1_backup'
        )
        self._connection.execute(
            CONFIRMATION_RESPONSE_OWNER_INDEX_SQL
        )
        self._connection.execute(
            CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL
        )
        self._connection.execute(
            'DROP TABLE confirmation_schema_metadata'
        )
        self._connection.execute(
            CONFIRMATION_SCHEMA_METADATA_TABLE_SQL
        )
        self._connection.execute(
            '''
            INSERT INTO confirmation_schema_metadata (
                singleton, schema_version
            ) VALUES (1, ?)
            ''',
            (CONFIRMATION_STORAGE_SCHEMA_VERSION,),
        )
        self._validate_confirmation_schema_locked()

    def _validate_confirmation_storage_v2_locked(self) -> None:
        """Validate the exact effects-v1 schema before widening it."""
        try:
            table_row = self._connection.execute(
                '''
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'confirmation_intents'
                '''
            ).fetchone()
            metadata_row = self._connection.execute(
                '''
                SELECT sql FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'confirmation_schema_metadata'
                '''
            ).fetchone()
            if (
                self._strict_schema_sql(table_row['sql'])
                != CONFIRMATION_INTENTS_TABLE_STORAGE_V2_SQL.strip()
                or self._strict_schema_sql(metadata_row['sql'])
                != CONFIRMATION_SCHEMA_METADATA_STORAGE_V2_TABLE_SQL.strip()
            ):
                raise ConfirmationSchemaError(
                    'confirmation storage v2 schema is incompatible'
                )
            self._validate_confirmation_indexes_locked()
            if self._connection.execute(
                'PRAGMA foreign_key_list(confirmation_intents)'
            ).fetchall():
                raise ConfirmationSchemaError(
                    'confirmation storage v2 has unexpected foreign keys'
                )
            trigger = self._connection.execute(
                '''
                SELECT 1 FROM sqlite_master
                WHERE type = 'trigger'
                  AND tbl_name IN (
                    'confirmation_intents',
                    'confirmation_schema_metadata'
                  )
                LIMIT 1
                '''
            ).fetchone()
            unsafe = self._connection.execute(
                '''
                SELECT 1 FROM confirmation_intents
                WHERE authority_kind != 'none'
                   OR eligible_for_execution != 0
                   OR execution_authorized != 0
                   OR effects_schema_version NOT IN (1)
                LIMIT 1
                '''
            ).fetchone()
            if trigger is not None or unsafe is not None:
                raise ConfirmationSchemaError(
                    'confirmation storage v2 is unsafe'
                )
        except ConfirmationSchemaError:
            raise
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            raise ConfirmationSchemaError(
                'confirmation storage v2 schema is incompatible'
            ) from None

    def _migrate_confirmation_schema_v2_locked(self) -> None:
        """Widen only the durable Effects discriminator to version 2."""
        dependent_triggers = tuple(
            (str(row['name']), str(row['sql']))
            for row in self._connection.execute(
                '''
                SELECT name, sql FROM sqlite_master
                WHERE type = 'trigger'
                  AND sql IS NOT NULL
                  AND tbl_name NOT IN (
                    'confirmation_intents',
                    'confirmation_schema_metadata'
                  )
                  AND instr(lower(sql), 'confirmation_intents') > 0
                ORDER BY name
                '''
            ).fetchall()
        )
        for name, _sql in dependent_triggers:
            quoted_name = name.replace('"', '""')
            self._connection.execute(
                f'DROP TRIGGER "{quoted_name}"'
            )
        columns = tuple(
            str(row['name'])
            for row in self._connection.execute(
                'PRAGMA table_info(confirmation_intents)'
            ).fetchall()
        )
        if not columns:
            raise ConfirmationSchemaError(
                'confirmation storage v2 schema is incompatible'
            )
        joined = ', '.join(columns)
        self._connection.execute(
            '''
            ALTER TABLE confirmation_intents
            RENAME TO confirmation_intents_v2_backup
            '''
        )
        self._connection.execute(CONFIRMATION_INTENTS_TABLE_SQL)
        self._connection.execute(
            f'''
            INSERT INTO confirmation_intents ({joined})
            SELECT {joined} FROM confirmation_intents_v2_backup
            '''
        )
        self._connection.execute(
            'DROP TABLE confirmation_intents_v2_backup'
        )
        self._connection.execute(CONFIRMATION_RESPONSE_OWNER_INDEX_SQL)
        self._connection.execute(CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL)
        self._connection.execute(
            'DROP TABLE confirmation_schema_metadata'
        )
        self._connection.execute(CONFIRMATION_SCHEMA_METADATA_TABLE_SQL)
        self._connection.execute(
            '''
            INSERT INTO confirmation_schema_metadata (
                singleton, schema_version
            ) VALUES (1, ?)
            ''',
            (CONFIRMATION_STORAGE_SCHEMA_VERSION,),
        )
        for _name, trigger_sql in dependent_triggers:
            self._connection.execute(trigger_sql)
        self._validate_confirmation_schema_locked()

    def _validate_confirmation_schema_locked(self) -> None:
        """Fail closed on a malformed or incompatible confirmation schema."""
        expected_columns = (
            ('schema_version', 'INTEGER', 1, None, 0),
            ('confirmation_request_id', 'TEXT', 1, None, 1),
            ('agent_request_id', 'TEXT', 1, None, 0),
            ('user_id', 'TEXT', 1, None, 0),
            ('speech_session_id', 'TEXT', 1, None, 0),
            ('source_utterance_id', 'TEXT', 1, None, 0),
            ('conversation_id', 'TEXT', 1, None, 0),
            ('session_instance_id', 'TEXT', 1, None, 0),
            ('generation', 'INTEGER', 1, None, 0),
            ('revision', 'INTEGER', 1, None, 0),
            ('ordinal', 'INTEGER', 1, None, 0),
            ('turn_id', 'TEXT', 1, None, 0),
            ('decision_id', 'TEXT', 1, None, 0),
            ('tool_name', 'TEXT', 1, None, 0),
            ('arguments_digest', 'TEXT', 1, None, 0),
            ('proposal_fingerprint', 'TEXT', 1, None, 0),
            ('issued_at', 'REAL', 1, None, 0),
            ('expires_at', 'REAL', 1, None, 0),
            ('risk_level', 'TEXT', 1, None, 0),
            ('confirmation_message', 'TEXT', 0, None, 0),
            ('target_binding_schema_version', 'INTEGER', 0, None, 0),
            ('target_device_id', 'TEXT', 0, None, 0),
            ('target_device_binding_revision', 'TEXT', 0, None, 0),
            ('target_source_revision', 'TEXT', 0, None, 0),
            ('target_map_id', 'TEXT', 0, None, 0),
            ('target_map_revision', 'TEXT', 0, None, 0),
            ('target_semantic_revision', 'TEXT', 0, None, 0),
            ('target_frame_id', 'TEXT', 0, None, 0),
            ('target_room_id', 'TEXT', 0, None, 0),
            ('target_room_name', 'TEXT', 0, None, 0),
            ('target_room_category', 'TEXT', 0, None, 0),
            ('target_geometry_json', 'TEXT', 0, None, 0),
            ('target_geometry_digest', 'TEXT', 0, None, 0),
            ('target_representative_x', 'REAL', 0, None, 0),
            ('target_representative_y', 'REAL', 0, None, 0),
            ('target_clearance_m', 'REAL', 0, None, 0),
            ('target_area_m2', 'REAL', 0, None, 0),
            ('target_source_arguments_digest', 'TEXT', 0, None, 0),
            ('target_binding_digest', 'TEXT', 0, None, 0),
            ('effects_schema_version', 'INTEGER', 0, None, 0),
            ('effect_physical_navigation', 'INTEGER', 0, None, 0),
            ('effect_camera_capture', 'INTEGER', 0, None, 0),
            ('effect_external_video_stream', 'INTEGER', 0, None, 0),
            ('effect_video_recording', 'INTEGER', 0, None, 0),
            ('effect_audio_capture', 'INTEGER', 0, None, 0),
            ('effect_coverage_mode', 'TEXT', 0, None, 0),
            ('effect_viewer_scope', 'TEXT', 0, None, 0),
            ('effect_talkback_allowed', 'INTEGER', 0, None, 0),
            ('effect_max_duration_seconds', 'INTEGER', 0, None, 0),
            ('effects_digest', 'TEXT', 0, None, 0),
            ('state', 'TEXT', 1, None, 0),
            ('disposition', 'TEXT', 0, None, 0),
            ('requested_disposition', 'TEXT', 0, None, 0),
            ('result_code', 'TEXT', 0, None, 0),
            ('confirmation_result_id', 'TEXT', 0, None, 0),
            ('response_id', 'TEXT', 0, None, 0),
            ('response_fingerprint', 'TEXT', 0, None, 0),
            ('response_channel', 'TEXT', 0, None, 0),
            ('assurance_level', 'TEXT', 0, None, 0),
            ('provenance_ref', 'TEXT', 0, None, 0),
            ('verifier_ref', 'TEXT', 0, None, 0),
            ('resolved_at', 'REAL', 0, None, 0),
            ('created_at', 'REAL', 1, None, 0),
            ('updated_at', 'REAL', 1, None, 0),
            ('authority_kind', 'TEXT', 1, "'none'", 0),
            ('eligible_for_execution', 'INTEGER', 1, '0', 0),
            ('execution_authorized', 'INTEGER', 1, '0', 0),
        )
        metadata_columns = (
            ('singleton', 'INTEGER', 1, None, 1),
            ('schema_version', 'INTEGER', 1, None, 0),
        )
        try:
            columns = tuple(
                (
                    str(row['name']),
                    str(row['type']).upper(),
                    int(row['notnull']),
                    row['dflt_value'],
                    int(row['pk']),
                )
                for row in self._connection.execute(
                    'PRAGMA table_info(confirmation_intents)'
                ).fetchall()
            )
            metadata = tuple(
                (
                    str(row['name']),
                    str(row['type']).upper(),
                    int(row['notnull']),
                    row['dflt_value'],
                    int(row['pk']),
                )
                for row in self._connection.execute(
                    'PRAGMA table_info(confirmation_schema_metadata)'
                ).fetchall()
            )
            if columns != expected_columns or metadata != metadata_columns:
                raise ConfirmationSchemaError(
                    'confirmation schema is incompatible'
                )
            metadata_rows = self._connection.execute(
                '''
                SELECT singleton,
                       typeof(singleton) AS singleton_type,
                       schema_version,
                       typeof(schema_version) AS schema_version_type
                FROM confirmation_schema_metadata
                ORDER BY singleton
                '''
            ).fetchall()
            if (
                len(metadata_rows) != 1
                or metadata_rows[0]['singleton'] != 1
                or metadata_rows[0]['singleton_type'] != 'integer'
                or metadata_rows[0]['schema_version']
                != CONFIRMATION_STORAGE_SCHEMA_VERSION
                or metadata_rows[0]['schema_version_type'] != 'integer'
            ):
                raise ConfirmationSchemaError(
                    'confirmation schema metadata is incompatible'
                )
            self._validate_confirmation_indexes_locked()
            table_row = self._connection.execute(
                '''
                SELECT sql
                FROM sqlite_master
                WHERE type = 'table' AND name = 'confirmation_intents'
                '''
            ).fetchone()
            metadata_row = self._connection.execute(
                '''
                SELECT sql
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'confirmation_schema_metadata'
                '''
            ).fetchone()
            table_sql = self._strict_schema_sql(table_row['sql'])
            metadata_sql = self._strict_schema_sql(
                metadata_row['sql']
            )
            if table_sql != CONFIRMATION_INTENTS_TABLE_SQL.strip():
                raise ConfirmationSchemaError(
                    'confirmation schema constraints are incompatible'
                )
            if (
                metadata_sql
                != CONFIRMATION_SCHEMA_METADATA_TABLE_SQL.strip()
            ):
                raise ConfirmationSchemaError(
                    'confirmation schema metadata is incompatible'
                )
            foreign_keys = self._connection.execute(
                'PRAGMA foreign_key_list(confirmation_intents)'
            ).fetchall()
            if foreign_keys:
                raise ConfirmationSchemaError(
                    'confirmation schema has unexpected foreign keys'
                )
            trigger = self._connection.execute(
                '''
                SELECT 1
                FROM sqlite_master
                WHERE type = 'trigger'
                  AND tbl_name IN (
                    'confirmation_intents',
                    'confirmation_schema_metadata'
                  )
                LIMIT 1
                '''
            ).fetchone()
            if trigger is not None:
                raise ConfirmationSchemaError(
                    'confirmation schema has unexpected triggers'
                )
            unsafe = self._connection.execute(
                '''
                SELECT 1
                FROM confirmation_intents
                WHERE authority_kind != 'none'
                   OR eligible_for_execution != 0
                   OR execution_authorized != 0
                LIMIT 1
                '''
            ).fetchone()
            if unsafe is not None:
                raise ConfirmationSchemaError(
                    'confirmation schema contains execution authority'
                )
        except ConfirmationSchemaError:
            raise
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            raise ConfirmationSchemaError(
                'confirmation schema is incompatible'
            ) from None

    def _validate_confirmation_indexes_locked(self) -> None:
        """Verify the identity and terminal-winner unique indexes."""
        rows = self._connection.execute(
            'PRAGMA index_list(confirmation_intents)'
        ).fetchall()
        indexes = {
            str(row['name']): row
            for row in rows
        }
        expected_named = {
            'confirmation_response_owner_idx': (
                ('user_id', 'response_id'),
                CONFIRMATION_RESPONSE_OWNER_INDEX_SQL.strip(),
            ),
            'confirmation_one_pending_session_idx': (
                (
                    'user_id',
                    'conversation_id',
                    'session_instance_id',
                ),
                CONFIRMATION_ONE_PENDING_SESSION_INDEX_SQL.strip(),
            ),
        }
        for name, (expected_fields, expected_sql) in expected_named.items():
            row = indexes.get(name)
            if (
                row is None
                or int(row['unique']) != 1
                or str(row['origin']) != 'c'
                or int(row['partial']) != 1
            ):
                raise ConfirmationSchemaError(
                    'confirmation schema index is incompatible'
                )
            fields = tuple(
                str(field['name'])
                for field in self._connection.execute(
                    '''
                    SELECT name
                    FROM pragma_index_info(?)
                    ORDER BY seqno
                    ''',
                    (name,),
                ).fetchall()
            )
            sql_row = self._connection.execute(
                '''
                SELECT sql FROM sqlite_master
                WHERE type = 'index' AND name = ?
                ''',
                (name,),
            ).fetchone()
            sql = self._strict_schema_sql(sql_row['sql'])
            if fields != expected_fields or sql != expected_sql:
                raise ConfirmationSchemaError(
                    'confirmation schema index is incompatible'
                )
        required_unique_fields = {
            ('confirmation_request_id',),
            ('decision_id',),
            ('proposal_fingerprint',),
            ('confirmation_result_id',),
        }
        actual_unique_fields = set()
        for row in rows:
            if int(row['unique']) != 1 or int(row['partial']) != 0:
                continue
            fields = tuple(
                str(field['name'])
                for field in self._connection.execute(
                    '''
                    SELECT name
                    FROM pragma_index_info(?)
                    ORDER BY seqno
                    ''',
                    (str(row['name']),),
                ).fetchall()
            )
            actual_unique_fields.add(fields)
        if not required_unique_fields.issubset(actual_unique_fields):
            raise ConfirmationSchemaError(
                'confirmation schema identity index is incompatible'
            )

    @staticmethod
    def _strict_schema_sql(value: Any) -> str:
        """Return stored DDL without weakening literal semantics."""
        if not isinstance(value, str) or not value:
            raise ConfirmationSchemaError(
                'confirmation schema definition is missing'
            )
        return value.strip()

    def _ensure_session_instance_columns(self) -> None:
        """Add opaque session identities to databases from version 0.2."""
        session_columns = {
            row['name']
            for row in self._connection.execute(
                'PRAGMA table_info(conversation_sessions)'
            ).fetchall()
        }
        if 'session_instance_id' not in session_columns:
            self._connection.execute(
                '''
                ALTER TABLE conversation_sessions
                ADD COLUMN session_instance_id TEXT
                '''
            )
        turn_columns = {
            row['name']
            for row in self._connection.execute(
                'PRAGMA table_info(conversation_turns)'
            ).fetchall()
        }
        if 'session_instance_id' not in turn_columns:
            self._connection.execute(
                '''
                ALTER TABLE conversation_turns
                ADD COLUMN session_instance_id TEXT
                '''
            )
        rows = self._connection.execute(
            '''
            SELECT user_id, conversation_id, session_instance_id
            FROM conversation_sessions
            '''
        ).fetchall()
        for row in rows:
            instance_id = row['session_instance_id']
            if not instance_id:
                instance_id = str(uuid.uuid4())
                self._connection.execute(
                    '''
                    UPDATE conversation_sessions
                    SET session_instance_id = ?
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (
                        instance_id,
                        row['user_id'],
                        row['conversation_id'],
                    ),
                )
            self._connection.execute(
                '''
                UPDATE conversation_turns
                SET session_instance_id = ?
                WHERE user_id = ? AND conversation_id = ?
                  AND (
                      session_instance_id IS NULL
                      OR session_instance_id = ''
                  )
                ''',
                (
                    instance_id,
                    row['user_id'],
                    row['conversation_id'],
                ),
            )

    def _secure_file_permissions(self) -> None:
        if self.database_path == ':memory:':
            return
        expanded = str(Path(self.database_path).expanduser())
        for suffix in ('', '-wal', '-shm'):
            candidate = expanded + suffix
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def attest_command_boundary_durability(self) -> None:
        """
        Prove this file-backed store is safe before a UDS command.

        This is intentionally a read-only runtime check.  Initialization may
        provision private modes, but a later permission, path, inode, WAL, or
        PRAGMA drift is never repaired and never followed by an external
        command.
        """
        binding = None
        try:
            with _COMMAND_DURABILITY_SEAL_LOCK:
                binding = _COMMAND_DURABILITY_SEALS.get(self)
        except Exception:
            binding = None
        if type(binding) is not tuple or len(binding) != 7:
            raise _command_durability_failure()
        lock = binding[1]
        if not hasattr(lock, '__enter__'):
            raise _command_durability_failure()
        with lock:
            _attest_command_database_binding_locked(self, binding)

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()

    def create(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
    ) -> ConversationSession:
        """Create or idempotently return one active session."""
        normalized_user = validate_user_id(user_id)
        normalized_id = (
            validate_conversation_id(conversation_id)
            if conversation_id is not None
            else str(uuid.uuid4())
        )
        now = self._now()
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is not None:
                    session = self._session_from_row(row)
                    if session.status == 'active':
                        self._connection.commit()
                        return session
                    raise ConversationConflictError(
                        'conversation_id belongs to a closed or '
                        'expired session'
                    )
                count_row = self._connection.execute(
                    '''
                    SELECT COUNT(*) AS session_count
                    FROM conversation_sessions
                    WHERE user_id = ?
                    ''',
                    (normalized_user,),
                ).fetchone()
                if (
                    int(count_row['session_count'])
                    >= self.max_sessions_per_user
                ):
                    raise ConversationStateError(
                        'conversation session limit reached; '
                        'delete an old session'
                    )
                self._connection.execute(
                    '''
                    INSERT INTO conversation_sessions (
                        user_id,
                        conversation_id,
                        session_instance_id,
                        status,
                        generation,
                        revision,
                        created_at,
                        updated_at,
                        expires_at
                    )
                    VALUES (?, ?, ?, 'active', 1, 0, ?, ?, ?)
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        str(uuid.uuid4()),
                        now,
                        now,
                        now + self.ttl_seconds,
                    ),
                )
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                self._connection.commit()
                return self._session_from_row(row)
            except Exception:
                self._connection.rollback()
                raise

    def get(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Return one session after lazily expiring due sessions."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        now = self._now()
        with self._lock:
            self._expire_and_commit(now)
            row = self._select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            return self._session_from_row(row)

    def get_summary(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[ConversationSummary]:
        """Return the summary for the current session generation."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._expire_and_commit(self._now())
            row = self._select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            session = self._session_from_row(row)
            summary_row = self._select_summary_locked(session)
            return (
                self._summary_from_row(summary_row)
                if summary_row is not None
                else None
            )

    def snapshot(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> ConversationSnapshot:
        """Read session, turns, and summary in one transaction."""
        normalized_limit = self._bounded_integer(
            limit,
            'turn limit',
            1,
            500,
        )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'conversation was not found'
                    )
                session = self._session_from_row(row)
                turns = self._history_locked(
                    normalized_user,
                    normalized_id,
                    session.session_instance_id,
                    session.generation,
                    normalized_limit,
                )
                summary_row = self._select_summary_locked(session)
                summary = (
                    self._summary_from_row(summary_row)
                    if summary_row is not None
                    else None
                )
                trusted_results = list_trusted_results_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    limit=normalized_limit,
                )
                self._connection.commit()
                return ConversationSnapshot(
                    session=session,
                    turns=tuple(turns),
                    summary=summary,
                    trusted_results=trusted_results,
                )
            except Exception:
                self._connection.rollback()
                raise

    def begin_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
        request_id: str,
        request_fingerprint: str,
        user_content: str,
        expected_session_instance_id: Optional[str] = None,
    ) -> BeginTurnResult:
        """
        Reserve one ordered turn or return its durable response.

        ``expected_session_instance_id`` is trusted server context.  Voice
        adapters use it to prevent an old speech capability from attaching
        itself to a newly created lifecycle that reused the same public
        conversation id.
        """
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_turn = validate_turn_id(turn_id)
        normalized_request = self._required_text(
            request_id,
            'request_id',
            128,
        )
        normalized_fingerprint = self._required_text(
            request_fingerprint,
            'request_fingerprint',
            128,
        )
        normalized_content = self._required_text(
            user_content,
            'user_content',
            MAX_UTTERANCE_LENGTH,
        )
        normalized_expected_instance = None
        if expected_session_instance_id is not None:
            normalized_expected_instance = self._required_text(
                expected_session_instance_id,
                'expected_session_instance_id',
                128,
            )
        now = self._now()
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                session = self._require_active(row)
                if (
                    normalized_expected_instance is not None
                    and session.session_instance_id
                    != normalized_expected_instance
                ):
                    raise ConversationChangedError(
                        'conversation session instance changed'
                    )
                existing = self._existing_request_locked(
                    normalized_user,
                    normalized_request,
                )
                if existing is not None:
                    result = self._reuse_existing_locked(
                        existing,
                        normalized_id,
                        normalized_turn,
                        normalized_fingerprint,
                        normalized_content,
                        session,
                    )
                    self._connection.commit()
                    return result
                turn_row = self._connection.execute(
                    '''
                    SELECT *
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND turn_id = ?
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                        normalized_turn,
                    ),
                ).fetchone()
                if turn_row is not None:
                    raise ConversationConflictError(
                        'turn_id was already used in this conversation'
                    )
                pending = self._connection.execute(
                    '''
                    SELECT turn_id
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND status = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                    ),
                ).fetchone()
                if pending is not None:
                    raise ConversationConflictError(
                        'another turn is already in progress'
                    )
                count_row = self._connection.execute(
                    '''
                    SELECT COUNT(*) AS turn_count
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND status = 'completed'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        session.generation,
                    ),
                ).fetchone()
                turn_count = int(count_row['turn_count'])
                if turn_count >= self.max_turns_per_session:
                    raise ConversationStateError(
                        'conversation turn limit reached; '
                        'reset or create a new session'
                    )
                summary = self._summary_for_window_locked(
                    session,
                    turn_count,
                    now,
                )
                history = self._history_locked(
                    normalized_user,
                    normalized_id,
                    session.session_instance_id,
                    session.generation,
                    self.history_limit,
                )
                trusted_results = list_trusted_results_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    limit=self.history_limit,
                )
                ordinal = turn_count + 1
                self._connection.execute(
                    '''
                    INSERT INTO conversation_turns (
                        user_id,
                        conversation_id,
                        session_instance_id,
                        turn_id,
                        request_id,
                        request_fingerprint,
                        generation,
                        ordinal,
                        status,
                        user_content,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                        normalized_turn,
                        normalized_request,
                        normalized_fingerprint,
                        session.generation,
                        ordinal,
                        normalized_content,
                        now,
                    ),
                )
                token = BeginTurnToken(
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=(
                        session.session_instance_id
                    ),
                    turn_id=normalized_turn,
                    request_id=normalized_request,
                    request_fingerprint=normalized_fingerprint,
                    generation=session.generation,
                    revision=session.revision,
                    ordinal=ordinal,
                )
                self._connection.commit()
                return BeginTurnResult(
                    session=session,
                    history=tuple(history),
                    summary=summary,
                    token=token,
                    cached_response=None,
                    trusted_results=trusted_results,
                )
            except Exception:
                self._connection.rollback()
                raise

    def complete_turn(
        self,
        token: BeginTurnToken,
        assistant_content: str,
        response: Dict[str, Any],
        confirmation_intent: Optional[ConfirmationIntentDraft] = None,
    ) -> Tuple[ConversationSession, ConversationTurn]:
        """Commit one response and optional confirmation in one transaction."""
        normalized_assistant = self._assistant_text(
            assistant_content
        )
        response_json = self._response_json(response)
        normalized_intent = self._normalize_confirmation_draft(
            confirmation_intent
        )
        now = self._now()
        changed_error: Optional[ConversationChangedError] = None
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    token.user_id,
                    token.conversation_id,
                )
                if row is None:
                    changed_error = ConversationChangedError(
                        'conversation was deleted during model inference'
                    )
                else:
                    session = self._session_from_row(row)
                    if (
                        session.status != 'active'
                        or (
                            session.session_instance_id
                            != token.session_instance_id
                        )
                        or session.generation != token.generation
                        or session.revision != token.revision
                    ):
                        changed_error = ConversationChangedError(
                            'conversation changed during model inference; '
                            'retry with a new request_id and turn_id'
                        )
                if changed_error is None:
                    cursor = self._connection.execute(
                        '''
                        UPDATE conversation_turns
                        SET status = 'completed',
                            assistant_content = ?,
                            response_json = ?,
                            completed_at = ?
                        WHERE user_id = ?
                          AND conversation_id = ?
                          AND session_instance_id = ?
                          AND turn_id = ?
                          AND request_id = ?
                          AND request_fingerprint = ?
                          AND generation = ?
                          AND ordinal = ?
                          AND status = 'pending'
                        ''',
                        (
                            normalized_assistant,
                            response_json,
                            now,
                            token.user_id,
                            token.conversation_id,
                            token.session_instance_id,
                            token.turn_id,
                            token.request_id,
                            token.request_fingerprint,
                            token.generation,
                            token.ordinal,
                        ),
                    )
                    if cursor.rowcount != 1:
                        changed_error = ConversationChangedError(
                            'pending conversation turn no longer exists'
                        )
                if changed_error is not None:
                    self._delete_pending_locked(token)
                    self._connection.commit()
                    raise changed_error
                self._advance_summary_locked(token, now)
                self._invalidate_pending_confirmations_locked(
                    token.user_id,
                    token.conversation_id,
                    'confirmation_conversation_changed',
                    now,
                )
                self._connection.execute(
                    '''
                    UPDATE conversation_sessions
                    SET revision = revision + 1,
                        updated_at = ?,
                        expires_at = ?
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                    ''',
                    (
                        now,
                        now + self.ttl_seconds,
                        token.user_id,
                        token.conversation_id,
                        token.session_instance_id,
                    ),
                )
                if normalized_intent is not None:
                    self._register_confirmation_intent_locked(
                        normalized_intent,
                        token,
                        response,
                        now,
                    )
                session_row = self._select_session_locked(
                    token.user_id,
                    token.conversation_id,
                )
                turn_row = self._select_turn_locked(token)
                self._connection.commit()
                return (
                    self._session_from_row(session_row),
                    self._turn_from_row(turn_row),
                )
            except Exception:
                self._connection.rollback()
                raise

    def fail_turn(self, token: BeginTurnToken) -> None:
        """Discard one pending turn after provider or safety failure."""
        with self._lock:
            self._delete_pending_locked(token)
            self._connection.commit()

    def register_confirmation_intent(
        self,
        draft: ConfirmationIntentDraft,
    ) -> DurableConfirmationIntent:
        """Idempotently attach a draft to an already completed turn."""
        normalized = self._normalize_confirmation_draft(draft)
        if normalized is None:
            raise TypeError('confirmation intent draft is required')
        now = self._now()
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(now)
                session_row = self._select_session_locked(
                    normalized.user_id,
                    normalized.conversation_id,
                )
                session = self._require_active(session_row)
                if (
                    session.session_instance_id
                    != normalized.session_instance_id
                    or session.generation != normalized.generation
                    or session.revision != normalized.revision
                ):
                    raise ConversationChangedError(
                        'confirmation conversation changed'
                    )
                turn_row = self._connection.execute(
                    '''
                    SELECT request_id, ordinal, status, response_json
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND turn_id = ?
                    ''',
                    (
                        normalized.user_id,
                        normalized.conversation_id,
                        normalized.session_instance_id,
                        normalized.generation,
                        normalized.turn_id,
                    ),
                ).fetchone()
                if (
                    turn_row is None
                    or turn_row['status'] != 'completed'
                    or turn_row['request_id']
                    != normalized.agent_request_id
                    or int(turn_row['ordinal']) != normalized.ordinal
                ):
                    raise ConversationChangedError(
                        'confirmation source turn changed'
                    )
                self._validate_confirmation_draft_response(
                    normalized,
                    self._load_response(turn_row['response_json']),
                )
                record = self._insert_confirmation_intent_locked(
                    normalized,
                    now,
                )
                if record.state != 'pending':
                    raise ConfirmationIntentAlreadyTerminalError(
                        'confirmation intent is already terminal'
                    )
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def get_confirmation_intent(
        self,
        user_id: str,
        confirmation_request_id: str,
    ) -> DurableConfirmationIntent:
        """Return one owner-scoped durable non-authorizing intent."""
        normalized_user = validate_user_id(user_id)
        normalized_request = self._required_text(
            confirmation_request_id,
            'confirmation_request_id',
            128,
        )
        with self._lock:
            row = self._connection.execute(
                '''
                SELECT * FROM confirmation_intents
                WHERE user_id = ? AND confirmation_request_id = ?
                ''',
                (normalized_user, normalized_request),
            ).fetchone()
            if row is None:
                raise ConfirmationIntentNotFoundError(
                    'confirmation intent was not found'
                )
            return self._confirmation_intent_from_row(row)

    def refresh_confirmation_intent(
        self,
        user_id: str,
        confirmation_request_id: str,
    ) -> DurableConfirmationIntent:
        """Return one intent after durable session/deadline housekeeping."""
        normalized_user = validate_user_id(user_id)
        normalized_request = self._required_text(
            confirmation_request_id,
            'confirmation_request_id',
            128,
        )
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._connection.execute(
                    '''
                    SELECT * FROM confirmation_intents
                    WHERE user_id = ? AND confirmation_request_id = ?
                    ''',
                    (normalized_user, normalized_request),
                ).fetchone()
                if row is None:
                    raise ConfirmationIntentNotFoundError(
                        'confirmation intent was not found'
                    )
                record = self._confirmation_intent_from_row(row)
                self._connection.commit()
                return record
            except Exception:
                self._connection.rollback()
                raise

    def expire_due_confirmation_intents(
        self,
        limit: int = 100,
    ) -> Tuple[DurableConfirmationIntent, ...]:
        """Terminalize due intents without restoring a speech session."""
        normalized_limit = self._bounded_integer(
            limit,
            'confirmation expiry limit',
            1,
            1000,
        )
        with self._lock:
            self._begin()
            try:
                now = self._now()
                due_ids = tuple(
                    str(row['confirmation_request_id'])
                    for row in self._connection.execute(
                        '''
                        SELECT confirmation_request_id
                        FROM confirmation_intents
                        WHERE state = 'pending' AND expires_at <= ?
                        ORDER BY expires_at, created_at,
                                 confirmation_request_id
                        LIMIT ?
                        ''',
                        (now, normalized_limit),
                    ).fetchall()
                )
                self._expire_due_locked(now)
                results = []
                for confirmation_request_id in due_ids:
                    row = self._connection.execute(
                        '''
                        SELECT * FROM confirmation_intents
                        WHERE confirmation_request_id = ?
                        ''',
                        (confirmation_request_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    if row['state'] != 'pending':
                        results.append(
                            self._confirmation_intent_from_row(row)
                        )
                        continue
                    context_code = (
                        self._confirmation_context_code_locked(row)
                    )
                    if context_code is not None:
                        self._terminalize_system_invalidation_locked(
                            row,
                            context_code,
                            now,
                        )
                    else:
                        response_id, fingerprint, provenance = (
                            self.confirmation_expiry_envelope(
                                confirmation_request_id,
                                str(row['proposal_fingerprint']),
                            )
                        )
                        owner = self._connection.execute(
                            '''
                            SELECT confirmation_request_id
                            FROM confirmation_intents
                            WHERE user_id = ?
                              AND response_id = ?
                              AND confirmation_request_id != ?
                            ''',
                            (
                                row['user_id'],
                                response_id,
                                confirmation_request_id,
                            ),
                        ).fetchone()
                        if owner is not None:
                            self._terminalize_system_invalidation_locked(
                                row,
                                (
                                    'confirmation_expiry_'
                                    'response_id_conflict'
                                ),
                                now,
                            )
                            terminal = self._connection.execute(
                                '''
                                SELECT * FROM confirmation_intents
                                WHERE confirmation_request_id = ?
                                ''',
                                (confirmation_request_id,),
                            ).fetchone()
                            results.append(
                                self._confirmation_intent_from_row(
                                    terminal
                                )
                            )
                            continue
                        result_id = self._confirmation_result_id(
                            confirmation_request_id,
                            'server_expiry',
                            response_id,
                        )
                        cursor = self._connection.execute(
                            '''
                            UPDATE confirmation_intents
                            SET state = 'resolved',
                                disposition = 'expired',
                                requested_disposition = 'cancel',
                                result_code = 'confirmation_expired',
                                confirmation_result_id = ?,
                                response_id = ?,
                                response_fingerprint = ?,
                                response_channel = 'server_expiry',
                                assurance_level = 'server_clock',
                                provenance_ref = ?,
                                verifier_ref = NULL,
                                resolved_at = ?,
                                updated_at = ?
                            WHERE confirmation_request_id = ?
                              AND state = 'pending'
                            ''',
                            (
                                result_id,
                                response_id,
                                fingerprint,
                                provenance,
                                now,
                                now,
                                confirmation_request_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ConfirmationIntentConflictError(
                                'confirmation intent is already terminal'
                            )
                    terminal = self._connection.execute(
                        '''
                        SELECT * FROM confirmation_intents
                        WHERE confirmation_request_id = ?
                        ''',
                        (confirmation_request_id,),
                    ).fetchone()
                    results.append(
                        self._confirmation_intent_from_row(terminal)
                    )
                self._connection.commit()
                return tuple(results)
            except Exception:
                self._connection.rollback()
                raise

    @classmethod
    def confirmation_expiry_envelope(
        cls,
        confirmation_request_id: str,
        proposal_fingerprint: str,
    ) -> Tuple[str, str, str]:
        """Return deterministic server-expiry identity and provenance."""
        normalized_request = cls._required_text(
            confirmation_request_id,
            'confirmation_request_id',
            128,
        )
        normalized_proposal = cls._confirmation_digest(
            proposal_fingerprint,
            'proposal_fingerprint',
        )
        response_digest = hashlib.sha256(
            (
                'confirmation-expiry-v1\0'
                f'{normalized_request}'
            ).encode('utf-8')
        ).hexdigest()[:40]
        response_id = (
            f'{CONFIRMATION_SERVER_RESPONSE_ID_PREFIX}{response_digest}'
        )
        fingerprint = hashlib.sha256(
            (
                'confirmation-expiry-fingerprint-v1\0'
                f'{normalized_request}\0{normalized_proposal}'
            ).encode('utf-8')
        ).hexdigest()
        provenance = hashlib.sha256(
            (
                'confirmation-provenance-v1\0'
                f'server_expiry\0{fingerprint}'
            ).encode('utf-8')
        ).hexdigest()
        return response_id, fingerprint, provenance

    def resolve_confirmation_intent(
        self,
        *,
        user_id: str,
        confirmation_request_id: str,
        proposal_fingerprint: str,
        response_id: str,
        response_fingerprint: str,
        requested_disposition: str,
        response_channel: str,
        assurance_level: str,
        provenance_ref: str,
        verifier_ref: Optional[str] = None,
    ) -> DurableConfirmationIntent:
        """Atomically record one terminal intent against current context."""
        normalized_user = validate_user_id(user_id)
        normalized_request = self._required_text(
            confirmation_request_id,
            'confirmation_request_id',
            128,
        )
        normalized_proposal = self._confirmation_digest(
            proposal_fingerprint,
            'proposal_fingerprint',
        )
        normalized_response = self._required_text(
            response_id,
            'response_id',
            128,
        )
        normalized_response_fingerprint = self._confirmation_digest(
            response_fingerprint,
            'response_fingerprint',
        )
        if requested_disposition not in {
            'approve', 'deny', 'cancel'
        }:
            raise ValidationError(
                'confirmation disposition is unsupported'
            )
        channel_assurance = {
            'voice': 'local_speech_binding',
            'ui_in_process': 'unverified_in_process_ui',
            'server_expiry': 'server_clock',
        }
        if response_channel not in channel_assurance:
            raise ValidationError(
                'confirmation response channel is unsupported'
            )
        if assurance_level != channel_assurance[response_channel]:
            raise ValidationError(
                'confirmation assurance does not match channel'
            )
        if (
            response_channel == 'server_expiry'
            and requested_disposition != 'cancel'
        ):
            raise ValidationError(
                'server expiry disposition is unsupported'
            )
        normalized_provenance = self._confirmation_digest(
            provenance_ref,
            'provenance_ref',
        )
        normalized_verifier = None
        if verifier_ref is not None:
            normalized_verifier = self._required_text(
                verifier_ref,
                'verifier_ref',
                128,
            )
        if normalized_verifier is not None:
            raise ValidationError(
                'confirmation verifier is not available'
            )
        with self._lock:
            self._begin()
            try:
                now = self._now()
                self._expire_due_locked(now)
                row = self._connection.execute(
                    '''
                    SELECT * FROM confirmation_intents
                    WHERE confirmation_request_id = ?
                    ''',
                    (normalized_request,),
                ).fetchone()
                if (
                    row is None
                    or row['user_id'] != normalized_user
                    or row['proposal_fingerprint']
                    != normalized_proposal
                ):
                    raise ConfirmationIntentNotFoundError(
                        'confirmation intent was not found'
                    )
                if row['state'] != 'pending':
                    if (
                        row['state'] == 'invalidated'
                        and row['response_id'] is None
                    ):
                        self._connection.commit()
                        return self._confirmation_intent_from_row(row)
                    if (
                        row['response_id'] == normalized_response
                        and row['response_fingerprint']
                        == normalized_response_fingerprint
                        and row['response_channel'] == response_channel
                        and row['assurance_level'] == assurance_level
                        and row['requested_disposition']
                        == requested_disposition
                        and row['provenance_ref']
                        == normalized_provenance
                        and row['verifier_ref']
                        == normalized_verifier
                    ):
                        self._connection.commit()
                        return self._confirmation_intent_from_row(row)
                    if row['response_id'] == normalized_response:
                        raise ConfirmationIntentConflictError(
                            'confirmation response id conflict'
                        )
                    raise ConfirmationIntentAlreadyTerminalError(
                        'confirmation intent is already terminal'
                    )
                if now < float(row['issued_at']):
                    raise ConversationClockError(
                        'server clock is before confirmation issue'
                    )
                if (
                    response_channel != 'server_expiry'
                    and normalized_response.startswith(
                        CONFIRMATION_SERVER_RESPONSE_ID_PREFIX
                    )
                ):
                    raise ConfirmationReservedResponseIdError(
                        'confirmation response id is server-reserved'
                    )
                if response_channel == 'server_expiry':
                    expected_response = self.confirmation_expiry_envelope(
                        normalized_request,
                        normalized_proposal,
                    )
                    if expected_response != (
                        normalized_response,
                        normalized_response_fingerprint,
                        normalized_provenance,
                    ):
                        raise ValidationError(
                            'server expiry envelope is invalid'
                        )
                if (
                    response_channel == 'server_expiry'
                    and now < float(row['expires_at'])
                ):
                    raise ValidationError(
                        'confirmation deadline has not elapsed'
                    )
                other = self._connection.execute(
                    '''
                    SELECT confirmation_request_id
                    FROM confirmation_intents
                    WHERE user_id = ? AND response_id = ?
                    ''',
                    (normalized_user, normalized_response),
                ).fetchone()
                if (
                    other is not None
                    and other['confirmation_request_id']
                    != normalized_request
                ):
                    raise ConfirmationIntentConflictError(
                        'confirmation response id conflict'
                    )
                context_code = self._confirmation_context_code_locked(row)
                fresh_now = self._now()
                if fresh_now < now or fresh_now < float(row['issued_at']):
                    raise ConversationClockError(
                        'server clock moved backwards'
                    )
                if fresh_now != now:
                    now = fresh_now
                    self._expire_due_locked(now)
                    row = self._connection.execute(
                        '''
                        SELECT * FROM confirmation_intents
                        WHERE confirmation_request_id = ?
                        ''',
                        (normalized_request,),
                    ).fetchone()
                    if row is None:
                        raise ConfirmationIntentNotFoundError(
                            'confirmation intent was not found'
                        )
                    if row['state'] != 'pending':
                        self._connection.commit()
                        return self._confirmation_intent_from_row(row)
                    context_code = (
                        self._confirmation_context_code_locked(row)
                    )
                if context_code is not None:
                    self._terminalize_confirmation_invalidation_locked(
                        row,
                        context_code,
                        normalized_response,
                        normalized_response_fingerprint,
                        requested_disposition,
                        response_channel,
                        assurance_level,
                        normalized_provenance,
                        normalized_verifier,
                        now,
                    )
                else:
                    disposition = (
                        'expired'
                        if now >= float(row['expires_at'])
                        else requested_disposition
                    )
                    code = {
                        'approve': (
                            'confirmation_approval_recorded_no_execution'
                        ),
                        'deny': 'confirmation_denial_recorded',
                        'cancel': 'confirmation_cancelled',
                        'expired': 'confirmation_expired',
                    }[disposition]
                    result_id = self._confirmation_result_id(
                        normalized_request,
                        response_channel,
                        normalized_response,
                    )
                    cursor = self._connection.execute(
                        '''
                        UPDATE confirmation_intents
                        SET state = 'resolved',
                            disposition = ?,
                            requested_disposition = ?,
                            result_code = ?,
                            confirmation_result_id = ?,
                            response_id = ?,
                            response_fingerprint = ?,
                            response_channel = ?,
                            assurance_level = ?,
                            provenance_ref = ?,
                            verifier_ref = ?,
                            resolved_at = ?,
                            updated_at = ?
                        WHERE confirmation_request_id = ?
                          AND state = 'pending'
                        ''',
                        (
                            disposition,
                            requested_disposition,
                            code,
                            result_id,
                            normalized_response,
                            normalized_response_fingerprint,
                            response_channel,
                            assurance_level,
                            normalized_provenance,
                            normalized_verifier,
                            now,
                            now,
                            normalized_request,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConfirmationIntentConflictError(
                            'confirmation intent is already terminal'
                        )
                terminal_row = self._connection.execute(
                    '''
                    SELECT * FROM confirmation_intents
                    WHERE confirmation_request_id = ?
                    ''',
                    (normalized_request,),
                ).fetchone()
                self._connection.commit()
                return self._confirmation_intent_from_row(terminal_row)
            except Exception:
                self._connection.rollback()
                raise

    def consume_approved_monitor_room_simulation(
        self,
        *,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
    ) -> DurableSimulationExecution:
        """Verify and spend one approval on a terminal pure simulation."""
        if not isinstance(approval, VerifiedSimulationApproval):
            raise TypeError(
                'approval must be a VerifiedSimulationApproval'
            )
        if not isinstance(request, SimulationConsumeRequest):
            raise TypeError('request must be a SimulationConsumeRequest')
        with self._lock:
            self._begin()
            try:
                now = self._now()

                def classify_context(
                    row: sqlite3.Row,
                    observed_at: float,
                ) -> Optional[str]:
                    self._expire_due_locked(observed_at)
                    return self._confirmation_context_code_locked(row)

                result = (
                    _consume_approved_monitor_room_simulation_locked(
                        self._connection,
                        approval=approval,
                        request=request,
                        verifier=self._simulation_execution_verifier,
                        now=now,
                        fresh_clock=self._now,
                        context_classifier=classify_context,
                    )
                )
                trusted_result = record_or_verify_trusted_result_locked(
                    self._connection,
                    confirmation_request_id=(
                        result.confirmation_request_id
                    ),
                    replayed=result.replayed,
                )
                record_or_verify_trusted_result_tts_locked(
                    self._connection,
                    trusted_result=trusted_result,
                    replayed=result.replayed,
                )
                self._connection.commit()
                return result
            except Exception:
                self._connection.rollback()
                raise

    def consume_approved_monitor_room_gazebo_simulation(
        self,
        *,
        approval: VerifiedSimulationApproval,
        request: SimulationConsumeRequest,
    ) -> GazeboSimulationConsumeResult:
        """Consume a fresh approval and atomically enqueue Gazebo."""
        if not isinstance(approval, VerifiedSimulationApproval):
            raise TypeError(
                'approval must be a VerifiedSimulationApproval'
            )
        if not isinstance(request, SimulationConsumeRequest):
            raise TypeError('request must be a SimulationConsumeRequest')
        policy = self._gazebo_execution_policy
        if type(policy) is not GazeboSimulationExecutionPolicy:
            raise ValidationError(
                'Gazebo simulation execution policy is not configured'
            )
        with self._lock:
            storage_failed = False
            self._begin()
            try:
                now = self._now()
                fresh_enqueue = None

                def classify_context(
                    row: sqlite3.Row,
                    observed_at: float,
                ) -> Optional[str]:
                    self._expire_due_locked(observed_at)
                    return self._confirmation_context_code_locked(row)

                def record_fresh_plan(
                    receipt: DurableSimulationExecution,
                    plan: Any,
                    target: TargetBinding,
                ) -> None:
                    nonlocal fresh_enqueue
                    final_wall = self._now()
                    if (
                        final_wall < receipt.completed_at
                        or final_wall >= approval.expires_at
                    ):
                        raise ValidationError(
                            'Gazebo approval expired before durable enqueue'
                        )
                    context = policy.verify_for_enqueue(
                        target,
                        wall_now=final_wall,
                    )
                    # A second wall read closes semantic TTL expiration while
                    # policy evidence and the exact plan were being checked.
                    durable_wall = self._now()
                    if durable_wall < final_wall:
                        raise ValidationError(
                            'conversation clock moved backwards'
                        )
                    fresh_enqueue = record_gazebo_execution_outbox_locked(
                        self._connection,
                        receipt=receipt,
                        plan=plan,
                        target=target,
                        policy=policy,
                        context=context,
                        created_wall=durable_wall,
                    )

                result = _consume_approved_monitor_room_simulation_locked(
                    self._connection,
                    approval=approval,
                    request=request,
                    verifier=self._simulation_execution_verifier,
                    now=now,
                    fresh_clock=self._now,
                    context_classifier=classify_context,
                    fresh_plan_validator=lambda plan: (
                        plan.sample_count
                        <= GAZEBO_EXECUTION_OUTBOX_MAX_SAMPLES
                    ),
                    fresh_planned_hook=record_fresh_plan,
                )
                enqueue = fresh_enqueue
                if result.record_kind == 'planned' and enqueue is None:
                    enqueue = (
                        get_gazebo_execution_enqueue_for_receipt_locked(
                            self._connection,
                            receipt=result,
                        )
                    )
                trusted_result = record_or_verify_trusted_result_locked(
                    self._connection,
                    confirmation_request_id=(
                        result.confirmation_request_id
                    ),
                    replayed=result.replayed,
                )
                record_or_verify_trusted_result_tts_locked(
                    self._connection,
                    trusted_result=trusted_result,
                    replayed=result.replayed,
                )
                response = GazeboSimulationConsumeResult(
                    receipt=result,
                    enqueue=enqueue,
                )
                self._connection.commit()
                return response
            except sqlite3.Error:
                self._connection.rollback()
                storage_failed = True
            except Exception:
                self._connection.rollback()
                raise
            if storage_failed:
                raise ValidationError(
                    'Gazebo durable enqueue storage failed'
                )

    def claim_gazebo_execution(
        self,
        claim_request_id: str,
        lease_seconds: int = 30,
        *,
        expected_outbox_id: Optional[str] = None,
        expected_operation_id: Optional[str] = None,
        expected_confirmation_request_id: Optional[str] = None,
    ) -> Optional[GazeboExecutionClaim]:
        """Commit one targeted or oldest lease before returning payload."""
        policy = self._gazebo_execution_policy
        if type(policy) is not GazeboSimulationExecutionPolicy:
            raise ValidationError(
                'Gazebo simulation execution policy is not configured'
            )
        with self._lock:
            storage_failed = False
            self._begin()
            try:
                claim = claim_gazebo_execution_locked(
                    self._connection,
                    policy=policy,
                    claim_request_id=claim_request_id,
                    lease_seconds=lease_seconds,
                    expected_outbox_id=expected_outbox_id,
                    expected_operation_id=expected_operation_id,
                    expected_confirmation_request_id=(
                        expected_confirmation_request_id
                    ),
                )
                self._connection.commit()
                return claim
            except sqlite3.Error:
                self._connection.rollback()
                storage_failed = True
            except Exception:
                self._connection.rollback()
                raise
            if storage_failed:
                raise ValidationError(
                    'Gazebo durable claim storage failed'
                )

    def acknowledge_gazebo_execution(
        self,
        *,
        outbox_id: str,
        claim_token: str,
        claim_fence: int,
        prepare_fingerprint: str,
    ) -> GazeboExecutionAcknowledgement:
        """Commit one exact, fenced prepare ACK before returning it."""
        policy = self._gazebo_execution_policy
        if type(policy) is not GazeboSimulationExecutionPolicy:
            raise ValidationError(
                'Gazebo simulation execution policy is not configured'
            )
        with self._lock:
            storage_failed = False
            self._begin()
            try:
                acknowledgement = acknowledge_gazebo_execution_locked(
                    self._connection,
                    policy=policy,
                    outbox_id=outbox_id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                    prepare_fingerprint=prepare_fingerprint,
                )
                self._connection.commit()
                return acknowledgement
            except sqlite3.Error:
                self._connection.rollback()
                storage_failed = True
            except Exception:
                self._connection.rollback()
                raise
            if storage_failed:
                raise ValidationError(
                    'Gazebo durable acknowledgement storage failed'
                )

    def resolve_prepared_gazebo_execution(
        self,
        *,
        confirmation_request_id: str,
        expected_user_id: str,
        execution_scope: str = 'observe',
    ) -> GazeboPreparedExecutionAuthority:
        """
        Rederive one owner-bound prepared selector from its durable ACK.

        The confirmation identifier is only a server-side lookup selector and
        ``expected_user_id`` comes from fixed authenticated server state.
        The default scope is read-only; command runners select a closed scope
        explicitly for each flow.
        Operation, outbox, fence, prepare, and acknowledgement identities are
        recovered from and cross-checked against the durable store.
        """
        policy = self._gazebo_execution_policy
        if type(policy) is not GazeboSimulationExecutionPolicy:
            raise ValidationError(
                'Gazebo simulation execution policy is not configured'
            )
        with self._lock:
            storage_failed = False
            SQLiteConversationStore.attest_command_boundary_durability(
                self
            )
            SQLiteConversationStore._begin(self)
            try:
                authority = resolve_prepared_gazebo_execution_locked(
                    self._connection,
                    policy=policy,
                    confirmation_request_id=confirmation_request_id,
                    expected_user_id=expected_user_id,
                    execution_scope=execution_scope,
                )
                self._connection.commit()
                return authority
            except sqlite3.Error:
                self._connection.rollback()
                storage_failed = True
            except Exception:
                self._connection.rollback()
                raise
            if storage_failed:
                raise ValidationError(
                    'Gazebo prepared execution storage failed'
                )

    def list_trusted_tool_results(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> Tuple[TrustedToolResult, ...]:
        """Return owner-scoped results for the current session generation."""
        normalized_limit = self._bounded_integer(
            limit,
            'trusted result limit',
            1,
            500,
        )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'conversation was not found'
                    )
                session = self._session_from_row(row)
                results = list_trusted_results_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    limit=normalized_limit,
                )
                self._connection.commit()
                return results
            except Exception:
                self._connection.rollback()
                raise

    def claim_trusted_result_tts(
        self,
        user_id: str,
        conversation_id: str,
        speech_session_id: str,
        claim_request_id: str,
        lease_seconds: int = 30,
    ) -> Optional[TrustedResultTTSClaim]:
        """Lease one ordered feedback event without playing audio."""
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        normalized_speech = self._required_text(
            speech_session_id, 'speech_session_id', 128
        )
        normalized_request = self._required_text(
            claim_request_id, 'claim_request_id', 128
        )
        normalized_lease = self._bounded_integer(
            lease_seconds, 'lease_seconds', 1, 300
        )
        with self._lock:
            self._begin()
            try:
                now = self._now()
                self._expire_due_locked(now)
                claim = claim_trusted_result_tts_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_conversation,
                    speech_session_id=normalized_speech,
                    claim_request_id=normalized_request,
                    lease_seconds=normalized_lease,
                    now=now,
                )
                self._connection.commit()
                return claim
            except Exception:
                self._connection.rollback()
                raise

    def acknowledge_trusted_result_tts(
        self,
        user_id: str,
        conversation_id: str,
        speech_session_id: str,
        *,
        event_id: str,
        claim_token: str,
        claim_fence: int,
    ) -> TrustedResultTTSEvent:
        """Persist a trusted adapter terminal ACK, not audible proof."""
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        normalized_speech = self._required_text(
            speech_session_id, 'speech_session_id', 128
        )
        normalized_event = self._required_text(
            event_id, 'event_id', 128
        )
        normalized_token = self._required_text(
            claim_token, 'claim_token', 128
        )
        normalized_fence = self._bounded_integer(
            claim_fence, 'claim_fence', 1, 5
        )
        with self._lock:
            self._begin()
            try:
                now = self._now()
                self._expire_due_locked(now)
                event = acknowledge_trusted_result_tts_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_conversation,
                    speech_session_id=normalized_speech,
                    event_id=normalized_event,
                    claim_token=normalized_token,
                    claim_fence=normalized_fence,
                    now=now,
                )
                self._connection.commit()
                return event
            except Exception:
                self._connection.rollback()
                raise

    def list_turns(
        self,
        user_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> List[ConversationTurn]:
        """Return completed turns in chronological order."""
        normalized_limit = self._bounded_integer(
            limit,
            'turn limit',
            1,
            500,
        )
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._expire_and_commit(self._now())
            row = self._select_session_locked(
                normalized_user,
                normalized_id,
            )
            if row is None:
                raise ConversationNotFoundError(
                    'conversation was not found'
                )
            session = self._session_from_row(row)
            return self._history_locked(
                normalized_user,
                normalized_id,
                session.session_instance_id,
                session.generation,
                normalized_limit,
            )

    def get_completed_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn_id: str,
    ) -> ConversationTurn:
        """Return one active-generation completed turn by exact identity."""
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(
            conversation_id
        )
        normalized_turn = validate_turn_id(turn_id)
        with self._lock:
            self._begin()
            try:
                self._expire_due_locked(self._now())
                row = self._select_session_locked(
                    normalized_user,
                    normalized_conversation,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'completed conversation turn was not found'
                    )
                session = self._require_active(row)
                turn_row = self._connection.execute(
                    '''
                    SELECT *
                    FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND generation = ?
                      AND turn_id = ?
                      AND status = 'completed'
                    ''',
                    (
                        normalized_user,
                        normalized_conversation,
                        session.session_instance_id,
                        session.generation,
                        normalized_turn,
                    ),
                ).fetchone()
                if turn_row is None:
                    raise ConversationNotFoundError(
                        'completed conversation turn was not found'
                    )
                self._connection.commit()
                return self._turn_from_row(turn_row)
            except Exception:
                self._connection.rollback()
                raise

    def reset(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Delete all turns and start a new active generation."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._begin()
            try:
                now = self._now()
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                session = self._require_active(row)
                self._invalidate_pending_confirmations_locked(
                    normalized_user,
                    normalized_id,
                    'confirmation_conversation_changed',
                    now,
                )
                cancel_trusted_result_tts_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    cancellation_code='conversation_reset',
                    now=now,
                )
                self._connection.execute(
                    '''
                    DELETE FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND status = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                    ),
                )
                self._connection.execute(
                    '''
                    DELETE FROM conversation_summaries
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (normalized_user, normalized_id),
                )
                self._connection.execute(
                    '''
                    UPDATE conversation_sessions
                    SET generation = generation + 1,
                        revision = revision + 1,
                        updated_at = ?,
                        expires_at = ?
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (
                        now,
                        now + self.ttl_seconds,
                        normalized_user,
                        normalized_id,
                    ),
                )
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                self._connection.commit()
                return self._session_from_row(row)
            except Exception:
                self._connection.rollback()
                raise

    def close_session(
        self,
        user_id: str,
        conversation_id: str,
    ) -> ConversationSession:
        """Close an active session without deleting committed history."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._begin()
            try:
                now = self._now()
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'conversation was not found'
                    )
                session = self._session_from_row(row)
                if session.status == 'expired':
                    raise ConversationStateError(
                        'conversation has expired'
                    )
                self._invalidate_pending_confirmations_locked(
                    normalized_user,
                    normalized_id,
                    'confirmation_conversation_inactive',
                    now,
                )
                cancel_trusted_result_tts_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    cancellation_code='conversation_inactive',
                    now=now,
                )
                self._connection.execute(
                    '''
                    DELETE FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND status = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                    ),
                )
                if session.status == 'active':
                    self._connection.execute(
                        '''
                        UPDATE conversation_sessions
                        SET status = 'closed',
                            revision = revision + 1,
                            updated_at = ?
                        WHERE user_id = ? AND conversation_id = ?
                        ''',
                        (now, normalized_user, normalized_id),
                    )
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                self._connection.commit()
                return self._session_from_row(row)
            except Exception:
                self._connection.rollback()
                raise

    def close_session_if_current(
        self,
        user_id: str,
        conversation_id: str,
        *,
        expected_session_instance_id: str,
        expected_generation: int,
    ) -> ConversationSession:
        """Close only the exact session generation checked in this write."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        normalized_instance = self._required_text(
            expected_session_instance_id,
            'expected_session_instance_id',
            128,
        )
        normalized_generation = self._bounded_integer(
            expected_generation,
            'expected_generation',
            1,
            9223372036854775807,
        )
        with self._lock:
            self._begin()
            try:
                now = self._now()
                self._expire_due_locked(now)
                row = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                if row is None:
                    raise ConversationNotFoundError(
                        'conversation was not found'
                    )
                session = self._session_from_row(row)
                if (
                    session.session_instance_id != normalized_instance
                    or session.generation != normalized_generation
                ):
                    raise ConversationChangedError(
                        'conversation generation changed before close'
                    )
                if session.status == 'expired':
                    raise ConversationStateError(
                        'conversation has expired'
                    )
                self._invalidate_pending_confirmations_locked(
                    normalized_user,
                    normalized_id,
                    'confirmation_conversation_inactive',
                    now,
                )
                cancel_trusted_result_tts_locked(
                    self._connection,
                    user_id=normalized_user,
                    conversation_id=normalized_id,
                    session_instance_id=session.session_instance_id,
                    generation=session.generation,
                    cancellation_code='conversation_inactive',
                    now=now,
                )
                self._connection.execute(
                    '''
                    DELETE FROM conversation_turns
                    WHERE user_id = ?
                      AND conversation_id = ?
                      AND session_instance_id = ?
                      AND status = 'pending'
                    ''',
                    (
                        normalized_user,
                        normalized_id,
                        session.session_instance_id,
                    ),
                )
                if session.status == 'active':
                    cursor = self._connection.execute(
                        '''
                        UPDATE conversation_sessions
                        SET status = 'closed',
                            revision = revision + 1,
                            updated_at = ?
                        WHERE user_id = ? AND conversation_id = ?
                          AND session_instance_id = ?
                          AND generation = ?
                          AND status = 'active'
                        ''',
                        (
                            now,
                            normalized_user,
                            normalized_id,
                            normalized_instance,
                            normalized_generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConversationChangedError(
                            'conversation changed before close'
                        )
                closed = self._select_session_locked(
                    normalized_user,
                    normalized_id,
                )
                self._connection.commit()
                return self._session_from_row(closed)
            except Exception:
                self._connection.rollback()
                raise

    def delete(
        self,
        user_id: str,
        conversation_id: str,
    ) -> bool:
        """Delete one session and all of its turns."""
        normalized_user = validate_user_id(user_id)
        normalized_id = validate_conversation_id(conversation_id)
        with self._lock:
            self._begin()
            try:
                self._connection.execute(
                    '''
                    DELETE FROM confirmation_intents
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (normalized_user, normalized_id),
                )
                cursor = self._connection.execute(
                    '''
                    DELETE FROM conversation_sessions
                    WHERE user_id = ? AND conversation_id = ?
                    ''',
                    (normalized_user, normalized_id),
                )
                self._connection.commit()
                return cursor.rowcount == 1
            except Exception:
                self._connection.rollback()
                raise

    def purge_expired(self) -> int:
        """Mark every due active session as expired."""
        with self._lock:
            self._begin()
            try:
                count = self._expire_due_locked(self._now())
                self._connection.commit()
                return count
            except Exception:
                self._connection.rollback()
                raise

    def _normalize_confirmation_draft(
        self,
        draft: Optional[ConfirmationIntentDraft],
    ) -> Optional[ConfirmationIntentDraft]:
        """Validate the storage-only confirmation boundary."""
        if draft is None:
            return None
        if not isinstance(draft, ConfirmationIntentDraft):
            raise TypeError(
                'confirmation_intent must be a ConfirmationIntentDraft'
            )
        if draft.schema_version != CONFIRMATION_REQUEST_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation intent schema_version is unsupported'
            )
        integers = {}
        for name in ('generation', 'revision', 'ordinal'):
            value = getattr(draft, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > (1 << 63) - 1
            ):
                raise ValidationError(
                    f'confirmation intent {name} is invalid'
                )
            integers[name] = value
        issued_at = self._confirmation_timestamp(
            draft.issued_at,
            'issued_at',
        )
        expires_at = self._confirmation_timestamp(
            draft.expires_at,
            'expires_at',
        )
        if expires_at <= issued_at:
            raise ValidationError('confirmation intent is not current')
        if draft.risk_level != 'L3':
            raise ValidationError(
                'confirmation intent risk level is unsupported'
            )
        target = self._normalize_confirmation_target(draft)
        return ConfirmationIntentDraft(
            schema_version=CONFIRMATION_REQUEST_SCHEMA_VERSION,
            confirmation_request_id=self._required_text(
                draft.confirmation_request_id,
                'confirmation_request_id',
                128,
            ),
            agent_request_id=self._required_text(
                draft.agent_request_id,
                'agent_request_id',
                128,
            ),
            user_id=validate_user_id(draft.user_id),
            speech_session_id=self._required_text(
                draft.speech_session_id,
                'speech_session_id',
                128,
            ),
            source_utterance_id=self._required_text(
                draft.source_utterance_id,
                'source_utterance_id',
                128,
            ),
            conversation_id=validate_conversation_id(
                draft.conversation_id
            ),
            session_instance_id=self._required_text(
                draft.session_instance_id,
                'session_instance_id',
                128,
            ),
            generation=integers['generation'],
            revision=integers['revision'],
            ordinal=integers['ordinal'],
            turn_id=validate_turn_id(draft.turn_id),
            decision_id=self._required_text(
                draft.decision_id,
                'decision_id',
                128,
            ),
            tool_name=self._required_text(
                draft.tool_name,
                'tool_name',
                128,
            ),
            arguments_digest=self._confirmation_digest(
                draft.arguments_digest,
                'arguments_digest',
            ),
            proposal_fingerprint=self._confirmation_digest(
                draft.proposal_fingerprint,
                'proposal_fingerprint',
            ),
            issued_at=issued_at,
            expires_at=expires_at,
            risk_level='L3',
            confirmation_message=self._required_text(
                draft.confirmation_message,
                'confirmation_message',
                1000,
            ),
            target_binding_schema_version=target.schema_version,
            target_device_id=target.device_id,
            target_device_binding_revision=(
                target.device_binding_revision
            ),
            target_source_revision=target.source_revision,
            target_map_id=target.map_id,
            target_map_revision=target.map_revision,
            target_semantic_revision=target.semantic_revision,
            target_frame_id=target.frame_id,
            target_room_id=target.room_id,
            target_room_name=target.room_name,
            target_room_category=target.room_category,
            target_geometry_json=target.geometry_json,
            target_geometry_digest=target.geometry_digest,
            target_representative_x=target.representative_point[0],
            target_representative_y=target.representative_point[1],
            target_clearance_m=target.clearance_m,
            target_area_m2=target.area_m2,
            target_source_arguments_digest=(
                target.source_arguments_digest
            ),
            target_binding_digest=target.binding_digest,
            effects_schema_version=target.effects.schema_version,
            effect_physical_navigation=(
                target.effects.physical_navigation
            ),
            effect_camera_capture=target.effects.camera_capture,
            effect_external_video_stream=(
                target.effects.external_video_stream
            ),
            effect_video_recording=target.effects.video_recording,
            effect_audio_capture=target.effects.audio_capture,
            effect_coverage_mode=target.effects.coverage_mode,
            effect_viewer_scope=target.effects.viewer_scope,
            effect_talkback_allowed=target.effects.talkback_allowed,
            effect_max_duration_seconds=(
                target.effects.max_duration_seconds
            ),
            effects_digest=target.effects_digest,
        )

    def _normalize_confirmation_target(
        self,
        draft: ConfirmationIntentDraft,
    ) -> TargetBinding:
        """Reconstruct and verify the complete v3 target/effects binding."""
        if draft.target_binding_schema_version != 1:
            raise ValidationError(
                'confirmation target schema_version is unsupported'
            )
        if draft.effects_schema_version not in {1, 2}:
            raise ValidationError(
                'confirmation effects schema_version is unsupported'
            )
        geometry_json = self._required_text(
            draft.target_geometry_json,
            'target_geometry_json',
            2 * 1024 * 1024,
        )
        effects = Effects(
            schema_version=draft.effects_schema_version,
            physical_navigation=draft.effect_physical_navigation,
            camera_capture=draft.effect_camera_capture,
            external_video_stream=draft.effect_external_video_stream,
            video_recording=draft.effect_video_recording,
            audio_capture=draft.effect_audio_capture,
            max_duration_seconds=draft.effect_max_duration_seconds,
            coverage_mode=draft.effect_coverage_mode,
            viewer_scope=draft.effect_viewer_scope,
            talkback_allowed=draft.effect_talkback_allowed,
        )
        if (
            draft.effects_schema_version == 2
            and not effects.gazebo_simulation_navigation
        ):
            raise ValidationError(
                'Gazebo simulation confirmation effects are invalid'
            )
        target = TargetBinding(
            schema_version=draft.target_binding_schema_version,
            device_id=self._required_text(
                draft.target_device_id,
                'target_device_id',
                128,
            ),
            device_binding_revision=self._required_text(
                draft.target_device_binding_revision,
                'target_device_binding_revision',
                128,
            ),
            source_revision=self._required_text(
                draft.target_source_revision,
                'target_source_revision',
                128,
            ),
            map_id=self._required_text(
                draft.target_map_id,
                'target_map_id',
                128,
            ),
            map_revision=self._required_text(
                draft.target_map_revision,
                'target_map_revision',
                128,
            ),
            semantic_revision=self._confirmation_digest(
                draft.target_semantic_revision,
                'target_semantic_revision',
            ),
            frame_id=self._required_text(
                draft.target_frame_id,
                'target_frame_id',
                32,
            ),
            room_id=self._required_text(
                draft.target_room_id,
                'target_room_id',
                128,
            ),
            room_name=self._required_text(
                draft.target_room_name,
                'target_room_name',
                128,
            ),
            room_category=self._required_text(
                draft.target_room_category,
                'target_room_category',
                64,
            ),
            source_arguments_digest=self._confirmation_digest(
                draft.target_source_arguments_digest,
                'target_source_arguments_digest',
            ),
            geometry_json=geometry_json,
            geometry_digest=self._confirmation_digest(
                draft.target_geometry_digest,
                'target_geometry_digest',
            ),
            representative_point=(
                self._confirmation_number(
                    draft.target_representative_x,
                    'target_representative_x',
                ),
                self._confirmation_number(
                    draft.target_representative_y,
                    'target_representative_y',
                ),
            ),
            clearance_m=self._confirmation_positive_number(
                draft.target_clearance_m,
                'target_clearance_m',
            ),
            area_m2=self._confirmation_positive_number(
                draft.target_area_m2,
                'target_area_m2',
            ),
            effects=effects,
        )
        expected_arguments = self._confirmation_digest(
            draft.arguments_digest,
            'arguments_digest',
        )
        expected_target = self._confirmation_digest(
            draft.target_binding_digest,
            'target_binding_digest',
        )
        expected_effects = self._confirmation_digest(
            draft.effects_digest,
            'effects_digest',
        )
        if (
            target.source_arguments_digest != expected_arguments
            or target.binding_digest != expected_target
            or target.effects_digest != expected_effects
        ):
            raise ValidationError(
                'confirmation target binding digest does not match'
            )
        return target

    def _register_confirmation_intent_locked(
        self,
        draft: ConfirmationIntentDraft,
        token: BeginTurnToken,
        response: Dict[str, Any],
        now: float,
    ) -> DurableConfirmationIntent:
        """Attach one draft to the turn being committed."""
        if (
            draft.user_id != token.user_id
            or draft.conversation_id != token.conversation_id
            or draft.session_instance_id != token.session_instance_id
            or draft.generation != token.generation
            or draft.revision != token.revision + 1
            or draft.ordinal != token.ordinal
            or draft.turn_id != token.turn_id
            or draft.agent_request_id != token.request_id
        ):
            raise ConversationChangedError(
                'confirmation intent does not match completed turn'
            )
        self._validate_confirmation_draft_response(draft, response)
        return self._insert_confirmation_intent_locked(draft, now)

    def _validate_confirmation_draft_response(
        self,
        draft: ConfirmationIntentDraft,
        response: Dict[str, Any],
    ) -> None:
        """Bind storage input to the exact persisted safe decision."""
        try:
            response_schema = response['schema_version']
            if response_schema not in {3, 4}:
                raise ValueError
            public = response['public']
            conversation = public['conversation']
            decision = public['decision']
            safety = public['safety']
            execution = public['execution']
            arguments_json = json.dumps(
                decision['arguments'],
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            )
            target = self._normalize_confirmation_target(draft)
            simulation_profile = (
                target.effects.gazebo_simulation_navigation
            )
            state_evidence = response.get('state_evidence')
            simulation_evidence_matches = not simulation_profile
            if simulation_profile:
                expected_state_fields = {
                    'schema_version', 'scope', 'profile',
                    'evidence_digest', 'user_id', 'device_id',
                    'map_id', 'map_revision', 'host_boot_id',
                    'instance_id', 'sequence',
                    'assembled_boottime_ns',
                    'valid_until_boottime_ns',
                    'semantic_content_sha256', 'zones_digest',
                    'semantic_map_generation',
                    'semantic_authorization_generation',
                    'semantic_expires_at_ms', 'room_id',
                    'geometry_digest', 'source_arguments_digest',
                    'target_binding_digest', 'effects_digest',
                }
                public_state_evidence = execution.get('state_evidence')
                simulation_evidence_matches = (
                    response_schema == 4
                    and type(state_evidence) is dict
                    and set(state_evidence) == expected_state_fields
                    and state_evidence.get('schema_version') == 1
                    and state_evidence.get('scope') == 'monitor_room'
                    and state_evidence.get('profile')
                    == 'gazebo_simulation_monitor_room_v1'
                    and state_evidence.get('user_id') == draft.user_id
                    and state_evidence.get('device_id')
                    == target.device_id
                    and state_evidence.get('map_id') == target.map_id
                    and state_evidence.get('map_revision')
                    == target.map_revision
                    and state_evidence.get('room_id') == target.room_id
                    and state_evidence.get('geometry_digest')
                    == target.geometry_digest
                    and state_evidence.get('source_arguments_digest')
                    == target.source_arguments_digest
                    and state_evidence.get('target_binding_digest')
                    == target.binding_digest
                    and state_evidence.get('effects_digest')
                    == target.effects_digest
                    and type(public_state_evidence) is dict
                    and public_state_evidence.get('scope')
                    == 'monitor_room'
                    and public_state_evidence.get('evidence_digest')
                    == state_evidence.get('evidence_digest')
                    and public_state_evidence.get('current') is True
                    and execution.get('state_evidence_scope')
                    == 'monitor_room'
                )
            fingerprint_body = {
                'schema_version': draft.schema_version,
                'agent_request_id': draft.agent_request_id,
                'user_id': draft.user_id,
                'speech_session_id': draft.speech_session_id,
                'source_utterance_id': draft.source_utterance_id,
                'conversation_id': draft.conversation_id,
                'conversation_session_instance_id': (
                    draft.session_instance_id
                ),
                'conversation_generation': draft.generation,
                'conversation_revision': draft.revision,
                'conversation_ordinal': draft.ordinal,
                'turn_id': draft.turn_id,
                'decision_id': draft.decision_id,
                'tool_name': draft.tool_name,
                'arguments': json.loads(arguments_json),
                'issued_at': draft.issued_at,
                'expires_at': draft.expires_at,
                'risk_level': draft.risk_level,
                'message': draft.confirmation_message,
                'target': target.to_private_dict(),
            }
            encoded_fingerprint = json.dumps(
                fingerprint_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ).encode('utf-8')
            matches = (
                simulation_evidence_matches
                and (
                    (simulation_profile and response_schema == 4)
                    or (not simulation_profile and response_schema == 3)
                )
                and public['request_id'] == draft.agent_request_id
                and conversation['conversation_id']
                == draft.conversation_id
                and conversation['session_instance_id']
                == draft.session_instance_id
                and conversation['turn_id'] == draft.turn_id
                and int(conversation['generation']) == draft.generation
                and int(conversation['revision']) == draft.revision
                and int(conversation['ordinal']) == draft.ordinal
                and decision['type'] == 'tool_call'
                and decision['tool_name'] == draft.tool_name
                and hashlib.sha256(
                    arguments_json.encode('utf-8')
                ).hexdigest() == draft.arguments_digest
                and safety['allowed'] is True
                and execution['decision_id'] == draft.decision_id
                and float(execution['issued_at']) == draft.issued_at
                and float(execution['expires_at']) == draft.expires_at
                and execution['proposal_authorized'] is True
                and execution['state_trusted'] is True
                and execution['authorized'] is False
                and execution['consume_once'] is False
                and execution['tool_call_id'] is None
                and hashlib.sha256(encoded_fingerprint).hexdigest()
                == draft.proposal_fingerprint
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            matches = False
        if not matches:
            raise ConfirmationIntentConflictError(
                'confirmation intent does not match safe response'
            )

    def _insert_confirmation_intent_locked(
        self,
        draft: ConfirmationIntentDraft,
        now: float,
    ) -> DurableConfirmationIntent:
        """Insert one pending draft or replay its exact durable row."""
        existing = self._connection.execute(
            '''
            SELECT * FROM confirmation_intents
            WHERE confirmation_request_id = ?
            ''',
            (draft.confirmation_request_id,),
        ).fetchone()
        if existing is not None:
            if not self._confirmation_draft_matches_row(draft, existing):
                raise ConfirmationIntentConflictError(
                    'confirmation request id conflict'
                )
            return self._confirmation_intent_from_row(existing)
        if now >= draft.expires_at:
            raise ConversationChangedError(
                'confirmation intent expired before commit'
            )
        columns = (
            'schema_version',
            'confirmation_request_id',
            'agent_request_id',
            'user_id',
            'speech_session_id',
            'source_utterance_id',
            'conversation_id',
            'session_instance_id',
            'generation',
            'revision',
            'ordinal',
            'turn_id',
            'decision_id',
            'tool_name',
            'arguments_digest',
            'proposal_fingerprint',
            'issued_at',
            'expires_at',
            'risk_level',
            'confirmation_message',
            'target_binding_schema_version',
            'target_device_id',
            'target_device_binding_revision',
            'target_source_revision',
            'target_map_id',
            'target_map_revision',
            'target_semantic_revision',
            'target_frame_id',
            'target_room_id',
            'target_room_name',
            'target_room_category',
            'target_geometry_json',
            'target_geometry_digest',
            'target_representative_x',
            'target_representative_y',
            'target_clearance_m',
            'target_area_m2',
            'target_source_arguments_digest',
            'target_binding_digest',
            'effects_schema_version',
            'effect_physical_navigation',
            'effect_camera_capture',
            'effect_external_video_stream',
            'effect_video_recording',
            'effect_audio_capture',
            'effect_coverage_mode',
            'effect_viewer_scope',
            'effect_talkback_allowed',
            'effect_max_duration_seconds',
            'effects_digest',
            'state',
            'created_at',
            'updated_at',
        )
        values = (
            draft.schema_version,
            draft.confirmation_request_id,
            draft.agent_request_id,
            draft.user_id,
            draft.speech_session_id,
            draft.source_utterance_id,
            draft.conversation_id,
            draft.session_instance_id,
            draft.generation,
            draft.revision,
            draft.ordinal,
            draft.turn_id,
            draft.decision_id,
            draft.tool_name,
            draft.arguments_digest,
            draft.proposal_fingerprint,
            draft.issued_at,
            draft.expires_at,
            draft.risk_level,
            draft.confirmation_message,
            draft.target_binding_schema_version,
            draft.target_device_id,
            draft.target_device_binding_revision,
            draft.target_source_revision,
            draft.target_map_id,
            draft.target_map_revision,
            draft.target_semantic_revision,
            draft.target_frame_id,
            draft.target_room_id,
            draft.target_room_name,
            draft.target_room_category,
            draft.target_geometry_json,
            draft.target_geometry_digest,
            draft.target_representative_x,
            draft.target_representative_y,
            draft.target_clearance_m,
            draft.target_area_m2,
            draft.target_source_arguments_digest,
            draft.target_binding_digest,
            draft.effects_schema_version,
            int(draft.effect_physical_navigation),
            int(draft.effect_camera_capture),
            int(draft.effect_external_video_stream),
            int(draft.effect_video_recording),
            int(draft.effect_audio_capture),
            draft.effect_coverage_mode,
            draft.effect_viewer_scope,
            int(draft.effect_talkback_allowed),
            draft.effect_max_duration_seconds,
            draft.effects_digest,
            'pending',
            now,
            now,
        )
        reserved = self._connection.execute(
            '''
            SELECT 1 FROM monitor_room_simulation_ledger
            WHERE confirmation_request_id = ? OR decision_id = ?
            LIMIT 1
            ''',
            (
                draft.confirmation_request_id,
                draft.decision_id,
            ),
        ).fetchone()
        if reserved is not None:
            raise ConfirmationIntentConflictError(
                'confirmation intent identity is permanently reserved'
            )
        try:
            self._connection.execute(
                'INSERT INTO confirmation_intents ('
                + ', '.join(columns)
                + ') VALUES ('
                + ', '.join('?' for _column in columns)
                + ')',
                values,
            )
        except sqlite3.IntegrityError as error:
            raise ConfirmationIntentConflictError(
                'confirmation intent conflict'
            ) from error
        _mark_confirmation_simulation_eligible_locked(
            self._connection,
            confirmation_request_id=draft.confirmation_request_id,
        )
        row = self._connection.execute(
            '''
            SELECT * FROM confirmation_intents
            WHERE confirmation_request_id = ?
            ''',
            (draft.confirmation_request_id,),
        ).fetchone()
        return self._confirmation_intent_from_row(row)

    @staticmethod
    def _confirmation_draft_matches_row(
        draft: ConfirmationIntentDraft,
        row: sqlite3.Row,
    ) -> bool:
        """Compare every immutable field without exposing a mismatch."""
        return (
            int(row['schema_version']) == draft.schema_version
            and row['agent_request_id'] == draft.agent_request_id
            and row['user_id'] == draft.user_id
            and row['speech_session_id'] == draft.speech_session_id
            and row['source_utterance_id']
            == draft.source_utterance_id
            and row['conversation_id'] == draft.conversation_id
            and row['session_instance_id'] == draft.session_instance_id
            and int(row['generation']) == draft.generation
            and int(row['revision']) == draft.revision
            and int(row['ordinal']) == draft.ordinal
            and row['turn_id'] == draft.turn_id
            and row['decision_id'] == draft.decision_id
            and row['tool_name'] == draft.tool_name
            and row['arguments_digest'] == draft.arguments_digest
            and row['proposal_fingerprint']
            == draft.proposal_fingerprint
            and float(row['issued_at']) == draft.issued_at
            and float(row['expires_at']) == draft.expires_at
            and row['risk_level'] == draft.risk_level
            and row['confirmation_message']
            == draft.confirmation_message
            and row['target_binding_schema_version']
            == draft.target_binding_schema_version
            and row['target_device_id'] == draft.target_device_id
            and row['target_device_binding_revision']
            == draft.target_device_binding_revision
            and row['target_source_revision']
            == draft.target_source_revision
            and row['target_map_id'] == draft.target_map_id
            and row['target_map_revision'] == draft.target_map_revision
            and row['target_semantic_revision']
            == draft.target_semantic_revision
            and row['target_frame_id'] == draft.target_frame_id
            and row['target_room_id'] == draft.target_room_id
            and row['target_room_name'] == draft.target_room_name
            and row['target_room_category']
            == draft.target_room_category
            and row['target_geometry_json'] == draft.target_geometry_json
            and row['target_geometry_digest']
            == draft.target_geometry_digest
            and row['target_representative_x']
            == draft.target_representative_x
            and row['target_representative_y']
            == draft.target_representative_y
            and row['target_clearance_m'] == draft.target_clearance_m
            and row['target_area_m2'] == draft.target_area_m2
            and row['target_source_arguments_digest']
            == draft.target_source_arguments_digest
            and row['target_binding_digest']
            == draft.target_binding_digest
            and row['effects_schema_version']
            == draft.effects_schema_version
            and row['effect_physical_navigation']
            == int(draft.effect_physical_navigation)
            and row['effect_camera_capture']
            == int(draft.effect_camera_capture)
            and row['effect_external_video_stream']
            == int(draft.effect_external_video_stream)
            and row['effect_video_recording']
            == int(draft.effect_video_recording)
            and row['effect_audio_capture']
            == int(draft.effect_audio_capture)
            and row['effect_coverage_mode']
            == draft.effect_coverage_mode
            and row['effect_viewer_scope'] == draft.effect_viewer_scope
            and row['effect_talkback_allowed']
            == int(draft.effect_talkback_allowed)
            and row['effect_max_duration_seconds']
            == draft.effect_max_duration_seconds
            and row['effects_digest'] == draft.effects_digest
        )

    def _confirmation_context_code_locked(
        self,
        row: sqlite3.Row,
    ) -> Optional[str]:
        """Classify current context while the write transaction is held."""
        session_row = self._select_session_locked(
            row['user_id'],
            row['conversation_id'],
        )
        if session_row is None:
            return 'confirmation_conversation_not_found'
        session = self._session_from_row(session_row)
        if session.status != 'active':
            return 'confirmation_conversation_inactive'
        if (
            session.session_instance_id != row['session_instance_id']
            or session.generation != int(row['generation'])
            or session.revision != int(row['revision'])
        ):
            return 'confirmation_conversation_changed'
        return None

    def _terminalize_confirmation_invalidation_locked(
        self,
        row: sqlite3.Row,
        code: str,
        response_id: str,
        response_fingerprint: str,
        requested_disposition: str,
        response_channel: str,
        assurance_level: str,
        provenance_ref: str,
        verifier_ref: Optional[str],
        now: float,
    ) -> None:
        """Persist one context tombstone without creating a resolution."""
        cursor = self._connection.execute(
            '''
            UPDATE confirmation_intents
            SET state = 'invalidated',
                result_code = ?,
                response_id = ?,
                response_fingerprint = ?,
                requested_disposition = ?,
                response_channel = ?,
                assurance_level = ?,
                provenance_ref = ?,
                verifier_ref = ?,
                resolved_at = ?,
                updated_at = ?
            WHERE confirmation_request_id = ?
              AND state = 'pending'
            ''',
            (
                code,
                response_id,
                response_fingerprint,
                requested_disposition,
                response_channel,
                assurance_level,
                provenance_ref,
                verifier_ref,
                now,
                now,
                row['confirmation_request_id'],
            ),
        )
        if cursor.rowcount != 1:
            raise ConfirmationIntentConflictError(
                'confirmation intent is already terminal'
            )

    def _terminalize_system_invalidation_locked(
        self,
        row: sqlite3.Row,
        code: str,
        now: float,
    ) -> None:
        """Write one content-free lifecycle tombstone during housekeeping."""
        cursor = self._connection.execute(
            '''
            UPDATE confirmation_intents
            SET state = 'invalidated',
                result_code = ?,
                resolved_at = ?,
                updated_at = ?
            WHERE confirmation_request_id = ?
              AND state = 'pending'
            ''',
            (
                code,
                now,
                now,
                row['confirmation_request_id'],
            ),
        )
        if cursor.rowcount != 1:
            raise ConfirmationIntentConflictError(
                'confirmation intent is already terminal'
            )

    @staticmethod
    def _confirmation_result_id(
        confirmation_request_id: str,
        response_channel: str,
        response_id: str,
    ) -> str:
        del response_channel
        digest = hashlib.sha256(
            (
                'confirmation-result-v1\0'
                f'{confirmation_request_id}\0'
                f'{response_id}'
            ).encode('utf-8')
        ).hexdigest()[:40]
        return f'confirmation-result-{digest}'

    @staticmethod
    def _confirmation_intent_from_row(
        row: sqlite3.Row,
    ) -> DurableConfirmationIntent:
        """Build one typed record from the private durable row."""
        if row is None:
            raise RuntimeError('stored confirmation intent is missing')

        def optional_float(name: str) -> Optional[float]:
            value = row[name]
            return float(value) if value is not None else None

        def optional_int(name: str) -> Optional[int]:
            value = row[name]
            return int(value) if value is not None else None

        def optional_bool(name: str) -> Optional[bool]:
            value = row[name]
            return bool(int(value)) if value is not None else None

        return DurableConfirmationIntent(
            schema_version=int(row['schema_version']),
            confirmation_request_id=row['confirmation_request_id'],
            agent_request_id=row['agent_request_id'],
            user_id=row['user_id'],
            speech_session_id=row['speech_session_id'],
            source_utterance_id=row['source_utterance_id'],
            conversation_id=row['conversation_id'],
            session_instance_id=row['session_instance_id'],
            generation=int(row['generation']),
            revision=int(row['revision']),
            ordinal=int(row['ordinal']),
            turn_id=row['turn_id'],
            decision_id=row['decision_id'],
            tool_name=row['tool_name'],
            arguments_digest=row['arguments_digest'],
            proposal_fingerprint=row['proposal_fingerprint'],
            issued_at=float(row['issued_at']),
            expires_at=float(row['expires_at']),
            risk_level=row['risk_level'],
            confirmation_message=row['confirmation_message'],
            target_binding_schema_version=optional_int(
                'target_binding_schema_version'
            ),
            target_device_id=row['target_device_id'],
            target_device_binding_revision=(
                row['target_device_binding_revision']
            ),
            target_source_revision=row['target_source_revision'],
            target_map_id=row['target_map_id'],
            target_map_revision=row['target_map_revision'],
            target_semantic_revision=row['target_semantic_revision'],
            target_frame_id=row['target_frame_id'],
            target_room_id=row['target_room_id'],
            target_room_name=row['target_room_name'],
            target_room_category=row['target_room_category'],
            target_geometry_json=row['target_geometry_json'],
            target_geometry_digest=row['target_geometry_digest'],
            target_representative_x=optional_float(
                'target_representative_x'
            ),
            target_representative_y=optional_float(
                'target_representative_y'
            ),
            target_clearance_m=optional_float('target_clearance_m'),
            target_area_m2=optional_float('target_area_m2'),
            target_source_arguments_digest=(
                row['target_source_arguments_digest']
            ),
            target_binding_digest=row['target_binding_digest'],
            effects_schema_version=optional_int('effects_schema_version'),
            effect_physical_navigation=optional_bool(
                'effect_physical_navigation'
            ),
            effect_camera_capture=optional_bool('effect_camera_capture'),
            effect_external_video_stream=optional_bool(
                'effect_external_video_stream'
            ),
            effect_video_recording=optional_bool(
                'effect_video_recording'
            ),
            effect_audio_capture=optional_bool('effect_audio_capture'),
            effect_coverage_mode=row['effect_coverage_mode'],
            effect_viewer_scope=row['effect_viewer_scope'],
            effect_talkback_allowed=optional_bool(
                'effect_talkback_allowed'
            ),
            effect_max_duration_seconds=optional_int(
                'effect_max_duration_seconds'
            ),
            effects_digest=row['effects_digest'],
            state=row['state'],
            disposition=row['disposition'],
            requested_disposition=row['requested_disposition'],
            result_code=row['result_code'],
            confirmation_result_id=row['confirmation_result_id'],
            response_id=row['response_id'],
            response_fingerprint=row['response_fingerprint'],
            response_channel=row['response_channel'],
            assurance_level=row['assurance_level'],
            provenance_ref=row['provenance_ref'],
            verifier_ref=row['verifier_ref'],
            resolved_at=(
                float(row['resolved_at'])
                if row['resolved_at'] is not None
                else None
            ),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
        )

    @staticmethod
    def _confirmation_digest(value: Any, name: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f'{name} must be a string')
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in '0123456789abcdef'
            for character in normalized
        ):
            raise ValidationError(f'{name} must be a sha256 digest')
        return normalized

    @staticmethod
    def _confirmation_timestamp(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f'{name} must be a number')
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValidationError(f'{name} must be a finite timestamp')
        return normalized

    @staticmethod
    def _confirmation_number(value: Any, name: str) -> float:
        """Return one exact finite non-boolean number."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f'{name} must be a number')
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValidationError(f'{name} must be finite')
        return normalized

    @classmethod
    def _confirmation_positive_number(cls, value: Any, name: str) -> float:
        """Return one finite positive target measurement."""
        normalized = cls._confirmation_number(value, name)
        if normalized <= 0:
            raise ValidationError(f'{name} must be positive')
        return normalized

    def _reuse_existing_locked(
        self,
        row: sqlite3.Row,
        conversation_id: str,
        turn_id: str,
        request_fingerprint: str,
        user_content: str,
        session: ConversationSession,
    ) -> BeginTurnResult:
        same_request = (
            row['conversation_id'] == conversation_id
            and row['turn_id'] == turn_id
            and row['request_fingerprint'] == request_fingerprint
            and row['user_content'] == user_content
        )
        if not same_request:
            raise ConversationConflictError(
                'request_id was already used with different input'
            )
        if int(row['generation']) != session.generation:
            raise ConversationConflictError(
                'request belongs to a previous conversation generation'
            )
        if row['session_instance_id'] != session.session_instance_id:
            raise ConversationConflictError(
                'request belongs to a previous conversation instance'
            )
        if row['status'] != 'completed':
            raise ConversationConflictError(
                'the same turn is already in progress'
            )
        response = self._load_response(row['response_json'])
        return BeginTurnResult(
            session=session,
            history=(),
            summary=None,
            token=None,
            cached_response=response,
        )

    def _advance_summary_locked(
        self,
        token: BeginTurnToken,
        now: float,
    ) -> Optional[ConversationSummary]:
        """Atomically extend the summary beyond the recent raw window."""
        source_end = token.ordinal - self.history_limit
        if source_end < 1:
            return None
        session_row = self._select_session_locked(
            token.user_id,
            token.conversation_id,
        )
        session = self._session_from_row(session_row)
        existing_row = self._select_summary_locked(session)
        existing = (
            self._summary_from_row(existing_row)
            if existing_row is not None
            else None
        )
        previous_end = (
            existing.source_end_ordinal
            if existing is not None
            else 0
        )
        if previous_end >= source_end:
            return existing
        source_turns = self._summary_source_turns_locked(
            token,
            previous_end,
            source_end,
        )
        expected_ordinals = list(
            range(previous_end + 1, source_end + 1)
        )
        actual_ordinals = [
            turn.ordinal
            for turn in source_turns
        ]
        previous_state = (
            existing_row['state_json']
            if existing_row is not None
            else ''
        )
        fallback_used = False
        digest_previous = (
            existing.source_digest
            if existing is not None
            else ''
        )
        digest_turns = source_turns
        try:
            if actual_ordinals != expected_ordinals:
                raise RuntimeError(
                    'summary source turns are not contiguous'
                )
            result = self._summarize_in_batches(
                self._summarizer,
                previous_state,
                source_turns,
                source_end,
            )
            if result.fallback_used:
                raise RuntimeError(
                    'configured summarizer requested recovery'
                )
            content = result.content
            state_json = result.state_json
            summarizer_name = result.algorithm
        except Exception:
            fallback_used = True
            full_turns = self._summary_source_turns_locked(
                token,
                0,
                source_end,
            )
            full_ordinals = [
                turn.ordinal
                for turn in full_turns
            ]
            if full_ordinals != list(range(1, source_end + 1)):
                full_turns = []
            recovery = ExtractiveConversationSummarizer()
            result = self._summarize_in_batches(
                recovery,
                '',
                full_turns,
                source_end,
            )
            content = result.content
            state_json = result.state_json
            summarizer_name = result.algorithm
            digest_previous = ''
            digest_turns = full_turns

        source_digest = self._extend_source_digest(
            digest_previous,
            digest_turns,
        )
        summary_id = (
            existing.summary_id
            if existing is not None
            else str(uuid.uuid4())
        )
        summary_revision = (
            existing.summary_revision + 1
            if existing is not None
            else 1
        )
        created_at = (
            existing.created_at
            if existing is not None
            else now
        )
        self._connection.execute(
            '''
            INSERT INTO conversation_summaries (
                user_id,
                conversation_id,
                session_instance_id,
                generation,
                summary_id,
                summary_revision,
                content,
                state_json,
                source_start_ordinal,
                source_end_ordinal,
                source_turn_count,
                source_digest,
                summarizer,
                fallback_used,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT (
                user_id,
                conversation_id,
                session_instance_id,
                generation
            )
            DO UPDATE SET
                summary_id = excluded.summary_id,
                summary_revision = excluded.summary_revision,
                content = excluded.content,
                state_json = excluded.state_json,
                source_start_ordinal =
                    excluded.source_start_ordinal,
                source_end_ordinal = excluded.source_end_ordinal,
                source_turn_count = excluded.source_turn_count,
                source_digest = excluded.source_digest,
                summarizer = excluded.summarizer,
                fallback_used = excluded.fallback_used,
                updated_at = excluded.updated_at
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                summary_id,
                summary_revision,
                content,
                state_json,
                1,
                source_end,
                source_end,
                source_digest,
                summarizer_name,
                int(fallback_used),
                created_at,
                now,
            ),
        )
        row = self._select_summary_locked(session)
        return self._summary_from_row(row)

    def _summary_source_turns_locked(
        self,
        token: BeginTurnToken,
        start_exclusive: int,
        end_inclusive: int,
    ) -> List[SummarySourceTurn]:
        rows = self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND status = 'completed'
              AND ordinal > ?
              AND ordinal <= ?
            ORDER BY ordinal ASC
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                start_exclusive,
                end_inclusive,
            ),
        ).fetchall()
        return [
            SummarySourceTurn(
                ordinal=int(row['ordinal']),
                turn_id=row['turn_id'],
                user_content=row['user_content'],
                assistant_content=row['assistant_content'],
            )
            for row in rows
        ]

    def _summarize_in_batches(
        self,
        summarizer: Any,
        previous_state: str,
        turns: List[SummarySourceTurn],
        source_end: int,
    ) -> SummaryResult:
        """Feed every source turn through a bounded rolling state."""
        batches = [
            turns[index:index + SUMMARY_UPDATE_BATCH_SIZE]
            for index in range(
                0,
                len(turns),
                SUMMARY_UPDATE_BATCH_SIZE,
            )
        ] or [[]]
        state = previous_state
        result: Optional[SummaryResult] = None
        for batch in batches:
            candidate = summarizer.update(
                previous_state_json=state,
                new_turns=batch,
                source_start_ordinal=1,
                source_end_ordinal=source_end,
                source_turn_count=source_end,
                max_chars=self.summary_max_chars,
            )
            if (
                not isinstance(candidate, SummaryResult)
                or not isinstance(candidate.content, str)
                or not isinstance(candidate.state_json, str)
                or not isinstance(candidate.algorithm, str)
                or len(candidate.content) > self.summary_max_chars
                or len(candidate.state_json) > 262144
            ):
                raise RuntimeError(
                    'summarizer returned an invalid bounded result'
                )
            state = candidate.state_json
            result = candidate
        if result is None:
            raise RuntimeError('summarizer returned no result')
        return result

    def _summary_for_window_locked(
        self,
        session: ConversationSession,
        completed_turn_count: int,
        now: float,
    ) -> Optional[ConversationSummary]:
        """Align persisted summary coverage with the configured raw N."""
        desired_end = max(
            0,
            completed_turn_count - self.history_limit,
        )
        row = self._select_summary_locked(session)
        existing = (
            self._summary_from_row(row)
            if row is not None
            else None
        )
        coverage_invalid = (
            existing is not None
            and (
                existing.source_start_ordinal != 1
                or existing.source_end_ordinal > desired_end
                or (
                    existing.source_turn_count
                    != existing.source_end_ordinal
                )
            )
        )
        if coverage_invalid:
            self._connection.execute(
                '''
                DELETE FROM conversation_summaries
                WHERE user_id = ?
                  AND conversation_id = ?
                  AND session_instance_id = ?
                  AND generation = ?
                ''',
                (
                    session.user_id,
                    session.conversation_id,
                    session.session_instance_id,
                    session.generation,
                ),
            )
            existing = None
        if desired_end == 0:
            return None
        if (
            existing is not None
            and existing.source_end_ordinal == desired_end
        ):
            return existing
        synthetic = BeginTurnToken(
            user_id=session.user_id,
            conversation_id=session.conversation_id,
            session_instance_id=session.session_instance_id,
            turn_id='',
            request_id='',
            request_fingerprint='',
            generation=session.generation,
            revision=session.revision,
            ordinal=completed_turn_count,
        )
        return self._advance_summary_locked(synthetic, now)

    @staticmethod
    def _extend_source_digest(
        previous_digest: str,
        turns: List[SummarySourceTurn],
    ) -> str:
        """Extend a deterministic per-turn digest chain."""
        try:
            digest_bytes = bytes.fromhex(previous_digest)
            if len(digest_bytes) != 32:
                raise ValueError
        except (TypeError, ValueError):
            digest_bytes = hashlib.sha256(b'').digest()
        for turn in turns:
            canonical = json.dumps(
                {
                    'ordinal': turn.ordinal,
                    'turn_id': turn.turn_id,
                    'user': turn.user_content,
                    'assistant': turn.assistant_content,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')
            digest_bytes = hashlib.sha256(
                digest_bytes + canonical
            ).digest()
        return digest_bytes.hex()

    def _history_locked(
        self,
        user_id: str,
        conversation_id: str,
        session_instance_id: str,
        generation: int,
        limit: int,
    ) -> List[ConversationTurn]:
        rows = self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND status = 'completed'
            ORDER BY ordinal DESC
            LIMIT ?
            ''',
            (
                user_id,
                conversation_id,
                session_instance_id,
                generation,
                limit,
            ),
        ).fetchall()
        return [
            self._turn_from_row(row)
            for row in reversed(rows)
        ]

    def _expire_and_commit(self, now: float) -> None:
        self._begin()
        try:
            self._expire_due_locked(now)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _invalidate_pending_confirmations_locked(
        self,
        user_id: str,
        conversation_id: str,
        code: str,
        now: float,
    ) -> int:
        """Tombstone every pending request before a lifecycle mutation."""
        cursor = self._connection.execute(
            '''
            UPDATE confirmation_intents
            SET state = 'invalidated',
                result_code = ?,
                resolved_at = ?,
                updated_at = ?
            WHERE user_id = ?
              AND conversation_id = ?
              AND state = 'pending'
            ''',
            (code, now, now, user_id, conversation_id),
        )
        return cursor.rowcount

    def _expire_due_locked(self, now: float) -> int:
        due_rows = self._connection.execute(
            '''
            SELECT user_id, conversation_id, session_instance_id
            FROM conversation_sessions
            WHERE status = 'active' AND expires_at <= ?
            ''',
            (now,),
        ).fetchall()
        for row in due_rows:
            self._invalidate_pending_confirmations_locked(
                row['user_id'],
                row['conversation_id'],
                'confirmation_conversation_inactive',
                now,
            )
            cancel_trusted_result_tts_locked(
                self._connection,
                user_id=row['user_id'],
                conversation_id=row['conversation_id'],
                session_instance_id=row['session_instance_id'],
                generation=None,
                cancellation_code='conversation_inactive',
                now=now,
            )
            self._connection.execute(
                '''
                DELETE FROM conversation_turns
                WHERE user_id = ?
                  AND conversation_id = ?
                  AND session_instance_id = ?
                  AND status = 'pending'
                ''',
                (
                    row['user_id'],
                    row['conversation_id'],
                    row['session_instance_id'],
                ),
            )
            self._connection.execute(
                '''
                DELETE FROM conversation_summaries
                WHERE user_id = ?
                  AND conversation_id = ?
                  AND session_instance_id = ?
                ''',
                (
                    row['user_id'],
                    row['conversation_id'],
                    row['session_instance_id'],
                ),
            )
        cursor = self._connection.execute(
            '''
            UPDATE conversation_sessions
            SET status = 'expired',
                generation = generation + 1,
                revision = revision + 1,
                updated_at = ?
            WHERE status = 'active' AND expires_at <= ?
            ''',
            (now, now),
        )
        return cursor.rowcount

    def _existing_request_locked(
        self,
        user_id: str,
        request_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ? AND request_id = ?
            ''',
            (user_id, request_id),
        ).fetchone()

    def _select_session_locked(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_sessions
            WHERE user_id = ? AND conversation_id = ?
            ''',
            (user_id, conversation_id),
        ).fetchone()

    def _select_summary_locked(
        self,
        session: ConversationSession,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_summaries
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
            ''',
            (
                session.user_id,
                session.conversation_id,
                session.session_instance_id,
                session.generation,
            ),
        ).fetchone()

    def _select_turn_locked(
        self,
        token: BeginTurnToken,
    ) -> Optional[sqlite3.Row]:
        return self._connection.execute(
            '''
            SELECT *
            FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND turn_id = ?
              AND status = 'completed'
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                token.turn_id,
            ),
        ).fetchone()

    def _delete_pending_locked(
        self,
        token: BeginTurnToken,
    ) -> None:
        self._connection.execute(
            '''
            DELETE FROM conversation_turns
            WHERE user_id = ?
              AND conversation_id = ?
              AND session_instance_id = ?
              AND generation = ?
              AND turn_id = ?
              AND request_id = ?
              AND status = 'pending'
            ''',
            (
                token.user_id,
                token.conversation_id,
                token.session_instance_id,
                token.generation,
                token.turn_id,
                token.request_id,
            ),
        )

    @staticmethod
    def _require_active(
        row: Optional[sqlite3.Row],
    ) -> ConversationSession:
        if row is None:
            raise ConversationNotFoundError(
                'conversation was not found'
            )
        session = SQLiteConversationStore._session_from_row(row)
        if session.status != 'active':
            raise ConversationStateError(
                f'conversation is {session.status}'
            )
        return session

    @staticmethod
    def _session_from_row(
        row: sqlite3.Row,
    ) -> ConversationSession:
        return ConversationSession(
            conversation_id=row['conversation_id'],
            user_id=row['user_id'],
            session_instance_id=row['session_instance_id'],
            status=row['status'],
            generation=int(row['generation']),
            revision=int(row['revision']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
            expires_at=float(row['expires_at']),
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> ConversationTurn:
        if row is None or row['status'] != 'completed':
            raise RuntimeError('conversation turn is not completed')
        response = SQLiteConversationStore._load_response(
            row['response_json']
        )
        return ConversationTurn(
            conversation_id=row['conversation_id'],
            user_id=row['user_id'],
            session_instance_id=row['session_instance_id'],
            turn_id=row['turn_id'],
            request_id=row['request_id'],
            request_fingerprint=row['request_fingerprint'],
            generation=int(row['generation']),
            ordinal=int(row['ordinal']),
            user_content=row['user_content'],
            assistant_content=row['assistant_content'],
            response=response,
            created_at=float(row['created_at']),
            completed_at=float(row['completed_at']),
        )

    @staticmethod
    def _summary_from_row(
        row: sqlite3.Row,
    ) -> ConversationSummary:
        if row is None:
            raise RuntimeError('conversation summary is missing')
        return ConversationSummary(
            summary_id=row['summary_id'],
            user_id=row['user_id'],
            conversation_id=row['conversation_id'],
            session_instance_id=row['session_instance_id'],
            generation=int(row['generation']),
            summary_revision=int(row['summary_revision']),
            content=row['content'],
            source_start_ordinal=int(
                row['source_start_ordinal']
            ),
            source_end_ordinal=int(row['source_end_ordinal']),
            source_turn_count=int(row['source_turn_count']),
            source_digest=row['source_digest'],
            summarizer=row['summarizer'],
            fallback_used=bool(row['fallback_used']),
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
        )

    @staticmethod
    def _load_response(value: Any) -> Dict[str, Any]:
        if not isinstance(value, str):
            raise RuntimeError(
                'stored conversation response is missing'
            )
        try:
            response = json.loads(value)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                'stored conversation response is invalid'
            ) from error
        if not isinstance(response, dict):
            raise RuntimeError(
                'stored conversation response must be an object'
            )
        return response

    @staticmethod
    def _response_json(value: Any) -> str:
        if not isinstance(value, dict):
            raise ValidationError('response must be an object')
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        if len(rendered) > MAX_RESPONSE_JSON_LENGTH:
            raise ValidationError('response is too large')
        return rendered

    @staticmethod
    def _required_text(
        value: Any,
        name: str,
        maximum: int,
    ) -> str:
        if not isinstance(value, str):
            raise ValidationError(f'{name} must be a string')
        result = value.strip()
        if not result:
            raise ValidationError(f'{name} must not be empty')
        if len(result) > maximum:
            raise ValidationError(
                f'{name} must be at most {maximum} characters'
            )
        return result

    @staticmethod
    def _assistant_text(value: Any) -> str:
        if not isinstance(value, str):
            raise ValidationError(
                'assistant_content must be a string'
            )
        if len(value) > MAX_UTTERANCE_LENGTH:
            raise ValidationError(
                'assistant_content is too long'
            )
        return value

    @staticmethod
    def _bounded_integer(
        value: Any,
        name: str,
        minimum: int,
        maximum: int,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f'{name} must be an integer')
        if value < minimum or value > maximum:
            raise ValueError(
                f'{name} must be between {minimum} and {maximum}'
            )
        return value

    def _now(self) -> float:
        try:
            value = float(self._clock())
        except (OverflowError, TypeError, ValueError):
            raise ConversationClockError(
                'conversation clock is not finite'
            ) from None
        if not math.isfinite(value):
            raise ConversationClockError(
                'conversation clock is not finite'
            )
        return value

    def _begin(self) -> None:
        self._connection.execute('BEGIN IMMEDIATE')
