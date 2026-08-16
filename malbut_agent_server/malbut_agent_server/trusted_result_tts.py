"""
Durable, non-authorizing TTS feedback for trusted tool results.

The outbox stores only a versioned template identity.  Korean speech text is
rendered from the closed mapping below and is never accepted from a caller.
An acknowledged row means that a trusted downstream adapter acknowledged its
terminal request; it is not proof that a person heard audio.

Delivery is deliberately at-least-once: a process can play audio and crash
before ACK, so a later lease may redeliver it.  Downstream adapters must dedupe
the stable ``tts_request_id`` (the event id) across every claim fence.
"""

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from malbut_agent_server.execution_ledger import (
    SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL,
)
from malbut_agent_server.schemas import (
    ValidationError,
    validate_conversation_id,
    validate_user_id,
)
from malbut_agent_server.trusted_results import (
    TrustedToolResult,
    _trusted_result_from_row,
)


TRUSTED_RESULT_TTS_SCHEMA_VERSION = 1
TRUSTED_RESULT_TTS_TEMPLATE_VERSION = 'monitor-room-result-ko-v1'
TRUSTED_RESULT_TTS_MAX_ATTEMPTS = 5
TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS = 1
TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS = 300

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_CLAIM_TOKEN = re.compile(r'^[A-Za-z0-9_-]{32,128}$')

_TEMPLATES: Dict[str, Tuple[str, str]] = {
    'semantic_sample_plan_created': (
        'monitor-room-simulation-plan-created-ko-v1',
        '요청한 방의 확인 지점 계획을 시뮬레이션으로 만들었어요. '
        '로봇 이동, 카메라 촬영, 영상 재생은 아직 하지 않았어요.',
    ),
    'semantic_sample_planning_failed': (
        'monitor-room-simulation-planning-failed-ko-v1',
        '요청한 방의 확인 지점 계획을 시뮬레이션으로 만들지 '
        '못했어요. 로봇 이동, 카메라 촬영, 영상 재생은 하지 '
        '않았어요.',
    ),
    'semantic_sample_result_invalid': (
        'monitor-room-simulation-planning-failed-ko-v1',
        '요청한 방의 확인 지점 계획을 시뮬레이션으로 만들지 '
        '못했어요. 로봇 이동, 카메라 촬영, 영상 재생은 하지 '
        '않았어요.',
    ),
}

TRUSTED_RESULT_TTS_ACTIVATION_SENTINEL = hashlib.sha256(
    b'malbut-trusted-result-tts-activation-v1'
).hexdigest()

_PLANNED_TEMPLATE_KEY, _PLANNED_TEXT = _TEMPLATES[
    'semantic_sample_plan_created'
]
_FAILED_TEMPLATE_KEY, _FAILED_TEXT = _TEMPLATES[
    'semantic_sample_planning_failed'
]
_PLANNED_TEMPLATE_DIGEST = hashlib.sha256(
    _PLANNED_TEXT.encode('utf-8')
).hexdigest()
_FAILED_TEMPLATE_DIGEST = hashlib.sha256(
    _FAILED_TEXT.encode('utf-8')
).hexdigest()


TRUSTED_RESULT_TTS_METADATA_TABLE_SQL = '''
CREATE TABLE trusted_result_tts_schema_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    activated_at REAL NOT NULL,
    activation_epoch TEXT NOT NULL,
    preactivation_count INTEGER NOT NULL,
    preactivation_digest TEXT NOT NULL,
    CHECK (
        typeof(activated_at) IN ('integer', 'real')
        AND activated_at >= 0
        AND activated_at <= 1.7976931348623157e308
    ),
    CHECK (
        length(activation_epoch) = 64
        AND activation_epoch NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(preactivation_count) = 'integer'
        AND preactivation_count >= 0
    ),
    CHECK (
        length(preactivation_digest) = 64
        AND preactivation_digest NOT GLOB '*[^0-9a-f]*'
    )
)
'''


TRUSTED_RESULT_TTS_PREACTIVATION_TABLE_SQL = '''
CREATE TABLE trusted_result_tts_preactivation_sources (
    trusted_result_id TEXT NOT NULL PRIMARY KEY,
    trusted_result_fingerprint TEXT NOT NULL UNIQUE,
    CHECK (
        length(trusted_result_fingerprint) = 64
        AND trusted_result_fingerprint NOT GLOB '*[^0-9a-f]*'
    )
)
'''


TRUSTED_RESULT_TTS_OUTBOX_TABLE_SQL = f'''
CREATE TABLE trusted_result_tts_outbox (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    event_id TEXT NOT NULL PRIMARY KEY,
    event_fingerprint TEXT NOT NULL UNIQUE,
    trusted_result_id TEXT NOT NULL UNIQUE,
    trusted_result_fingerprint TEXT NOT NULL,
    confirmation_request_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    speech_session_id TEXT NOT NULL,
    session_instance_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    source_ordinal INTEGER NOT NULL,
    result_code TEXT NOT NULL,
    template_key TEXT NOT NULL,
    template_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'claimed', 'acknowledged', 'cancelled')
    ),
    attempt_count INTEGER NOT NULL,
    claim_fence INTEGER NOT NULL,
    current_claim_request_id TEXT,
    current_claim_request_fingerprint TEXT,
    current_claim_token TEXT,
    current_lease_seconds INTEGER,
    created_at REAL NOT NULL,
    last_transition_at REAL NOT NULL,
    claimed_at REAL,
    lease_expires_at REAL,
    acknowledged_at REAL,
    cancelled_at REAL,
    cancellation_code TEXT,
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL DEFAULT 0
        CHECK (physical_effects = 0),
    execution_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (execution_authorized = 0),
    physical_audio_verified INTEGER NOT NULL DEFAULT 0
        CHECK (physical_audio_verified = 0),
    FOREIGN KEY (trusted_result_id)
        REFERENCES conversation_trusted_tool_results (trusted_result_id)
        ON DELETE CASCADE,
    CHECK (typeof(generation) = 'integer' AND generation >= 1),
    CHECK (
        typeof(source_ordinal) = 'integer' AND source_ordinal >= 1
    ),
    CHECK (
        length(event_fingerprint) = 64
        AND event_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(trusted_result_fingerprint) = 64
        AND trusted_result_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(template_digest) = 64
        AND template_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        (result_code = 'semantic_sample_plan_created'
         AND template_key = '{_PLANNED_TEMPLATE_KEY}'
         AND template_digest = '{_PLANNED_TEMPLATE_DIGEST}')
        OR
        (result_code IN (
             'semantic_sample_planning_failed',
             'semantic_sample_result_invalid'
         )
         AND template_key = '{_FAILED_TEMPLATE_KEY}'
         AND template_digest = '{_FAILED_TEMPLATE_DIGEST}')
    ),
    CHECK (
        typeof(attempt_count) = 'integer'
        AND attempt_count BETWEEN 0 AND {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
        AND typeof(claim_fence) = 'integer'
        AND claim_fence = attempt_count
    ),
    CHECK (
        typeof(created_at) IN ('integer', 'real')
        AND created_at >= 0
        AND created_at <= 1.7976931348623157e308
        AND typeof(last_transition_at) IN ('integer', 'real')
        AND last_transition_at >= created_at
        AND last_transition_at <= 1.7976931348623157e308
    ),
    CHECK (
        (state = 'pending'
         AND attempt_count = 0
         AND current_claim_request_id IS NULL
         AND current_claim_request_fingerprint IS NULL
         AND current_claim_token IS NULL
         AND current_lease_seconds IS NULL
         AND claimed_at IS NULL
         AND lease_expires_at IS NULL
         AND acknowledged_at IS NULL
         AND cancelled_at IS NULL
         AND cancellation_code IS NULL
         AND last_transition_at = created_at)
        OR
        (state = 'claimed'
         AND attempt_count BETWEEN 1 AND {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
         AND current_claim_request_id IS NOT NULL
         AND current_claim_request_fingerprint IS NOT NULL
         AND current_claim_token IS NOT NULL
         AND current_lease_seconds BETWEEN
             {TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS}
             AND {TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS}
         AND claimed_at IS NOT NULL
         AND lease_expires_at > claimed_at
         AND acknowledged_at IS NULL
         AND cancelled_at IS NULL
         AND cancellation_code IS NULL
         AND last_transition_at = claimed_at)
        OR
        (state = 'acknowledged'
         AND attempt_count BETWEEN 1 AND {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
         AND current_claim_request_id IS NOT NULL
         AND current_claim_request_fingerprint IS NOT NULL
         AND current_claim_token IS NOT NULL
         AND current_lease_seconds BETWEEN
             {TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS}
             AND {TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS}
         AND claimed_at IS NOT NULL
         AND lease_expires_at IS NULL
         AND acknowledged_at >= claimed_at
         AND cancelled_at IS NULL
         AND cancellation_code IS NULL
         AND last_transition_at = acknowledged_at)
        OR
        (state = 'cancelled'
         AND acknowledged_at IS NULL
         AND lease_expires_at IS NULL
         AND cancelled_at IS NOT NULL
         AND cancellation_code IN (
             'preactivation',
             'conversation_reset',
             'conversation_inactive',
             'delivery_attempts_exhausted'
         )
         AND last_transition_at = cancelled_at
         AND (
             (attempt_count = 0
              AND current_claim_request_id IS NULL
              AND current_claim_request_fingerprint IS NULL
              AND current_claim_token IS NULL
              AND current_lease_seconds IS NULL
              AND claimed_at IS NULL)
             OR
             (attempt_count BETWEEN 1
                                AND {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
              AND current_claim_request_id IS NOT NULL
              AND current_claim_request_fingerprint IS NOT NULL
              AND current_claim_token IS NOT NULL
              AND current_lease_seconds BETWEEN
                  {TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS}
                  AND {TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS}
              AND claimed_at IS NOT NULL
              AND cancelled_at >= claimed_at)
         ))
    )
)
'''


TRUSTED_RESULT_TTS_CLAIMS_TABLE_SQL = f'''
CREATE TABLE trusted_result_tts_claims (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    claim_request_id TEXT NOT NULL PRIMARY KEY,
    claim_request_fingerprint TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    speech_session_id TEXT NOT NULL,
    claim_fence INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    claim_token TEXT NOT NULL UNIQUE,
    lease_seconds INTEGER NOT NULL,
    claimed_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL,
    FOREIGN KEY (event_id)
        REFERENCES trusted_result_tts_outbox (event_id)
        ON DELETE CASCADE,
    UNIQUE (event_id, claim_fence),
    CHECK (
        length(claim_request_fingerprint) = 64
        AND claim_request_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(claim_fence) = 'integer'
        AND claim_fence BETWEEN 1 AND {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
        AND typeof(attempt_number) = 'integer'
        AND attempt_number = claim_fence
    ),
    CHECK (
        typeof(lease_seconds) = 'integer'
        AND lease_seconds BETWEEN {TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS}
                              AND {TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS}
    ),
    CHECK (
        typeof(claimed_at) IN ('integer', 'real')
        AND claimed_at >= 0
        AND claimed_at <= 1.7976931348623157e308
        AND typeof(lease_expires_at) IN ('integer', 'real')
        AND lease_expires_at > claimed_at
        AND lease_expires_at = claimed_at + lease_seconds
        AND lease_expires_at <= 1.7976931348623157e308
    )
)
'''


TRUSTED_RESULT_TTS_ACKS_TABLE_SQL = f'''
CREATE TABLE trusted_result_tts_acknowledgements (
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    acknowledgement_id TEXT NOT NULL PRIMARY KEY,
    acknowledgement_fingerprint TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL UNIQUE,
    claim_request_id TEXT NOT NULL UNIQUE,
    claim_request_fingerprint TEXT NOT NULL,
    claim_fence INTEGER NOT NULL,
    claim_token_digest TEXT NOT NULL,
    acknowledged_at REAL NOT NULL,
    result_code TEXT NOT NULL CHECK (
        result_code = 'tts_delivery_acknowledged'
    ),
    simulation INTEGER NOT NULL DEFAULT 1 CHECK (simulation = 1),
    physical_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (physical_authorized = 0),
    physical_effects INTEGER NOT NULL DEFAULT 0
        CHECK (physical_effects = 0),
    execution_authorized INTEGER NOT NULL DEFAULT 0
        CHECK (execution_authorized = 0),
    physical_audio_verified INTEGER NOT NULL DEFAULT 0
        CHECK (physical_audio_verified = 0),
    FOREIGN KEY (event_id)
        REFERENCES trusted_result_tts_outbox (event_id)
        ON DELETE CASCADE,
    CHECK (
        length(acknowledgement_fingerprint) = 64
        AND acknowledgement_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(claim_request_fingerprint) = 64
        AND claim_request_fingerprint NOT GLOB '*[^0-9a-f]*'
        AND length(claim_token_digest) = 64
        AND claim_token_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (
        typeof(claim_fence) = 'integer'
        AND claim_fence BETWEEN 1 AND {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
    ),
    CHECK (
        typeof(acknowledged_at) IN ('integer', 'real')
        AND acknowledged_at >= 0
        AND acknowledged_at <= 1.7976931348623157e308
    )
)
'''


TRUSTED_RESULT_TTS_OWNER_INDEX_SQL = '''
CREATE INDEX trusted_result_tts_owner_idx
ON trusted_result_tts_outbox (
    user_id,
    conversation_id,
    session_instance_id,
    generation,
    source_ordinal,
    event_id
)
'''


TRUSTED_RESULT_TTS_ONE_CLAIMED_INDEX_SQL = '''
CREATE UNIQUE INDEX trusted_result_tts_one_claimed_conversation_idx
ON trusted_result_tts_outbox (user_id, conversation_id)
WHERE state = 'claimed'
'''


TRUSTED_RESULT_TTS_INSERT_GUARD_SQL = '''
CREATE TRIGGER trusted_result_tts_insert_guard
BEFORE INSERT ON trusted_result_tts_outbox
WHEN NOT EXISTS (
    SELECT 1
    FROM conversation_trusted_tool_results AS result
    JOIN confirmation_intents AS confirmation
      ON confirmation.confirmation_request_id =
         result.confirmation_request_id
    JOIN conversation_sessions AS session
      ON session.user_id = result.user_id
     AND session.conversation_id = result.conversation_id
    WHERE result.trusted_result_id = NEW.trusted_result_id
      AND result.trusted_result_fingerprint =
          NEW.trusted_result_fingerprint
      AND result.confirmation_request_id =
          NEW.confirmation_request_id
      AND result.user_id = NEW.user_id
      AND result.conversation_id = NEW.conversation_id
      AND result.session_instance_id = NEW.session_instance_id
      AND result.generation = NEW.generation
      AND result.source_ordinal = NEW.source_ordinal
      AND result.result_code = NEW.result_code
      AND confirmation.speech_session_id = NEW.speech_session_id
      AND session.session_instance_id = NEW.session_instance_id
      AND (
          (NEW.state = 'pending'
           AND session.status = 'active'
           AND session.generation = NEW.generation
           AND NOT EXISTS (
               SELECT 1
               FROM trusted_result_tts_preactivation_sources AS old_source
               WHERE old_source.trusted_result_id =
                     NEW.trusted_result_id
           ))
          OR
          (NEW.state = 'cancelled'
           AND NEW.cancellation_code = 'preactivation'
           AND EXISTS (
               SELECT 1
               FROM trusted_result_tts_preactivation_sources AS old_source
               WHERE old_source.trusted_result_id =
                     NEW.trusted_result_id
                 AND old_source.trusted_result_fingerprint =
                     NEW.trusted_result_fingerprint
           ))
      )
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS source is invalid');
END
'''


TRUSTED_RESULT_TTS_IDENTITY_NO_UPDATE_SQL = '''
CREATE TRIGGER trusted_result_tts_identity_no_update
BEFORE UPDATE OF
    schema_version,
    event_id,
    event_fingerprint,
    trusted_result_id,
    trusted_result_fingerprint,
    confirmation_request_id,
    user_id,
    conversation_id,
    speech_session_id,
    session_instance_id,
    generation,
    source_ordinal,
    result_code,
    template_key,
    template_digest,
    created_at,
    simulation,
    physical_authorized,
    physical_effects,
    execution_authorized,
    physical_audio_verified
ON trusted_result_tts_outbox
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS identity is immutable');
END
'''


TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL = f'''
CREATE TRIGGER trusted_result_tts_transition_guard
BEFORE UPDATE ON trusted_result_tts_outbox
WHEN NOT (
    (
        NEW.state = 'claimed'
        AND OLD.state IN ('pending', 'claimed')
        AND NEW.attempt_count = OLD.attempt_count + 1
        AND NEW.claim_fence = OLD.claim_fence + 1
        AND NEW.attempt_count <= {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
        AND NEW.current_claim_request_id IS NOT NULL
        AND NEW.current_claim_request_fingerprint IS NOT NULL
        AND NEW.current_claim_token IS NOT NULL
        AND NEW.current_lease_seconds BETWEEN
            {TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS}
            AND {TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS}
        AND NEW.claimed_at >= OLD.last_transition_at
        AND NEW.lease_expires_at =
            NEW.claimed_at + NEW.current_lease_seconds
        AND NEW.last_transition_at = NEW.claimed_at
        AND NEW.acknowledged_at IS NULL
        AND NEW.cancelled_at IS NULL
        AND NEW.cancellation_code IS NULL
        AND (
            OLD.state = 'pending'
            OR NEW.claimed_at >= OLD.lease_expires_at
        )
        AND EXISTS (
            SELECT 1 FROM trusted_result_tts_claims AS claim
            WHERE claim.claim_request_id =
                  NEW.current_claim_request_id
              AND claim.claim_request_fingerprint =
                  NEW.current_claim_request_fingerprint
              AND claim.event_id = NEW.event_id
              AND claim.user_id = NEW.user_id
              AND claim.conversation_id = NEW.conversation_id
              AND claim.speech_session_id = NEW.speech_session_id
              AND claim.claim_fence = NEW.claim_fence
              AND claim.attempt_number = NEW.attempt_count
              AND claim.claim_token = NEW.current_claim_token
              AND claim.lease_seconds = NEW.current_lease_seconds
              AND claim.claimed_at = NEW.claimed_at
              AND claim.lease_expires_at = NEW.lease_expires_at
        )
    )
    OR
    (
        OLD.state = 'claimed'
        AND NEW.state = 'acknowledged'
        AND NEW.attempt_count = OLD.attempt_count
        AND NEW.claim_fence = OLD.claim_fence
        AND NEW.current_claim_request_id =
            OLD.current_claim_request_id
        AND NEW.current_claim_request_fingerprint =
            OLD.current_claim_request_fingerprint
        AND NEW.current_claim_token = OLD.current_claim_token
        AND NEW.current_lease_seconds = OLD.current_lease_seconds
        AND NEW.claimed_at = OLD.claimed_at
        AND NEW.lease_expires_at IS NULL
        AND NEW.acknowledged_at >= OLD.claimed_at
        AND NEW.acknowledged_at < OLD.lease_expires_at
        AND NEW.last_transition_at = NEW.acknowledged_at
        AND NEW.cancelled_at IS NULL
        AND NEW.cancellation_code IS NULL
        AND EXISTS (
            SELECT 1
            FROM trusted_result_tts_acknowledgements AS ack
            WHERE ack.event_id = NEW.event_id
              AND ack.claim_request_id =
                  NEW.current_claim_request_id
              AND ack.claim_request_fingerprint =
                  NEW.current_claim_request_fingerprint
              AND ack.claim_fence = NEW.claim_fence
              AND ack.acknowledged_at = NEW.acknowledged_at
              AND ack.result_code = 'tts_delivery_acknowledged'
        )
    )
    OR
    (
        OLD.state IN ('pending', 'claimed')
        AND NEW.state = 'cancelled'
        AND NEW.attempt_count = OLD.attempt_count
        AND NEW.claim_fence = OLD.claim_fence
        AND NEW.current_claim_request_id IS
            OLD.current_claim_request_id
        AND NEW.current_claim_request_fingerprint IS
            OLD.current_claim_request_fingerprint
        AND NEW.current_claim_token IS OLD.current_claim_token
        AND NEW.current_lease_seconds IS OLD.current_lease_seconds
        AND NEW.claimed_at IS OLD.claimed_at
        AND NEW.lease_expires_at IS NULL
        AND NEW.acknowledged_at IS NULL
        AND NEW.cancelled_at >= OLD.last_transition_at
        AND NEW.last_transition_at = NEW.cancelled_at
        AND NEW.cancellation_code IN (
            'conversation_reset',
            'conversation_inactive',
            'delivery_attempts_exhausted'
        )
        AND (
            NEW.cancellation_code != 'delivery_attempts_exhausted'
            OR (
                OLD.state = 'claimed'
                AND OLD.attempt_count =
                    {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
                AND NEW.cancelled_at >= OLD.lease_expires_at
            )
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS transition is invalid');
END
'''


TRUSTED_RESULT_TTS_NO_REPLACE_SQL = '''
CREATE TRIGGER trusted_result_tts_no_replace
BEFORE INSERT ON trusted_result_tts_outbox
WHEN EXISTS (
    SELECT 1 FROM trusted_result_tts_outbox
    WHERE event_id = NEW.event_id
       OR event_fingerprint = NEW.event_fingerprint
       OR trusted_result_id = NEW.trusted_result_id
       OR confirmation_request_id = NEW.confirmation_request_id
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS identity is immutable');
END
'''


TRUSTED_RESULT_TTS_CLAIM_INSERT_GUARD_SQL = f'''
CREATE TRIGGER trusted_result_tts_claim_insert_guard
BEFORE INSERT ON trusted_result_tts_claims
WHEN NOT EXISTS (
    SELECT 1 FROM trusted_result_tts_outbox AS event
    WHERE event.event_id = NEW.event_id
      AND event.user_id = NEW.user_id
      AND event.conversation_id = NEW.conversation_id
      AND event.speech_session_id = NEW.speech_session_id
      AND event.state IN ('pending', 'claimed')
      AND NEW.claim_fence = event.claim_fence + 1
      AND NEW.attempt_number = event.attempt_count + 1
      AND NEW.attempt_number <= {TRUSTED_RESULT_TTS_MAX_ATTEMPTS}
      AND NEW.claimed_at >= event.last_transition_at
      AND (
          event.state = 'pending'
          OR NEW.claimed_at >= event.lease_expires_at
      )
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS claim is invalid');
END
'''


TRUSTED_RESULT_TTS_CLAIM_NO_UPDATE_SQL = '''
CREATE TRIGGER trusted_result_tts_claim_no_update
BEFORE UPDATE ON trusted_result_tts_claims
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS claim is immutable');
END
'''


TRUSTED_RESULT_TTS_CLAIM_NO_REPLACE_SQL = '''
CREATE TRIGGER trusted_result_tts_claim_no_replace
BEFORE INSERT ON trusted_result_tts_claims
WHEN EXISTS (
    SELECT 1 FROM trusted_result_tts_claims
    WHERE claim_request_id = NEW.claim_request_id
       OR claim_request_fingerprint = NEW.claim_request_fingerprint
       OR claim_token = NEW.claim_token
       OR (event_id = NEW.event_id
           AND claim_fence = NEW.claim_fence)
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS claim identity is immutable');
END
'''


TRUSTED_RESULT_TTS_CLAIM_NO_DELETE_SQL = '''
CREATE TRIGGER trusted_result_tts_claim_no_delete
BEFORE DELETE ON trusted_result_tts_claims
WHEN EXISTS (
    SELECT 1 FROM trusted_result_tts_outbox
    WHERE event_id = OLD.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS claim is immutable');
END
'''


TRUSTED_RESULT_TTS_ACK_INSERT_GUARD_SQL = '''
CREATE TRIGGER trusted_result_tts_ack_insert_guard
BEFORE INSERT ON trusted_result_tts_acknowledgements
WHEN NOT EXISTS (
    SELECT 1
    FROM trusted_result_tts_outbox AS event
    JOIN trusted_result_tts_claims AS claim
      ON claim.event_id = event.event_id
     AND claim.claim_request_id = event.current_claim_request_id
     AND claim.claim_fence = event.claim_fence
    WHERE event.event_id = NEW.event_id
      AND event.state = 'claimed'
      AND event.current_claim_request_id = NEW.claim_request_id
      AND event.current_claim_request_fingerprint =
          NEW.claim_request_fingerprint
      AND event.claim_fence = NEW.claim_fence
      AND claim.claim_request_fingerprint =
          NEW.claim_request_fingerprint
      AND NEW.acknowledged_at >= claim.claimed_at
      AND NEW.acknowledged_at < claim.lease_expires_at
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS ACK source is invalid');
END
'''


TRUSTED_RESULT_TTS_ACK_NO_UPDATE_SQL = '''
CREATE TRIGGER trusted_result_tts_ack_no_update
BEFORE UPDATE ON trusted_result_tts_acknowledgements
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS ACK is immutable');
END
'''


TRUSTED_RESULT_TTS_ACK_NO_REPLACE_SQL = '''
CREATE TRIGGER trusted_result_tts_ack_no_replace
BEFORE INSERT ON trusted_result_tts_acknowledgements
WHEN EXISTS (
    SELECT 1 FROM trusted_result_tts_acknowledgements
    WHERE acknowledgement_id = NEW.acknowledgement_id
       OR acknowledgement_fingerprint =
          NEW.acknowledgement_fingerprint
       OR event_id = NEW.event_id
       OR claim_request_id = NEW.claim_request_id
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS ACK identity is immutable');
END
'''


TRUSTED_RESULT_TTS_ACK_NO_DELETE_SQL = '''
CREATE TRIGGER trusted_result_tts_ack_no_delete
BEFORE DELETE ON trusted_result_tts_acknowledgements
WHEN EXISTS (
    SELECT 1 FROM trusted_result_tts_outbox
    WHERE event_id = OLD.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS ACK is immutable');
END
'''


TRUSTED_RESULT_TTS_PREACTIVATION_NO_UPDATE_SQL = '''
CREATE TRIGGER trusted_result_tts_preactivation_no_update
BEFORE UPDATE ON trusted_result_tts_preactivation_sources
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS activation is immutable');
END
'''


TRUSTED_RESULT_TTS_PREACTIVATION_NO_DELETE_SQL = '''
CREATE TRIGGER trusted_result_tts_preactivation_no_delete
BEFORE DELETE ON trusted_result_tts_preactivation_sources
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS activation is immutable');
END
'''


TRUSTED_RESULT_TTS_PREACTIVATION_NO_INSERT_SQL = '''
CREATE TRIGGER trusted_result_tts_preactivation_no_insert
BEFORE INSERT ON trusted_result_tts_preactivation_sources
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS activation is immutable');
END
'''


TRUSTED_RESULT_TTS_METADATA_NO_UPDATE_SQL = '''
CREATE TRIGGER trusted_result_tts_metadata_no_update
BEFORE UPDATE ON trusted_result_tts_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS metadata is immutable');
END
'''


TRUSTED_RESULT_TTS_METADATA_NO_DELETE_SQL = '''
CREATE TRIGGER trusted_result_tts_metadata_no_delete
BEFORE DELETE ON trusted_result_tts_schema_metadata
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS metadata is immutable');
END
'''


TRUSTED_RESULT_TTS_METADATA_NO_REPLACE_SQL = '''
CREATE TRIGGER trusted_result_tts_metadata_no_replace
BEFORE INSERT ON trusted_result_tts_schema_metadata
WHEN EXISTS (
    SELECT 1 FROM trusted_result_tts_schema_metadata
    WHERE singleton = NEW.singleton
)
BEGIN
    SELECT RAISE(ABORT, 'trusted result TTS metadata is immutable');
END
'''


class TrustedResultTTSError(RuntimeError):
    """Raised when the durable TTS contract cannot be trusted."""


class TrustedResultTTSConflictError(TrustedResultTTSError):
    """Raised for a stale, mutated, or competing claim/ACK."""


@dataclass(frozen=True)
class TrustedResultTTSEvent:
    """Content-minimized state for one stable feedback request."""

    event_id: str
    user_id: str = field(repr=False)
    conversation_id: str = field(repr=False)
    speech_session_id: str = field(repr=False)
    session_instance_id: str = field(repr=False)
    generation: int = field(repr=False)
    result_code: str
    template_key: str
    template_digest: str = field(repr=False)
    state: str
    attempt_count: int
    claim_fence: int
    created_at: float
    last_transition_at: float
    acknowledged_at: Optional[float] = None
    cancelled_at: Optional[float] = None
    cancellation_code: Optional[str] = None
    schema_version: int = TRUSTED_RESULT_TTS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject states outside the closed outbox projection."""
        _validate_public_identity(
            self.event_id,
            self.user_id,
            self.conversation_id,
            self.speech_session_id,
            self.session_instance_id,
            self.generation,
        )
        template_key, _message = _template(self.result_code)
        if (
            type(self.schema_version) is not int
            or self.schema_version != TRUSTED_RESULT_TTS_SCHEMA_VERSION
            or self.template_key != template_key
            or self.template_digest != _template_digest(self.result_code)
            or self.state not in {
                'pending', 'claimed', 'acknowledged', 'cancelled'
            }
            or type(self.attempt_count) is not int
            or not 0 <= self.attempt_count <= TRUSTED_RESULT_TTS_MAX_ATTEMPTS
            or type(self.claim_fence) is not int
            or self.claim_fence != self.attempt_count
            or (
                self.state == 'pending'
                and self.attempt_count != 0
            )
            or (
                self.state in {'claimed', 'acknowledged'}
                and self.attempt_count < 1
            )
        ):
            raise ValidationError('trusted result TTS event is invalid')
        for value in (self.created_at, self.last_transition_at):
            _timestamp(value, 'trusted result TTS event timestamp')
        if self.last_transition_at < self.created_at:
            raise ValidationError('trusted result TTS event is invalid')
        if self.state == 'acknowledged':
            if (
                self.acknowledged_at is None
                or _timestamp(self.acknowledged_at, 'acknowledged_at')
                != self.last_transition_at
                or self.cancelled_at is not None
                or self.cancellation_code is not None
            ):
                raise ValidationError('trusted result TTS event is invalid')
        elif self.state == 'cancelled':
            if (
                self.cancelled_at is None
                or _timestamp(self.cancelled_at, 'cancelled_at')
                != self.last_transition_at
                or self.cancellation_code not in {
                    'preactivation',
                    'conversation_reset',
                    'conversation_inactive',
                    'delivery_attempts_exhausted',
                }
                or self.acknowledged_at is not None
            ):
                raise ValidationError('trusted result TTS event is invalid')
        elif (
            self.acknowledged_at is not None
            or self.cancelled_at is not None
            or self.cancellation_code is not None
        ):
            raise ValidationError('trusted result TTS event is invalid')

    @property
    def message(self) -> str:
        """Render the immutable Korean template selected by result code."""
        return _template(self.result_code)[1]

    @property
    def tts_request_id(self) -> str:
        """Return the stable downstream idempotency key."""
        return self.event_id

    def to_public_dict(self) -> Dict[str, Any]:
        """Return the identifier-free, explicitly non-authorizing view."""
        return {
            'schema_version': self.schema_version,
            'tts_request_id': self.tts_request_id,
            'result_code': self.result_code,
            'template_key': self.template_key,
            'message': self.message,
            'state': self.state,
            'attempt_count': self.attempt_count,
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'execution_authorized': False,
            'physical_audio_verified': False,
        }


@dataclass(frozen=True)
class TrustedResultTTSClaim:
    """One leased non-authorizing request for a downstream TTS adapter."""

    event_id: str
    claim_request_id: str = field(repr=False)
    claim_token: str = field(repr=False)
    claim_fence: int
    attempt_number: int
    result_code: str
    template_key: str
    template_digest: str = field(repr=False)
    speech_session_id: str = field(repr=False)
    claimed_at: float
    lease_expires_at: float
    schema_version: int = TRUSTED_RESULT_TTS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject forged payloads and malformed lease credentials."""
        template_key, _message = _template(self.result_code)
        if (
            type(self.schema_version) is not int
            or self.schema_version != TRUSTED_RESULT_TTS_SCHEMA_VERSION
            or not _SAFE_IDENTIFIER.fullmatch(self.event_id)
            or not _identifier(self.claim_request_id, 'claim_request_id')
            or not _CLAIM_TOKEN.fullmatch(self.claim_token)
            or type(self.claim_fence) is not int
            or not 1 <= self.claim_fence <= TRUSTED_RESULT_TTS_MAX_ATTEMPTS
            or type(self.attempt_number) is not int
            or self.attempt_number != self.claim_fence
            or self.template_key != template_key
            or self.template_digest != _template_digest(self.result_code)
            or _identifier(
                self.speech_session_id, 'speech_session_id'
            ) != self.speech_session_id
        ):
            raise ValidationError('trusted result TTS claim is invalid')
        claimed = _timestamp(self.claimed_at, 'claimed_at')
        expires = _timestamp(self.lease_expires_at, 'lease_expires_at')
        if expires <= claimed:
            raise ValidationError('trusted result TTS claim is invalid')

    @property
    def message(self) -> str:
        """Render the version-bound Korean template without stored text."""
        return _template(self.result_code)[1]

    @property
    def tts_request_id(self) -> str:
        """Return the stable request id across every delivery attempt."""
        return self.event_id

    @property
    def physical_authorized(self) -> bool:
        """Never grant physical authority from a feedback claim."""
        return False

    @property
    def physical_effects(self) -> bool:
        """Report that the simulation produced no physical effects."""
        return False

    @property
    def physical_audio_verified(self) -> bool:
        """Never treat a downstream ACK as proof audio was heard."""
        return False

    @property
    def execution_authorized(self) -> bool:
        """Never authorize execution from an outbox lease."""
        return False

    def to_public_dict(self) -> Dict[str, Any]:
        """Return the adapter payload without private source identifiers."""
        return {
            'schema_version': self.schema_version,
            'tts_request_id': self.tts_request_id,
            'claim_fence': self.claim_fence,
            'attempt_number': self.attempt_number,
            'result_code': self.result_code,
            'template_key': self.template_key,
            'message': self.message,
            'claimed_at': self.claimed_at,
            'lease_expires_at': self.lease_expires_at,
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'execution_authorized': False,
            'physical_audio_verified': False,
        }


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f'{name} is invalid')
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(character) < 32 or ord(character) == 127
               for character in normalized)
    ):
        raise ValidationError(f'{name} is invalid')
    return normalized


def _timestamp(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValidationError(f'{name} is invalid')
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValidationError(f'{name} is invalid') from None
    if not math.isfinite(normalized) or normalized < 0:
        raise ValidationError(f'{name} is invalid')
    return 0.0 if normalized == 0 else normalized


def _validate_public_identity(
    event_id: str,
    user_id: str,
    conversation_id: str,
    speech_session_id: str,
    session_instance_id: str,
    generation: int,
) -> None:
    try:
        normalized_user = validate_user_id(user_id)
        normalized_conversation = validate_conversation_id(conversation_id)
    except ValidationError:
        raise ValidationError(
            'trusted result TTS identity is invalid'
        ) from None
    if (
        not isinstance(event_id, str)
        or not event_id.startswith('trusted-result-tts-')
        or not _SAFE_IDENTIFIER.fullmatch(event_id)
        or normalized_user != user_id
        or normalized_conversation != conversation_id
        or _identifier(speech_session_id, 'speech_session_id')
        != speech_session_id
        or _identifier(session_instance_id, 'session_instance_id')
        != session_instance_id
        or type(generation) is not int
        or generation < 1
    ):
        raise ValidationError('trusted result TTS identity is invalid')


def _template(result_code: str) -> Tuple[str, str]:
    try:
        return _TEMPLATES[result_code]
    except (KeyError, TypeError):
        raise TrustedResultTTSError(
            'trusted result TTS code is unsupported'
        ) from None


def _template_digest(result_code: str) -> str:
    return hashlib.sha256(
        _template(result_code)[1].encode('utf-8')
    ).hexdigest()


def _canonical_hash(value: Dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _activation_anchor_value(preactivation_digest: str) -> int:
    """Bind immutable metadata into an external positive SQLite integer."""
    if not isinstance(preactivation_digest, str) or not _HEX_DIGEST.fullmatch(
        preactivation_digest
    ):
        raise TrustedResultTTSError(
            'trusted result TTS activation digest is invalid'
        )
    return int(preactivation_digest[:15], 16) + 1


def _event_id(trusted_result_id: str) -> str:
    digest = hashlib.sha256(
        b'tool-result-tts-v1\0' + trusted_result_id.encode('utf-8')
    ).hexdigest()
    return f'trusted-result-tts-{digest}'


def _event_values(
    trusted_result: TrustedToolResult,
    source: sqlite3.Row,
    *,
    created_at: Optional[float] = None,
) -> Dict[str, Any]:
    template_key, _message = _template(trusted_result.result_code)
    values: Dict[str, Any] = {
        'trusted_result_id': trusted_result.trusted_result_id,
        'trusted_result_fingerprint': (
            trusted_result.trusted_result_fingerprint
        ),
        'confirmation_request_id': source['confirmation_request_id'],
        'user_id': trusted_result.user_id,
        'conversation_id': trusted_result.conversation_id,
        'speech_session_id': source['speech_session_id'],
        'session_instance_id': trusted_result.session_instance_id,
        'generation': trusted_result.generation,
        'source_ordinal': trusted_result.source_ordinal,
        'result_code': trusted_result.result_code,
        'template_key': template_key,
        'template_digest': _template_digest(trusted_result.result_code),
        'created_at': _timestamp(
            trusted_result.completed_at
            if created_at is None else created_at,
            'created_at',
        ),
    }
    values['event_id'] = _event_id(trusted_result.trusted_result_id)
    values['event_fingerprint'] = _canonical_hash(
        {
            'schema_version': TRUSTED_RESULT_TTS_SCHEMA_VERSION,
            'domain': 'tool-result-tts-v1',
            'event_id': values['event_id'],
            'trusted_result_id': values['trusted_result_id'],
            'trusted_result_fingerprint': (
                values['trusted_result_fingerprint']
            ),
            'confirmation_request_id': values['confirmation_request_id'],
            'user_id': values['user_id'],
            'conversation_id': values['conversation_id'],
            'speech_session_id': values['speech_session_id'],
            'session_instance_id': values['session_instance_id'],
            'generation': values['generation'],
            'source_ordinal': values['source_ordinal'],
            'result_code': values['result_code'],
            'template_version': TRUSTED_RESULT_TTS_TEMPLATE_VERSION,
            'template_key': values['template_key'],
            'template_digest': values['template_digest'],
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'execution_authorized': False,
            'physical_audio_verified': False,
        }
    )
    return values


def _source_for_trusted_result(
    connection: sqlite3.Connection,
    trusted_result: TrustedToolResult,
) -> Tuple[sqlite3.Row, TrustedToolResult]:
    if not isinstance(trusted_result, TrustedToolResult):
        raise TypeError('trusted_result must be a TrustedToolResult')
    row = connection.execute(
        '''
        SELECT result.*, confirmation.speech_session_id
        FROM conversation_trusted_tool_results AS result
        JOIN confirmation_intents AS confirmation
          ON confirmation.confirmation_request_id =
             result.confirmation_request_id
        WHERE result.trusted_result_id = ?
        ''',
        (trusted_result.trusted_result_id,),
    ).fetchone()
    if row is None:
        raise TrustedResultTTSError('trusted result TTS source is missing')
    stored = _trusted_result_from_row(connection, row)
    if stored != trusted_result:
        raise TrustedResultTTSError('trusted result TTS source changed')
    _identifier(row['speech_session_id'], 'speech_session_id')
    return row, stored


def _expected_objects() -> Dict[str, Tuple[str, str]]:
    return {
        'trusted_result_tts_schema_metadata': (
            'table', TRUSTED_RESULT_TTS_METADATA_TABLE_SQL
        ),
        'trusted_result_tts_preactivation_sources': (
            'table', TRUSTED_RESULT_TTS_PREACTIVATION_TABLE_SQL
        ),
        'trusted_result_tts_outbox': (
            'table', TRUSTED_RESULT_TTS_OUTBOX_TABLE_SQL
        ),
        'trusted_result_tts_claims': (
            'table', TRUSTED_RESULT_TTS_CLAIMS_TABLE_SQL
        ),
        'trusted_result_tts_acknowledgements': (
            'table', TRUSTED_RESULT_TTS_ACKS_TABLE_SQL
        ),
        'trusted_result_tts_owner_idx': (
            'index', TRUSTED_RESULT_TTS_OWNER_INDEX_SQL
        ),
        'trusted_result_tts_one_claimed_conversation_idx': (
            'index', TRUSTED_RESULT_TTS_ONE_CLAIMED_INDEX_SQL
        ),
        'trusted_result_tts_insert_guard': (
            'trigger', TRUSTED_RESULT_TTS_INSERT_GUARD_SQL
        ),
        'trusted_result_tts_identity_no_update': (
            'trigger', TRUSTED_RESULT_TTS_IDENTITY_NO_UPDATE_SQL
        ),
        'trusted_result_tts_transition_guard': (
            'trigger', TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL
        ),
        'trusted_result_tts_no_replace': (
            'trigger', TRUSTED_RESULT_TTS_NO_REPLACE_SQL
        ),
        'trusted_result_tts_claim_insert_guard': (
            'trigger', TRUSTED_RESULT_TTS_CLAIM_INSERT_GUARD_SQL
        ),
        'trusted_result_tts_claim_no_update': (
            'trigger', TRUSTED_RESULT_TTS_CLAIM_NO_UPDATE_SQL
        ),
        'trusted_result_tts_claim_no_replace': (
            'trigger', TRUSTED_RESULT_TTS_CLAIM_NO_REPLACE_SQL
        ),
        'trusted_result_tts_claim_no_delete': (
            'trigger', TRUSTED_RESULT_TTS_CLAIM_NO_DELETE_SQL
        ),
        'trusted_result_tts_ack_insert_guard': (
            'trigger', TRUSTED_RESULT_TTS_ACK_INSERT_GUARD_SQL
        ),
        'trusted_result_tts_ack_no_update': (
            'trigger', TRUSTED_RESULT_TTS_ACK_NO_UPDATE_SQL
        ),
        'trusted_result_tts_ack_no_replace': (
            'trigger', TRUSTED_RESULT_TTS_ACK_NO_REPLACE_SQL
        ),
        'trusted_result_tts_ack_no_delete': (
            'trigger', TRUSTED_RESULT_TTS_ACK_NO_DELETE_SQL
        ),
        'trusted_result_tts_preactivation_no_update': (
            'trigger', TRUSTED_RESULT_TTS_PREACTIVATION_NO_UPDATE_SQL
        ),
        'trusted_result_tts_preactivation_no_delete': (
            'trigger', TRUSTED_RESULT_TTS_PREACTIVATION_NO_DELETE_SQL
        ),
        'trusted_result_tts_preactivation_no_insert': (
            'trigger', TRUSTED_RESULT_TTS_PREACTIVATION_NO_INSERT_SQL
        ),
        'trusted_result_tts_metadata_no_update': (
            'trigger', TRUSTED_RESULT_TTS_METADATA_NO_UPDATE_SQL
        ),
        'trusted_result_tts_metadata_no_delete': (
            'trigger', TRUSTED_RESULT_TTS_METADATA_NO_DELETE_SQL
        ),
        'trusted_result_tts_metadata_no_replace': (
            'trigger', TRUSTED_RESULT_TTS_METADATA_NO_REPLACE_SQL
        ),
    }


def _insert_event_locked(
    connection: sqlite3.Connection,
    values: Dict[str, Any],
    *,
    preactivation_cancelled_at: Optional[float] = None,
) -> None:
    if preactivation_cancelled_at is None:
        state = 'pending'
        transition_at = values['created_at']
        cancelled_at = None
        cancellation_code = None
    else:
        state = 'cancelled'
        transition_at = max(
            values['created_at'],
            _timestamp(preactivation_cancelled_at, 'cancelled_at'),
        )
        cancelled_at = transition_at
        cancellation_code = 'preactivation'
    connection.execute(
        '''
        INSERT INTO trusted_result_tts_outbox (
            schema_version, event_id, event_fingerprint,
            trusted_result_id, trusted_result_fingerprint,
            confirmation_request_id, user_id, conversation_id,
            speech_session_id, session_instance_id, generation,
            source_ordinal, result_code, template_key,
            template_digest, state, attempt_count, claim_fence,
            current_claim_request_id,
            current_claim_request_fingerprint,
            current_claim_token, current_lease_seconds,
            created_at, last_transition_at, claimed_at,
            lease_expires_at, acknowledged_at, cancelled_at,
            cancellation_code, simulation, physical_authorized,
            physical_effects, execution_authorized,
            physical_audio_verified
        ) VALUES (
            1, :event_id, :event_fingerprint,
            :trusted_result_id, :trusted_result_fingerprint,
            :confirmation_request_id, :user_id, :conversation_id,
            :speech_session_id, :session_instance_id, :generation,
            :source_ordinal, :result_code, :template_key,
            :template_digest, :state, 0, 0,
            NULL, NULL, NULL, NULL,
            :created_at, :last_transition_at, NULL,
            NULL, NULL, :cancelled_at,
            :cancellation_code, 1, 0, 0, 0, 0
        )
        ''',
        {
            **values,
            'state': state,
            'last_transition_at': transition_at,
            'cancelled_at': cancelled_at,
            'cancellation_code': cancellation_code,
        },
    )


def prepare_trusted_result_tts_schema_locked(
    connection: sqlite3.Connection,
    *,
    activated_at: float,
) -> None:
    """Create once or exactly validate the durable feedback schema."""
    if not connection.in_transaction:
        raise TrustedResultTTSError(
            'trusted result TTS schema requires a write transaction'
        )
    normalized_time = _timestamp(activated_at, 'activated_at')
    expected = _expected_objects()
    placeholders = ','.join('?' for _ in expected)
    objects = connection.execute(
        f'''
        SELECT name, type FROM sqlite_master
        WHERE name IN ({placeholders})
        ''',
        tuple(expected),
    ).fetchall()
    sentinel = connection.execute(
        '''
        SELECT proposal_fingerprint
        FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (TRUSTED_RESULT_TTS_ACTIVATION_SENTINEL,),
    ).fetchone()
    created_fresh = False
    if not objects:
        if sentinel is not None:
            raise TrustedResultTTSError(
                'trusted result TTS schema was removed after activation'
            )
        connection.execute(TRUSTED_RESULT_TTS_METADATA_TABLE_SQL)
        created_fresh = True
        connection.execute(TRUSTED_RESULT_TTS_PREACTIVATION_TABLE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_OUTBOX_TABLE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_CLAIMS_TABLE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_ACKS_TABLE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_OWNER_INDEX_SQL)
        connection.execute(TRUSTED_RESULT_TTS_ONE_CLAIMED_INDEX_SQL)
        connection.execute(TRUSTED_RESULT_TTS_INSERT_GUARD_SQL)
        connection.execute(TRUSTED_RESULT_TTS_IDENTITY_NO_UPDATE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_TRANSITION_GUARD_SQL)
        connection.execute(TRUSTED_RESULT_TTS_NO_REPLACE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_CLAIM_INSERT_GUARD_SQL)
        connection.execute(TRUSTED_RESULT_TTS_CLAIM_NO_UPDATE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_CLAIM_NO_REPLACE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_CLAIM_NO_DELETE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_ACK_INSERT_GUARD_SQL)
        connection.execute(TRUSTED_RESULT_TTS_ACK_NO_UPDATE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_ACK_NO_REPLACE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_ACK_NO_DELETE_SQL)
        existing_sources = connection.execute(
            '''
            SELECT result.*, confirmation.speech_session_id
            FROM conversation_trusted_tool_results AS result
            JOIN confirmation_intents AS confirmation
              ON confirmation.confirmation_request_id =
                 result.confirmation_request_id
            ORDER BY result.source_ordinal, result.trusted_result_id
            '''
        ).fetchall()
        for source in existing_sources:
            connection.execute(
                '''
                INSERT INTO trusted_result_tts_preactivation_sources (
                    trusted_result_id, trusted_result_fingerprint
                ) VALUES (?, ?)
                ''',
                (
                    source['trusted_result_id'],
                    source['trusted_result_fingerprint'],
                ),
            )
            trusted_result = _trusted_result_from_row(connection, source)
            _insert_event_locked(
                connection,
                _event_values(trusted_result, source),
                preactivation_cancelled_at=normalized_time,
            )
        connection.execute(TRUSTED_RESULT_TTS_PREACTIVATION_NO_UPDATE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_PREACTIVATION_NO_DELETE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_PREACTIVATION_NO_INSERT_SQL)
        connection.execute(TRUSTED_RESULT_TTS_METADATA_NO_UPDATE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_METADATA_NO_DELETE_SQL)
        connection.execute(TRUSTED_RESULT_TTS_METADATA_NO_REPLACE_SQL)
        activation_epoch = secrets.token_hex(32)
        preactivation_sources = [
            [row['trusted_result_id'], row['trusted_result_fingerprint']]
            for row in sorted(
                existing_sources,
                key=lambda item: item['trusted_result_id'],
            )
        ]
        connection.execute(
            '''
            INSERT INTO trusted_result_tts_schema_metadata (
                singleton, schema_version, activated_at,
                activation_epoch, preactivation_count,
                preactivation_digest
            ) VALUES (1, 1, ?, ?, ?, ?)
            ''',
            (
                normalized_time,
                activation_epoch,
                len(existing_sources),
                _canonical_hash(
                    {
                        'schema_version': 1,
                        'activated_at': normalized_time,
                        'activation_epoch': activation_epoch,
                        'sources': preactivation_sources,
                    }
                ),
            ),
        )
    elif {str(row['name']) for row in objects} != set(expected):
        raise TrustedResultTTSError(
            'trusted result TTS schema is incomplete'
        )
    if sentinel is None:
        if not created_fresh:
            empty_counts = (
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'trusted_result_tts_preactivation_sources'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM trusted_result_tts_outbox'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM trusted_result_tts_claims'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'trusted_result_tts_acknowledgements'
                ).fetchone()[0],
                connection.execute(
                    'SELECT COUNT(*) FROM '
                    'conversation_trusted_tool_results'
                ).fetchone()[0],
                connection.execute(
                    '''
                    SELECT COUNT(*) FROM monitor_room_simulation_ledger
                    WHERE schema_version = 4
                      AND record_kind IN ('planned', 'planning_failed')
                    '''
                ).fetchone()[0],
            )
            existing_metadata = connection.execute(
                '''
                SELECT preactivation_count
                FROM trusted_result_tts_schema_metadata
                WHERE singleton = 1
                '''
            ).fetchone()
            if (
                existing_metadata is None
                or existing_metadata['preactivation_count'] != 0
                or any(int(count) != 0 for count in empty_counts)
            ):
                raise TrustedResultTTSError(
                    'trusted result TTS activation anchor is missing'
                )
        simulation = connection.execute(
            '''
            SELECT activation_epoch, activated_at
            FROM monitor_room_simulation_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        metadata = connection.execute(
            '''
            SELECT preactivation_count, preactivation_digest
            FROM trusted_result_tts_schema_metadata
            WHERE singleton = 1
            '''
        ).fetchone()
        if simulation is None or metadata is None:
            raise TrustedResultTTSError(
                'trusted result TTS activation source is missing'
            )
        connection.execute(
            'DROP TRIGGER monitor_room_simulation_preactivation_no_insert'
        )
        connection.execute(
            '''
            INSERT INTO monitor_room_simulation_preactivation_proposals (
                proposal_fingerprint, activation_epoch,
                snapshot_rowid, snapshotted_at
            ) VALUES (?, ?, ?, ?)
            ''',
            (
                TRUSTED_RESULT_TTS_ACTIVATION_SENTINEL,
                simulation['activation_epoch'],
                _activation_anchor_value(
                    metadata['preactivation_digest']
                ),
                simulation['activated_at'],
            ),
        )
        connection.execute(SIMULATION_PREACTIVATION_NO_INSERT_TRIGGER_SQL)
    validate_trusted_result_tts_schema_locked(connection)


def _event_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    allow_stale: bool = False,
) -> TrustedResultTTSEvent:
    source = connection.execute(
        '''
        SELECT result.*, confirmation.speech_session_id
        FROM conversation_trusted_tool_results AS result
        JOIN confirmation_intents AS confirmation
          ON confirmation.confirmation_request_id =
             result.confirmation_request_id
        WHERE result.trusted_result_id = ?
        ''',
        (row['trusted_result_id'],),
    ).fetchone()
    if source is None:
        raise TrustedResultTTSError('trusted result TTS source is missing')
    trusted_result = _trusted_result_from_row(connection, source)
    expected = _event_values(trusted_result, source)
    exact_fields = (
        'event_id',
        'event_fingerprint',
        'trusted_result_id',
        'trusted_result_fingerprint',
        'confirmation_request_id',
        'user_id',
        'conversation_id',
        'speech_session_id',
        'session_instance_id',
        'generation',
        'source_ordinal',
        'result_code',
        'template_key',
        'template_digest',
        'created_at',
    )
    if (
        any(row[name] != expected[name] for name in exact_fields)
        or any(
            type(row[name]) is not int or row[name] != expected_value
            for name, expected_value in (
                ('schema_version', 1),
                ('simulation', 1),
                ('physical_authorized', 0),
                ('physical_effects', 0),
                ('execution_authorized', 0),
                ('physical_audio_verified', 0),
            )
        )
        or type(row['generation']) is not int
        or type(row['source_ordinal']) is not int
        or type(row['attempt_count']) is not int
        or type(row['claim_fence']) is not int
    ):
        raise TrustedResultTTSError(
            'trusted result TTS event is incompatible'
        )
    session = connection.execute(
        '''
        SELECT session_instance_id, generation, status
        FROM conversation_sessions
        WHERE user_id = ? AND conversation_id = ?
        ''',
        (row['user_id'], row['conversation_id']),
    ).fetchone()
    if session is None:
        raise TrustedResultTTSError(
            'trusted result TTS owner is missing'
        )
    if not allow_stale and row['state'] in {'pending', 'claimed'} and (
        session['session_instance_id'] != row['session_instance_id']
        or session['generation'] != row['generation']
        or session['status'] != 'active'
    ):
        raise TrustedResultTTSError(
            'trusted result TTS event survived its lifecycle'
        )
    preactivation = connection.execute(
        '''
        SELECT 1 FROM trusted_result_tts_preactivation_sources
        WHERE trusted_result_id = ?
        ''',
        (row['trusted_result_id'],),
    ).fetchone()
    if (preactivation is not None) != (
        row['state'] == 'cancelled'
        and row['cancellation_code'] == 'preactivation'
    ):
        raise TrustedResultTTSError(
            'trusted result TTS activation state is incompatible'
        )
    _validate_current_claim_binding_locked(connection, row)
    try:
        return TrustedResultTTSEvent(
            event_id=row['event_id'],
            user_id=row['user_id'],
            conversation_id=row['conversation_id'],
            speech_session_id=row['speech_session_id'],
            session_instance_id=row['session_instance_id'],
            generation=row['generation'],
            result_code=row['result_code'],
            template_key=row['template_key'],
            template_digest=row['template_digest'],
            state=row['state'],
            attempt_count=row['attempt_count'],
            claim_fence=row['claim_fence'],
            created_at=row['created_at'],
            last_transition_at=row['last_transition_at'],
            acknowledged_at=row['acknowledged_at'],
            cancelled_at=row['cancelled_at'],
            cancellation_code=row['cancellation_code'],
        )
    except (TypeError, ValidationError):
        raise TrustedResultTTSError(
            'trusted result TTS event is incompatible'
        ) from None


def _claim_request_fingerprint(row: sqlite3.Row) -> str:
    return _canonical_hash(
        {
            'schema_version': 1,
            'domain': 'tool-result-tts-claim-v1',
            'claim_request_id': row['claim_request_id'],
            'event_id': row['event_id'],
            'user_id': row['user_id'],
            'conversation_id': row['conversation_id'],
            'speech_session_id': row['speech_session_id'],
            'claim_fence': int(row['claim_fence']),
            'attempt_number': int(row['attempt_number']),
            'claim_token': row['claim_token'],
            'lease_seconds': int(row['lease_seconds']),
            'claimed_at': _timestamp(row['claimed_at'], 'claimed_at'),
            'lease_expires_at': _timestamp(
                row['lease_expires_at'], 'lease_expires_at'
            ),
        }
    )


def _ack_token_digest(claim_token: str) -> str:
    return hashlib.sha256(
        b'tool-result-tts-ack-token-v1\0' + claim_token.encode('utf-8')
    ).hexdigest()


def _ack_values(
    event: sqlite3.Row,
    claim: sqlite3.Row,
    acknowledged_at: float,
) -> Dict[str, Any]:
    normalized_time = _timestamp(acknowledged_at, 'acknowledged_at')
    identity_digest = hashlib.sha256(
        b'tool-result-tts-ack-v1\0'
        + event['event_id'].encode('utf-8')
        + b'\0'
        + str(int(claim['claim_fence'])).encode('ascii')
    ).hexdigest()
    values: Dict[str, Any] = {
        'schema_version': 1,
        'acknowledgement_id': (
            f'trusted-result-tts-ack-{identity_digest}'
        ),
        'event_id': event['event_id'],
        'claim_request_id': claim['claim_request_id'],
        'claim_request_fingerprint': (
            claim['claim_request_fingerprint']
        ),
        'claim_fence': int(claim['claim_fence']),
        'claim_token_digest': _ack_token_digest(claim['claim_token']),
        'acknowledged_at': normalized_time,
        'result_code': 'tts_delivery_acknowledged',
    }
    values['acknowledgement_fingerprint'] = _canonical_hash(
        {
            **values,
            'domain': 'tool-result-tts-ack-v1',
            'event_fingerprint': event['event_fingerprint'],
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'execution_authorized': False,
            'physical_audio_verified': False,
        }
    )
    return values


def _validate_ack_row(
    event: sqlite3.Row,
    claim: sqlite3.Row,
    ack: sqlite3.Row,
) -> None:
    expected = _ack_values(event, claim, ack['acknowledged_at'])
    exact_fields = (
        'schema_version',
        'acknowledgement_id',
        'acknowledgement_fingerprint',
        'event_id',
        'claim_request_id',
        'claim_request_fingerprint',
        'claim_fence',
        'claim_token_digest',
        'acknowledged_at',
        'result_code',
    )
    if (
        any(ack[name] != expected[name] for name in exact_fields)
        or any(
            type(ack[name]) is not int or ack[name] != expected_value
            for name, expected_value in (
                ('schema_version', 1),
                ('simulation', 1),
                ('physical_authorized', 0),
                ('physical_effects', 0),
                ('execution_authorized', 0),
                ('physical_audio_verified', 0),
            )
        )
        or ack['acknowledged_at'] < claim['claimed_at']
        or ack['acknowledged_at'] >= claim['lease_expires_at']
    ):
        raise TrustedResultTTSError(
            'trusted result TTS ACK receipt is incompatible'
        )


def _validate_current_claim_binding_locked(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
) -> Optional[sqlite3.Row]:
    """Convert malformed restored-trigger rows to a closed error."""
    try:
        return _validate_current_claim_binding_unchecked(
            connection, event
        )
    except TrustedResultTTSError:
        raise
    except (
        AttributeError,
        OverflowError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        raise TrustedResultTTSError(
            'trusted result TTS current claim is incompatible'
        ) from None


def _validate_current_claim_binding_unchecked(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
) -> Optional[sqlite3.Row]:
    """Validate mutable current fields against the append-only claims."""
    claims = connection.execute(
        '''
        SELECT * FROM trusted_result_tts_claims
        WHERE event_id = ? ORDER BY claim_fence
        ''',
        (event['event_id'],),
    ).fetchall()
    if (
        type(event['attempt_count']) is not int
        or type(event['claim_fence']) is not int
        or event['claim_fence'] != event['attempt_count']
        or len(claims) != event['attempt_count']
    ):
        raise TrustedResultTTSError(
            'trusted result TTS claim sequence is incompatible'
        )
    previous_expiry = _timestamp(event['created_at'], 'created_at')
    for index, claim in enumerate(claims, start=1):
        if (
            type(claim['schema_version']) is not int
            or claim['schema_version'] != 1
            or type(claim['claim_fence']) is not int
            or claim['claim_fence'] != index
            or type(claim['attempt_number']) is not int
            or claim['attempt_number'] != index
            or type(claim['lease_seconds']) is not int
            or not TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS
            <= claim['lease_seconds']
            <= TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS
            or claim['user_id'] != event['user_id']
            or claim['conversation_id'] != event['conversation_id']
            or claim['speech_session_id'] != event['speech_session_id']
            or not isinstance(claim['claim_token'], str)
            or not _CLAIM_TOKEN.fullmatch(claim['claim_token'])
            or _timestamp(claim['lease_expires_at'], 'lease_expires_at')
            != _timestamp(claim['claimed_at'], 'claimed_at')
            + claim['lease_seconds']
            or _timestamp(claim['claimed_at'], 'claimed_at')
            < previous_expiry
            or claim['claim_request_fingerprint']
            != _claim_request_fingerprint(claim)
        ):
            raise TrustedResultTTSError(
                'trusted result TTS claim is incompatible'
            )
        previous_expiry = _timestamp(
            claim['lease_expires_at'], 'lease_expires_at'
        )
    acknowledgements = connection.execute(
        '''
        SELECT * FROM trusted_result_tts_acknowledgements
        WHERE event_id = ?
        ''',
        (event['event_id'],),
    ).fetchall()
    if len(acknowledgements) > 1:
        raise TrustedResultTTSError(
            'trusted result TTS ACK receipt is incompatible'
        )
    if not claims:
        if acknowledgements:
            raise TrustedResultTTSError(
                'trusted result TTS ACK receipt has no claim'
            )
        return None
    current = claims[-1]
    exact_text_fields = (
        ('current_claim_request_id', 'claim_request_id'),
        (
            'current_claim_request_fingerprint',
            'claim_request_fingerprint',
        ),
    )
    if (
        any(
            event[event_name] != current[claim_name]
            for event_name, claim_name in exact_text_fields
        )
        or not isinstance(event['current_claim_token'], str)
        or not hmac.compare_digest(
            event['current_claim_token'], current['claim_token']
        )
        or event['current_lease_seconds'] != current['lease_seconds']
        or event['claimed_at'] != current['claimed_at']
        or (
            event['state'] == 'claimed'
            and event['lease_expires_at']
            != current['lease_expires_at']
        )
        or (
            event['state'] != 'claimed'
            and event['lease_expires_at'] is not None
        )
        or (
            event['state'] == 'acknowledged'
            and not (
                event['acknowledged_at'] >= current['claimed_at']
                and event['acknowledged_at']
                < current['lease_expires_at']
            )
        )
        or (
            event['cancellation_code']
            == 'delivery_attempts_exhausted'
            and (
                event['attempt_count']
                != TRUSTED_RESULT_TTS_MAX_ATTEMPTS
                or event['cancelled_at']
                < current['lease_expires_at']
            )
        )
    ):
        raise TrustedResultTTSError(
            'trusted result TTS current claim is incompatible'
        )
    if event['state'] == 'acknowledged':
        if len(acknowledgements) != 1:
            raise TrustedResultTTSError(
                'trusted result TTS ACK receipt is missing'
            )
        ack = acknowledgements[0]
        _validate_ack_row(event, current, ack)
        if event['acknowledged_at'] != ack['acknowledged_at']:
            raise TrustedResultTTSError(
                'trusted result TTS ACK receipt is incompatible'
            )
    elif acknowledgements:
        raise TrustedResultTTSError(
            'trusted result TTS ACK receipt contradicts event state'
        )
    return current


def _claim_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> TrustedResultTTSClaim:
    event = connection.execute(
        'SELECT * FROM trusted_result_tts_outbox WHERE event_id = ?',
        (row['event_id'],),
    ).fetchone()
    if event is None:
        raise TrustedResultTTSError('trusted result TTS event is missing')
    _event_from_row(connection, event)
    if (
        row['claim_request_fingerprint']
        != _claim_request_fingerprint(row)
        or type(row['schema_version']) is not int
        or row['schema_version'] != 1
        or type(row['claim_fence']) is not int
        or type(row['attempt_number']) is not int
        or type(row['lease_seconds']) is not int
        or row['user_id'] != event['user_id']
        or row['conversation_id'] != event['conversation_id']
        or row['speech_session_id'] != event['speech_session_id']
    ):
        raise TrustedResultTTSError(
            'trusted result TTS claim is incompatible'
        )
    try:
        return TrustedResultTTSClaim(
            event_id=event['event_id'],
            claim_request_id=row['claim_request_id'],
            claim_token=row['claim_token'],
            claim_fence=row['claim_fence'],
            attempt_number=row['attempt_number'],
            result_code=event['result_code'],
            template_key=event['template_key'],
            template_digest=event['template_digest'],
            speech_session_id=event['speech_session_id'],
            claimed_at=row['claimed_at'],
            lease_expires_at=row['lease_expires_at'],
        )
    except (TypeError, ValidationError):
        raise TrustedResultTTSError(
            'trusted result TTS claim is incompatible'
        ) from None


def validate_trusted_result_tts_schema_locked(
    connection: sqlite3.Connection,
) -> None:
    """Fail closed on DDL, activation, source, payload, or state drift."""
    expected = _expected_objects()
    for name, (object_type, exact_sql) in expected.items():
        row = connection.execute(
            'SELECT type, sql FROM sqlite_master WHERE name = ?',
            (name,),
        ).fetchone()
        if (
            row is None
            or row['type'] != object_type
            or str(row['sql']).strip() != exact_sql.strip()
        ):
            raise TrustedResultTTSError(
                'trusted result TTS schema is incompatible'
            )
    custom = {
        (str(row['type']), str(row['name']), str(row['tbl_name']))
        for row in connection.execute(
            '''
            SELECT type, name, tbl_name FROM sqlite_master
            WHERE type IN ('index', 'trigger')
              AND tbl_name IN (
                  'trusted_result_tts_schema_metadata',
                  'trusted_result_tts_preactivation_sources',
                  'trusted_result_tts_outbox',
                  'trusted_result_tts_claims',
                  'trusted_result_tts_acknowledgements'
              )
              AND sql IS NOT NULL
            '''
        ).fetchall()
    }
    table_for_name = {
        'trusted_result_tts_owner_idx': 'trusted_result_tts_outbox',
        'trusted_result_tts_one_claimed_conversation_idx': (
            'trusted_result_tts_outbox'
        ),
        'trusted_result_tts_insert_guard': 'trusted_result_tts_outbox',
        'trusted_result_tts_identity_no_update': (
            'trusted_result_tts_outbox'
        ),
        'trusted_result_tts_transition_guard': (
            'trusted_result_tts_outbox'
        ),
        'trusted_result_tts_no_replace': 'trusted_result_tts_outbox',
        'trusted_result_tts_claim_insert_guard': (
            'trusted_result_tts_claims'
        ),
        'trusted_result_tts_claim_no_update': (
            'trusted_result_tts_claims'
        ),
        'trusted_result_tts_claim_no_replace': (
            'trusted_result_tts_claims'
        ),
        'trusted_result_tts_claim_no_delete': (
            'trusted_result_tts_claims'
        ),
        'trusted_result_tts_ack_insert_guard': (
            'trusted_result_tts_acknowledgements'
        ),
        'trusted_result_tts_ack_no_update': (
            'trusted_result_tts_acknowledgements'
        ),
        'trusted_result_tts_ack_no_replace': (
            'trusted_result_tts_acknowledgements'
        ),
        'trusted_result_tts_ack_no_delete': (
            'trusted_result_tts_acknowledgements'
        ),
        'trusted_result_tts_preactivation_no_update': (
            'trusted_result_tts_preactivation_sources'
        ),
        'trusted_result_tts_preactivation_no_delete': (
            'trusted_result_tts_preactivation_sources'
        ),
        'trusted_result_tts_preactivation_no_insert': (
            'trusted_result_tts_preactivation_sources'
        ),
        'trusted_result_tts_metadata_no_update': (
            'trusted_result_tts_schema_metadata'
        ),
        'trusted_result_tts_metadata_no_delete': (
            'trusted_result_tts_schema_metadata'
        ),
        'trusted_result_tts_metadata_no_replace': (
            'trusted_result_tts_schema_metadata'
        ),
    }
    expected_custom = {
        (object_type, name, table_for_name[name])
        for name, (object_type, _sql) in expected.items()
        if object_type in {'index', 'trigger'}
    }
    if custom != expected_custom:
        raise TrustedResultTTSError(
            'trusted result TTS schema has unexpected objects'
        )
    metadata_rows = connection.execute(
        '''
        SELECT *, typeof(singleton) AS singleton_type,
               typeof(schema_version) AS version_type,
               typeof(activated_at) AS activated_type,
               typeof(activation_epoch) AS epoch_type,
               typeof(preactivation_count) AS count_type,
               typeof(preactivation_digest) AS digest_type
        FROM trusted_result_tts_schema_metadata
        '''
    ).fetchall()
    if len(metadata_rows) != 1:
        raise TrustedResultTTSError(
            'trusted result TTS metadata is incompatible'
        )
    metadata = metadata_rows[0]
    if (
        metadata['singleton'] != 1
        or metadata['schema_version'] != 1
        or metadata['singleton_type'] != 'integer'
        or metadata['version_type'] != 'integer'
        or metadata['activated_type'] not in {'integer', 'real'}
        or metadata['epoch_type'] != 'text'
        or metadata['count_type'] != 'integer'
        or metadata['digest_type'] != 'text'
        or not _HEX_DIGEST.fullmatch(metadata['activation_epoch'])
        or not _HEX_DIGEST.fullmatch(metadata['preactivation_digest'])
        or int(metadata['preactivation_count']) < 0
    ):
        raise TrustedResultTTSError(
            'trusted result TTS metadata is incompatible'
        )
    _timestamp(metadata['activated_at'], 'activated_at')
    simulation = connection.execute(
        '''
        SELECT activation_epoch, activated_at
        FROM monitor_room_simulation_schema_metadata
        WHERE singleton = 1
        '''
    ).fetchone()
    sentinel = connection.execute(
        '''
        SELECT * FROM monitor_room_simulation_preactivation_proposals
        WHERE proposal_fingerprint = ?
        ''',
        (TRUSTED_RESULT_TTS_ACTIVATION_SENTINEL,),
    ).fetchone()
    if (
        simulation is None
        or sentinel is None
        or sentinel['activation_epoch'] != simulation['activation_epoch']
        or sentinel['snapshotted_at'] != simulation['activated_at']
        or type(sentinel['snapshot_rowid']) is not int
        or sentinel['snapshot_rowid']
        != _activation_anchor_value(metadata['preactivation_digest'])
    ):
        raise TrustedResultTTSError(
            'trusted result TTS activation anchor is incompatible'
        )
    snapshot = connection.execute(
        '''
        SELECT trusted_result_id, trusted_result_fingerprint
        FROM trusted_result_tts_preactivation_sources
        ORDER BY trusted_result_id
        '''
    ).fetchall()
    snapshot_digest = _canonical_hash(
        {
            'schema_version': 1,
            'activated_at': _timestamp(
                metadata['activated_at'], 'activated_at'
            ),
            'activation_epoch': metadata['activation_epoch'],
            'sources': [
                [row['trusted_result_id'], row['trusted_result_fingerprint']]
                for row in snapshot
            ],
        }
    )
    if (
        len(snapshot) != int(metadata['preactivation_count'])
        or snapshot_digest != metadata['preactivation_digest']
        or any(
            not isinstance(row['trusted_result_id'], str)
            or not _SAFE_IDENTIFIER.fullmatch(row['trusted_result_id'])
            or not isinstance(row['trusted_result_fingerprint'], str)
            or not _HEX_DIGEST.fullmatch(
                row['trusted_result_fingerprint']
            )
            for row in snapshot
        )
    ):
        raise TrustedResultTTSError(
            'trusted result TTS preactivation snapshot is incompatible'
        )
    outbox_foreign_keys = connection.execute(
        'PRAGMA foreign_key_list(trusted_result_tts_outbox)'
    ).fetchall()
    if len(outbox_foreign_keys) != 1 or (
        outbox_foreign_keys[0]['table']
        != 'conversation_trusted_tool_results'
        or outbox_foreign_keys[0]['from'] != 'trusted_result_id'
        or outbox_foreign_keys[0]['to'] != 'trusted_result_id'
        or str(outbox_foreign_keys[0]['on_delete']).upper() != 'CASCADE'
    ):
        raise TrustedResultTTSError(
            'trusted result TTS ownership is incompatible'
        )
    claim_foreign_keys = connection.execute(
        'PRAGMA foreign_key_list(trusted_result_tts_claims)'
    ).fetchall()
    if len(claim_foreign_keys) != 1 or (
        claim_foreign_keys[0]['table'] != 'trusted_result_tts_outbox'
        or claim_foreign_keys[0]['from'] != 'event_id'
        or claim_foreign_keys[0]['to'] != 'event_id'
        or str(claim_foreign_keys[0]['on_delete']).upper() != 'CASCADE'
    ):
        raise TrustedResultTTSError(
            'trusted result TTS claim ownership is incompatible'
        )
    ack_foreign_keys = connection.execute(
        'PRAGMA foreign_key_list(trusted_result_tts_acknowledgements)'
    ).fetchall()
    if len(ack_foreign_keys) != 1 or (
        ack_foreign_keys[0]['table'] != 'trusted_result_tts_outbox'
        or ack_foreign_keys[0]['from'] != 'event_id'
        or ack_foreign_keys[0]['to'] != 'event_id'
        or str(ack_foreign_keys[0]['on_delete']).upper() != 'CASCADE'
    ):
        raise TrustedResultTTSError(
            'trusted result TTS ACK ownership is incompatible'
        )
    event_rows = connection.execute(
        'SELECT * FROM trusted_result_tts_outbox'
    ).fetchall()
    snapshot_ids = {row['trusted_result_id'] for row in snapshot}
    for event in event_rows:
        _event_from_row(connection, event)
        is_old = event['trusted_result_id'] in snapshot_ids
        if is_old != (
            event['state'] == 'cancelled'
            and event['cancellation_code'] == 'preactivation'
        ):
            raise TrustedResultTTSError(
                'trusted result TTS activation state is incompatible'
            )
        if is_old and (
            event['cancelled_at']
            != max(event['created_at'], metadata['activated_at'])
            or event['last_transition_at'] != event['cancelled_at']
        ):
            raise TrustedResultTTSError(
                'trusted result TTS activation time is incompatible'
            )
        claims = connection.execute(
            '''
            SELECT * FROM trusted_result_tts_claims
            WHERE event_id = ? ORDER BY claim_fence
            ''',
            (event['event_id'],),
        ).fetchall()
        if (
            len(claims) != event['attempt_count']
            or any(
                claim['claim_fence'] != index
                for index, claim in enumerate(claims, start=1)
            )
        ):
            raise TrustedResultTTSError(
                'trusted result TTS claim sequence is incompatible'
            )
        for claim in claims:
            _claim_from_row(connection, claim)
        if event['attempt_count'] > 0:
            current = claims[-1]
            for claim_field in (
                'claim_request_id',
                'claim_request_fingerprint',
                'claim_token',
                'lease_seconds',
                'claimed_at',
            ):
                event_field = (
                    'current_' + claim_field
                    if claim_field in {
                        'claim_request_id',
                        'claim_request_fingerprint',
                        'claim_token',
                        'lease_seconds',
                    }
                    else claim_field
                )
                if event[event_field] != current[claim_field]:
                    raise TrustedResultTTSError(
                        'trusted result TTS current claim is incompatible'
                    )
            if (
                event['state'] == 'claimed'
                and event['lease_expires_at']
                != current['lease_expires_at']
            ):
                raise TrustedResultTTSError(
                    'trusted result TTS current lease is incompatible'
                )
            if event['state'] == 'acknowledged' and not (
                event['acknowledged_at'] >= current['claimed_at']
                and event['acknowledged_at']
                < current['lease_expires_at']
            ):
                raise TrustedResultTTSError(
                    'trusted result TTS ACK time is incompatible'
                )
            if (
                event['cancellation_code']
                == 'delivery_attempts_exhausted'
                and (
                    event['attempt_count']
                    != TRUSTED_RESULT_TTS_MAX_ATTEMPTS
                    or event['cancelled_at']
                    < current['lease_expires_at']
                )
            ):
                raise TrustedResultTTSError(
                    'trusted result TTS exhaustion is incompatible'
                )
        if event['cancellation_code'] in {
            'conversation_reset', 'conversation_inactive'
        }:
            owner = connection.execute(
                '''
                SELECT session_instance_id, generation, status
                FROM conversation_sessions
                WHERE user_id = ? AND conversation_id = ?
                ''',
                (event['user_id'], event['conversation_id']),
            ).fetchone()
            if (
                owner is not None
                and owner['session_instance_id']
                == event['session_instance_id']
                and owner['generation'] == event['generation']
                and owner['status'] == 'active'
            ):
                raise TrustedResultTTSError(
                    'trusted result TTS lifecycle cancel is incompatible'
                )
    missing = connection.execute(
        '''
        SELECT 1
        FROM conversation_trusted_tool_results AS result
        LEFT JOIN trusted_result_tts_outbox AS event
          ON event.trusted_result_id = result.trusted_result_id
        WHERE event.trusted_result_id IS NULL
        LIMIT 1
        '''
    ).fetchone()
    if missing is not None:
        raise TrustedResultTTSError(
            'trusted result TTS event is missing'
        )


def record_or_verify_trusted_result_tts_locked(
    connection: sqlite3.Connection,
    *,
    trusted_result: Optional[TrustedToolResult],
    replayed: bool,
) -> Optional[TrustedResultTTSEvent]:
    """Atomically append one pending event or verify its exact replay."""
    if not connection.in_transaction:
        raise TrustedResultTTSError(
            'trusted result TTS recording requires a write transaction'
        )
    if trusted_result is None:
        return None
    source, stored_result = _source_for_trusted_result(
        connection, trusted_result
    )
    existing = connection.execute(
        '''
        SELECT * FROM trusted_result_tts_outbox
        WHERE trusted_result_id = ?
        ''',
        (stored_result.trusted_result_id,),
    ).fetchone()
    if existing is not None:
        return _event_from_row(connection, existing)
    if replayed:
        raise TrustedResultTTSError(
            'trusted result TTS event is missing for exact replay'
        )
    snapshot = connection.execute(
        '''
        SELECT 1 FROM trusted_result_tts_preactivation_sources
        WHERE trusted_result_id = ?
        ''',
        (stored_result.trusted_result_id,),
    ).fetchone()
    if snapshot is not None:
        raise TrustedResultTTSError(
            'preactivation trusted result TTS event is missing'
        )
    values = _event_values(stored_result, source)
    _insert_event_locked(connection, values)
    row = connection.execute(
        'SELECT * FROM trusted_result_tts_outbox WHERE event_id = ?',
        (values['event_id'],),
    ).fetchone()
    return _event_from_row(connection, row)


def cancel_trusted_result_tts_locked(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    session_instance_id: str,
    generation: Optional[int],
    cancellation_code: str,
    now: float,
) -> int:
    """Cancel nonterminal events for one exact lifecycle boundary."""
    if not connection.in_transaction:
        raise TrustedResultTTSError(
            'trusted result TTS cancellation requires a write transaction'
        )
    if cancellation_code not in {
        'conversation_reset', 'conversation_inactive'
    }:
        raise ValueError('trusted result TTS cancellation code is invalid')
    normalized_now = _timestamp(now, 'cancelled_at')
    parameters: list[Any] = [
        user_id,
        conversation_id,
        session_instance_id,
    ]
    generation_sql = ''
    if generation is not None:
        if type(generation) is not int or generation < 1:
            raise ValueError('generation is invalid')
        generation_sql = ' AND generation = ?'
        parameters.append(generation)
    rows = connection.execute(
        f'''
        SELECT * FROM trusted_result_tts_outbox
        WHERE user_id = ? AND conversation_id = ?
          AND session_instance_id = ?
          {generation_sql}
          AND state IN ('pending', 'claimed')
        ORDER BY source_ordinal, event_id
        ''',
        tuple(parameters),
    ).fetchall()
    for row in rows:
        _event_from_row(connection, row)
        if normalized_now < row['last_transition_at']:
            raise TrustedResultTTSError('server clock moved backwards')
        cursor = connection.execute(
            '''
            UPDATE trusted_result_tts_outbox
            SET state = 'cancelled',
                lease_expires_at = NULL,
                acknowledged_at = NULL,
                cancelled_at = ?,
                cancellation_code = ?,
                last_transition_at = ?
            WHERE event_id = ? AND state IN ('pending', 'claimed')
            ''',
            (
                normalized_now,
                cancellation_code,
                normalized_now,
                row['event_id'],
            ),
        )
        if cursor.rowcount != 1:
            raise TrustedResultTTSConflictError(
                'trusted result TTS lifecycle changed'
            )
    return len(rows)


def _cancel_stale_or_exhausted_locked(
    connection: sqlite3.Connection,
    *,
    now: float,
) -> None:
    rows = connection.execute(
        '''
        SELECT event.*, session.status AS owner_status,
               session.generation AS owner_generation,
               session.session_instance_id AS owner_instance,
               session.expires_at AS owner_expires_at
        FROM trusted_result_tts_outbox AS event
        LEFT JOIN conversation_sessions AS session
          ON session.user_id = event.user_id
         AND session.conversation_id = event.conversation_id
        WHERE event.state IN ('pending', 'claimed')
        ORDER BY event.source_ordinal, event.event_id
        '''
    ).fetchall()
    for row in rows:
        _event_from_row(connection, row, allow_stale=True)
        stale = (
            row['owner_status'] != 'active'
            or row['owner_generation'] != row['generation']
            or row['owner_instance'] != row['session_instance_id']
            or row['owner_expires_at'] <= now
        )
        exhausted = (
            row['state'] == 'claimed'
            and row['attempt_count'] == TRUSTED_RESULT_TTS_MAX_ATTEMPTS
            and row['lease_expires_at'] <= now
        )
        if not stale and not exhausted:
            continue
        if now < row['last_transition_at']:
            raise TrustedResultTTSError('server clock moved backwards')
        code = (
            'conversation_inactive'
            if stale else 'delivery_attempts_exhausted'
        )
        cursor = connection.execute(
            '''
            UPDATE trusted_result_tts_outbox
            SET state = 'cancelled',
                lease_expires_at = NULL,
                acknowledged_at = NULL,
                cancelled_at = ?,
                cancellation_code = ?,
                last_transition_at = ?
            WHERE event_id = ? AND state IN ('pending', 'claimed')
            ''',
            (now, code, now, row['event_id']),
        )
        if cursor.rowcount != 1:
            raise TrustedResultTTSConflictError(
                'trusted result TTS lifecycle changed'
            )


def claim_trusted_result_tts_locked(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    speech_session_id: str,
    claim_request_id: str,
    lease_seconds: int,
    now: float,
) -> Optional[TrustedResultTTSClaim]:
    """Lease the oldest current event with commit-response replay safety."""
    if not connection.in_transaction:
        raise TrustedResultTTSError(
            'trusted result TTS claim requires a write transaction'
        )
    normalized_now = _timestamp(now, 'claimed_at')
    normalized_speech = _identifier(
        speech_session_id, 'speech_session_id'
    )
    normalized_request = _identifier(
        claim_request_id, 'claim_request_id'
    )
    if (
        type(lease_seconds) is not int
        or not TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS
        <= lease_seconds
        <= TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS
    ):
        raise ValueError('lease_seconds is invalid')
    _cancel_stale_or_exhausted_locked(
        connection,
        now=normalized_now,
    )
    prior = connection.execute(
        '''
        SELECT * FROM trusted_result_tts_claims
        WHERE claim_request_id = ?
        ''',
        (normalized_request,),
    ).fetchone()
    if prior is not None:
        claim = _claim_from_row(connection, prior)
        event = connection.execute(
            'SELECT * FROM trusted_result_tts_outbox WHERE event_id = ?',
            (prior['event_id'],),
        ).fetchone()
        if (
            prior['user_id'] != user_id
            or prior['conversation_id'] != conversation_id
            or prior['speech_session_id'] != normalized_speech
            or prior['lease_seconds'] != lease_seconds
            or event['current_claim_request_id'] != normalized_request
            or event['claim_fence'] != prior['claim_fence']
        ):
            raise TrustedResultTTSConflictError(
                'trusted result TTS claim request conflicts'
            )
        if event['state'] == 'acknowledged':
            return None
        if event['state'] != 'claimed':
            raise TrustedResultTTSConflictError(
                'trusted result TTS claim is no longer current'
            )
        if normalized_now >= prior['lease_expires_at']:
            return None
        return claim
    row = connection.execute(
        '''
        SELECT event.*
        FROM trusted_result_tts_outbox AS event
        JOIN conversation_sessions AS session
          ON session.user_id = event.user_id
         AND session.conversation_id = event.conversation_id
        WHERE event.user_id = ? AND event.conversation_id = ?
          AND event.session_instance_id = session.session_instance_id
          AND event.generation = session.generation
          AND session.status = 'active'
          AND session.expires_at > ?
          AND event.state IN ('pending', 'claimed')
        ORDER BY event.source_ordinal, event.event_id
        LIMIT 1
        ''',
        (user_id, conversation_id, normalized_now),
    ).fetchone()
    if row is None:
        return None
    _event_from_row(connection, row)
    if row['speech_session_id'] != normalized_speech:
        return None
    if row['state'] == 'claimed' and normalized_now < row['lease_expires_at']:
        return None
    if normalized_now < row['last_transition_at']:
        raise TrustedResultTTSError('server clock moved backwards')
    next_fence = int(row['claim_fence']) + 1
    if next_fence > TRUSTED_RESULT_TTS_MAX_ATTEMPTS:
        raise TrustedResultTTSError(
            'trusted result TTS attempt bound is incompatible'
        )
    lease_expires_at = normalized_now + lease_seconds
    if not math.isfinite(lease_expires_at):
        raise TrustedResultTTSError('trusted result TTS lease is invalid')
    claim_values: Dict[str, Any] = {
        'schema_version': 1,
        'claim_request_id': normalized_request,
        'event_id': row['event_id'],
        'user_id': user_id,
        'conversation_id': conversation_id,
        'speech_session_id': normalized_speech,
        'claim_fence': next_fence,
        'attempt_number': next_fence,
        'claim_token': secrets.token_urlsafe(32),
        'lease_seconds': lease_seconds,
        'claimed_at': normalized_now,
        'lease_expires_at': lease_expires_at,
    }
    claim_values['claim_request_fingerprint'] = (
        _claim_request_fingerprint(claim_values)  # type: ignore[arg-type]
    )
    connection.execute(
        '''
        INSERT INTO trusted_result_tts_claims (
            schema_version, claim_request_id,
            claim_request_fingerprint, event_id,
            user_id, conversation_id, speech_session_id,
            claim_fence, attempt_number, claim_token,
            lease_seconds, claimed_at, lease_expires_at
        ) VALUES (
            :schema_version, :claim_request_id,
            :claim_request_fingerprint, :event_id,
            :user_id, :conversation_id, :speech_session_id,
            :claim_fence, :attempt_number, :claim_token,
            :lease_seconds, :claimed_at, :lease_expires_at
        )
        ''',
        claim_values,
    )
    cursor = connection.execute(
        '''
        UPDATE trusted_result_tts_outbox
        SET state = 'claimed',
            attempt_count = ?,
            claim_fence = ?,
            current_claim_request_id = ?,
            current_claim_request_fingerprint = ?,
            current_claim_token = ?,
            current_lease_seconds = ?,
            claimed_at = ?,
            lease_expires_at = ?,
            acknowledged_at = NULL,
            cancelled_at = NULL,
            cancellation_code = NULL,
            last_transition_at = ?
        WHERE event_id = ?
          AND claim_fence = ?
          AND state = ?
        ''',
        (
            next_fence,
            next_fence,
            normalized_request,
            claim_values['claim_request_fingerprint'],
            claim_values['claim_token'],
            lease_seconds,
            normalized_now,
            lease_expires_at,
            normalized_now,
            row['event_id'],
            row['claim_fence'],
            row['state'],
        ),
    )
    if cursor.rowcount != 1:
        raise TrustedResultTTSConflictError(
            'trusted result TTS claim changed'
        )
    stored = connection.execute(
        '''
        SELECT * FROM trusted_result_tts_claims
        WHERE claim_request_id = ?
        ''',
        (normalized_request,),
    ).fetchone()
    return _claim_from_row(connection, stored)


def acknowledge_trusted_result_tts_locked(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    speech_session_id: str,
    event_id: str,
    claim_token: str,
    claim_fence: int,
    now: float,
) -> TrustedResultTTSEvent:
    """Record a trusted downstream terminal ACK, never audible proof."""
    if not connection.in_transaction:
        raise TrustedResultTTSError(
            'trusted result TTS ACK requires a write transaction'
        )
    normalized_now = _timestamp(now, 'acknowledged_at')
    normalized_speech = _identifier(
        speech_session_id, 'speech_session_id'
    )
    normalized_event = _identifier(event_id, 'event_id')
    normalized_token = _identifier(claim_token, 'claim_token')
    if (
        not _SAFE_IDENTIFIER.fullmatch(normalized_event)
        or not _CLAIM_TOKEN.fullmatch(normalized_token)
        or type(claim_fence) is not int
        or not 1 <= claim_fence <= TRUSTED_RESULT_TTS_MAX_ATTEMPTS
    ):
        raise ValidationError('trusted result TTS ACK is invalid')
    row = connection.execute(
        'SELECT * FROM trusted_result_tts_outbox WHERE event_id = ?',
        (normalized_event,),
    ).fetchone()
    if row is None:
        raise TrustedResultTTSConflictError(
            'trusted result TTS event was not found'
        )
    event = _event_from_row(connection, row)
    exact = (
        row['user_id'] == user_id
        and row['conversation_id'] == conversation_id
        and row['speech_session_id'] == normalized_speech
        and isinstance(row['current_claim_token'], str)
        and hmac.compare_digest(
            row['current_claim_token'], normalized_token
        )
        and row['claim_fence'] == claim_fence
    )
    if row['state'] == 'acknowledged':
        if not exact:
            raise TrustedResultTTSConflictError(
                'trusted result TTS ACK conflicts'
            )
        return event
    if row['state'] != 'claimed' or not exact:
        raise TrustedResultTTSConflictError(
            'trusted result TTS ACK is stale'
        )
    if normalized_now < row['claimed_at']:
        raise TrustedResultTTSError('server clock moved backwards')
    if normalized_now >= row['lease_expires_at']:
        raise TrustedResultTTSConflictError(
            'trusted result TTS claim lease expired'
        )
    current_claim = _validate_current_claim_binding_locked(
        connection, row
    )
    if current_claim is None:
        raise TrustedResultTTSError(
            'trusted result TTS ACK claim is missing'
        )
    ack_values = _ack_values(row, current_claim, normalized_now)
    connection.execute(
        '''
        INSERT INTO trusted_result_tts_acknowledgements (
            schema_version, acknowledgement_id,
            acknowledgement_fingerprint, event_id,
            claim_request_id, claim_request_fingerprint,
            claim_fence, claim_token_digest, acknowledged_at,
            result_code, simulation, physical_authorized,
            physical_effects, execution_authorized,
            physical_audio_verified
        ) VALUES (
            :schema_version, :acknowledgement_id,
            :acknowledgement_fingerprint, :event_id,
            :claim_request_id, :claim_request_fingerprint,
            :claim_fence, :claim_token_digest, :acknowledged_at,
            :result_code, 1, 0, 0, 0, 0
        )
        ''',
        ack_values,
    )
    cursor = connection.execute(
        '''
        UPDATE trusted_result_tts_outbox
        SET state = 'acknowledged',
            lease_expires_at = NULL,
            acknowledged_at = ?,
            cancelled_at = NULL,
            cancellation_code = NULL,
            last_transition_at = ?
        WHERE event_id = ? AND state = 'claimed'
          AND claim_fence = ? AND current_claim_token = ?
        ''',
        (
            normalized_now,
            normalized_now,
            normalized_event,
            claim_fence,
            normalized_token,
        ),
    )
    if cursor.rowcount != 1:
        raise TrustedResultTTSConflictError(
            'trusted result TTS ACK changed'
        )
    stored = connection.execute(
        'SELECT * FROM trusted_result_tts_outbox WHERE event_id = ?',
        (normalized_event,),
    ).fetchone()
    return _event_from_row(connection, stored)


__all__ = [
    'TRUSTED_RESULT_TTS_MAX_ATTEMPTS',
    'TRUSTED_RESULT_TTS_MAX_LEASE_SECONDS',
    'TRUSTED_RESULT_TTS_MIN_LEASE_SECONDS',
    'TRUSTED_RESULT_TTS_SCHEMA_VERSION',
    'TRUSTED_RESULT_TTS_TEMPLATE_VERSION',
    'TrustedResultTTSClaim',
    'TrustedResultTTSConflictError',
    'TrustedResultTTSError',
    'TrustedResultTTSEvent',
    'acknowledge_trusted_result_tts_locked',
    'cancel_trusted_result_tts_locked',
    'claim_trusted_result_tts_locked',
    'prepare_trusted_result_tts_schema_locked',
    'record_or_verify_trusted_result_tts_locked',
    'validate_trusted_result_tts_schema_locked',
]
