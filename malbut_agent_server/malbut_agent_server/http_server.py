"""Small JSON HTTP server for the Malbut agent boundary."""

import hmac
import json
import threading
import time
import weakref
from collections import deque
from http import HTTPStatus
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from typing import Any, Dict, Optional, Tuple

from malbut_agent_server.config import (
    DEFAULT_FAILED_AUTH_ATTEMPTS_PER_MINUTE,
    MAX_FAILED_AUTH_ATTEMPTS_PER_MINUTE,
    validate_scripted_auth_token,
)
from malbut_agent_server.conversation import (
    ConversationChangedError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStateError,
)
from malbut_agent_server.gateway import (
    GatewayConflictError,
    ToolGateway,
    ToolQuery,
)
from malbut_agent_server.gazebo_simulation_execution import (
    GazeboSimulationExecutionError,
    GazeboSimulationExecutionResult,
    GazeboSimulationExecutionSeam,
)
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    ExpiredDecisionError,
    MemoryChangedError,
)
from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.schemas import (
    AgentRequest,
    ValidationError,
    validate_conversation_id,
    validate_user_id,
)
from malbut_agent_server.speech import (
    SpeechConversationCoordinator,
    SpeechTranscriptEvent,
    TrustedSpeechBinding,
)


SCRIPTED_SPEECH_RUNTIME = 'scripted_text_only'
SCRIPTED_SPEAKER_ID = 'scripted-http-user'
SCRIPTED_SOURCE = 'scripted-http'
AUTHORIZATION_ALLOWED = 'allowed'
AUTHORIZATION_REJECTED = 'rejected'
AUTHORIZATION_RATE_LIMITED = 'rate_limited'
GAZEBO_SIMULATION_EXECUTION_PATH = (
    '/v1/internal/gazebo-simulation/consume-and-prepare'
)
_GAZEBO_SERVER_BINDING_LOCK = threading.RLock()
_GAZEBO_SERVER_BINDINGS: (
    'weakref.WeakKeyDictionary[Any, Tuple[Any, ...]]'
) = weakref.WeakKeyDictionary()


class AgentHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying explicit service dependencies."""

    daemon_threads = False
    block_on_close = True

    def __init__(
        self,
        address: Tuple[str, int],
        orchestrator: AgentOrchestrator,
        max_request_bytes: int,
        auth_token: str = '',
        allowed_user_id: str = 'local-user',
        max_concurrent_requests: int = 8,
        requests_per_minute: int = 60,
        failed_auth_attempts_per_minute: int = (
            DEFAULT_FAILED_AUTH_ATTEMPTS_PER_MINUTE
        ),
        socket_timeout_seconds: int = 10,
        tool_gateway: Optional[ToolGateway] = None,
        speech_coordinator: Optional[
            SpeechConversationCoordinator
        ] = None,
        gazebo_simulation_execution_seam: Optional[
            GazeboSimulationExecutionSeam
        ] = None,
    ) -> None:
        """Attach runtime services before binding the HTTP listener."""
        if address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('Agent HTTP server is loopback-only')
        if max_request_bytes < 1:
            raise ValueError('max_request_bytes must be positive')
        if max_concurrent_requests < 1:
            raise ValueError(
                'max_concurrent_requests must be positive'
            )
        if requests_per_minute < 1:
            raise ValueError('requests_per_minute must be positive')
        if (
            isinstance(failed_auth_attempts_per_minute, bool)
            or not isinstance(failed_auth_attempts_per_minute, int)
            or failed_auth_attempts_per_minute < 1
            or failed_auth_attempts_per_minute
            > MAX_FAILED_AUTH_ATTEMPTS_PER_MINUTE
        ):
            raise ValueError(
                'failed_auth_attempts_per_minute is invalid'
            )
        if socket_timeout_seconds < 1:
            raise ValueError(
                'socket_timeout_seconds must be positive'
            )
        if orchestrator.test_only_trusted_robot_state:
            raise ValueError(
                'HTTP cannot use test-only client RobotState trust'
            )
        normalized_allowed_user = validate_user_id(allowed_user_id)
        if speech_coordinator is not None:
            if not isinstance(
                speech_coordinator,
                SpeechConversationCoordinator,
            ):
                raise TypeError(
                    'speech_coordinator must be a '
                    'SpeechConversationCoordinator'
                )
            if speech_coordinator.orchestrator is not orchestrator:
                raise ValueError(
                    'speech coordinator and HTTP server must share one '
                    'orchestrator'
                )
            if not auth_token:
                raise ValueError(
                    'scripted speech requires HTTP bearer auth'
                )
            validate_scripted_auth_token(auth_token)
        if gazebo_simulation_execution_seam is not None:
            if type(
                gazebo_simulation_execution_seam
            ) is not GazeboSimulationExecutionSeam:
                raise TypeError(
                    'gazebo_simulation_execution_seam must be a fixed '
                    'GazeboSimulationExecutionSeam'
                )
            if not auth_token:
                raise ValueError(
                    'Gazebo simulation execution requires HTTP bearer auth'
                )
            validate_scripted_auth_token(auth_token)
            if not (
                GazeboSimulationExecutionSeam.matches_runtime(
                    gazebo_simulation_execution_seam,
                    orchestrator.conversation_store,
                    normalized_allowed_user,
                )
            ):
                raise ValueError(
                    'Gazebo simulation execution and HTTP server must '
                    'share one principal and store'
                )
        self.orchestrator = orchestrator
        self.memory_store = orchestrator.memory_store
        self.conversation_store = orchestrator.conversation_store
        if (
            tool_gateway is not None
            and (
                tool_gateway.registry
                is not orchestrator.capability_registry
            )
        ):
            raise ValueError(
                'Tool Gateway and orchestrator must share one registry'
            )
        self.tool_gateway = tool_gateway or ToolGateway(
            orchestrator.capability_registry
        )
        self.speech_coordinator = speech_coordinator
        self.gazebo_simulation_execution_seam = (
            gazebo_simulation_execution_seam
        )
        self.max_request_bytes = max_request_bytes
        self.auth_token = auth_token
        self.allowed_user_id = normalized_allowed_user
        self.socket_timeout_seconds = socket_timeout_seconds
        self.requests_per_minute = requests_per_minute
        self.failed_auth_attempts_per_minute = (
            failed_auth_attempts_per_minute
        )
        self._capacity = threading.BoundedSemaphore(
            max_concurrent_requests
        )
        self._rate_lock = threading.Lock()
        self._request_times = deque()
        self._failed_auth_lock = threading.Lock()
        self._failed_auth_times = deque()
        super().__init__(address, AgentRequestHandler)
        with _GAZEBO_SERVER_BINDING_LOCK:
            _GAZEBO_SERVER_BINDINGS[self] = (
                orchestrator,
                orchestrator.conversation_store,
                normalized_allowed_user,
                auth_token,
                gazebo_simulation_execution_seam,
            )

    def attested_gazebo_simulation_execution_seam(
        self,
    ) -> Optional[GazeboSimulationExecutionSeam]:
        """Return only the immutable server-owned execution binding."""
        expected = None
        current = None
        try:
            with _GAZEBO_SERVER_BINDING_LOCK:
                expected = _GAZEBO_SERVER_BINDINGS.get(self)
            current = (
                object.__getattribute__(self, 'orchestrator'),
                object.__getattribute__(self, 'conversation_store'),
                object.__getattribute__(self, 'allowed_user_id'),
                object.__getattribute__(self, 'auth_token'),
                object.__getattribute__(
                    self,
                    'gazebo_simulation_execution_seam',
                ),
            )
        except Exception:
            expected = None
            current = None
        if (
            type(self) is not AgentHTTPServer
            or expected is None
            or current is None
            or len(expected) != 5
            or current[0] is not expected[0]
            or current[1] is not expected[1]
            or current[2] != expected[2]
            or current[3] != expected[3]
            or current[4] is not expected[4]
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_configuration_changed'
            )
        seam = expected[4]
        if seam is not None and not (
            GazeboSimulationExecutionSeam.matches_runtime(
                seam,
                expected[1],
                expected[2],
            )
        ):
            raise GazeboSimulationExecutionError(
                'gazebo_simulation_configuration_changed'
            )
        return seam

    def server_close(self) -> None:
        """Close adapter workers together with the HTTP listener."""
        try:
            super().server_close()
        finally:
            self.tool_gateway.close()

    def get_request(self) -> Tuple[Any, Any]:
        """Apply an inbound socket timeout before parsing HTTP."""
        request, client_address = super().get_request()
        request.settimeout(self.socket_timeout_seconds)
        return request, client_address

    def process_request(
        self,
        request: Any,
        client_address: Any,
    ) -> None:
        """Start at most the configured number of handler threads."""
        if not self._capacity.acquire(blocking=False):
            self._reject_overloaded_connection(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._capacity.release()
            raise

    def process_request_thread(
        self,
        request: Any,
        client_address: Any,
    ) -> None:
        """Release bounded capacity after the handler exits."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()

    def _reject_overloaded_connection(self, request: Any) -> None:
        body = (
            b'{"error":{"code":"server_busy",'
            b'"message":"Server is busy."}}'
        )
        response = (
            b'HTTP/1.1 503 Service Unavailable\r\n'
            b'Content-Type: application/json\r\n'
            b'Connection: close\r\n'
            b'Cache-Control: no-store\r\n'
            b'Content-Length: '
            + str(len(body)).encode('ascii')
            + b'\r\n\r\n'
            + body
        )
        try:
            request.sendall(response)
        finally:
            self.shutdown_request(request)

    def consume_rate_slot(self) -> bool:
        """Consume one global request slot in a fixed 60-second window."""
        current_time = time.monotonic()
        cutoff = current_time - 60.0
        with self._rate_lock:
            while (
                self._request_times
                and self._request_times[0] <= cutoff
            ):
                self._request_times.popleft()
            if len(self._request_times) >= self.requests_per_minute:
                return False
            self._request_times.append(current_time)
            return True

    def authorize(self, authorization: str) -> str:
        """Check one bearer within a separate failed-attempt budget."""
        if not self.auth_token:
            return AUTHORIZATION_ALLOWED
        expected = f'Bearer {self.auth_token}'
        try:
            authorized = hmac.compare_digest(
                authorization.encode('utf-8'),
                expected.encode('utf-8'),
            )
        except UnicodeError:
            authorized = False
        if authorized:
            return AUTHORIZATION_ALLOWED
        current_time = time.monotonic()
        cutoff = current_time - 60.0
        with self._failed_auth_lock:
            while (
                self._failed_auth_times
                and self._failed_auth_times[0] <= cutoff
            ):
                self._failed_auth_times.popleft()
            if (
                len(self._failed_auth_times)
                >= self.failed_auth_attempts_per_minute
            ):
                return AUTHORIZATION_RATE_LIMITED
            self._failed_auth_times.append(current_time)
            return AUTHORIZATION_REJECTED


class AgentRequestHandler(BaseHTTPRequestHandler):
    """Bounded request handler that never logs bodies or credentials."""

    server: AgentHTTPServer
    protocol_version = 'HTTP/1.1'

    def log_message(self, format_string: str, *args: Any) -> None:
        """Suppress BaseHTTPRequestHandler logs containing user paths."""
        del format_string, args

    def do_GET(self) -> None:
        """Serve health publicly and capabilities behind local auth."""
        if self.path == '/healthz':
            self._send_json(
                HTTPStatus.OK,
                {
                    'status': 'ok',
                    'service': 'malbut_agent_server',
                },
            )
            return
        if self.path != '/v1/tools/capabilities':
            self._send_error(
                HTTPStatus.NOT_FOUND,
                'not_found',
                'Endpoint not found.',
            )
            return
        if not self._authorize_request():
            return
        if not self.server.consume_rate_slot():
            self.close_connection = True
            self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                'rate_limited',
                'Request rate limit exceeded.',
            )
            return
        self._send_json(
            HTTPStatus.OK,
            self.server.tool_gateway.registry.to_dict(),
        )

    def do_POST(self) -> None:
        """Route JSON-only mutation and query endpoints."""
        try:
            gazebo_execution_path = (
                self.path == GAZEBO_SIMULATION_EXECUTION_PATH
            )
            gazebo_execution_seam = None
            if gazebo_execution_path:
                gazebo_execution_seam = (
                    AgentHTTPServer
                    .attested_gazebo_simulation_execution_seam(
                        self.server
                    )
                )
            if not self._authorize_request():
                return
            if not self.server.consume_rate_slot():
                self.close_connection = True
                self._send_error(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    'rate_limited',
                    'Request rate limit exceeded.',
                )
                return
            body = self._read_json_body()
            if self.path == '/v1/agent/respond':
                self._handle_agent(body)
            elif self.path == '/v1/tools/query':
                self._handle_tool_query(body)
            elif self.path == '/v1/conversations':
                self._handle_create_conversation(body)
            elif self.path == '/v1/conversations/get':
                self._handle_get_conversation(body)
            elif self.path == '/v1/conversations/reset':
                self._handle_reset_conversation(body)
            elif self.path == '/v1/conversations/close':
                self._handle_close_conversation(body)
            elif self.path == '/v1/conversations/delete':
                self._handle_delete_conversation(body)
            elif (
                gazebo_execution_path
                and gazebo_execution_seam is not None
            ):
                self._handle_gazebo_simulation_consume_and_prepare(
                    body,
                    gazebo_execution_seam,
                )
            elif (
                self.server.speech_coordinator is not None
                and self.path
                == '/v1/speech/scripted/sessions/open'
            ):
                self._handle_scripted_speech_open(body)
            elif (
                self.server.speech_coordinator is not None
                and self.path == '/v1/speech/scripted/transcripts'
            ):
                self._handle_scripted_speech_transcript(body)
            elif (
                self.server.speech_coordinator is not None
                and self.path
                == '/v1/speech/scripted/trusted-result-tts/claim'
            ):
                self._handle_scripted_trusted_result_tts_claim(body)
            elif (
                self.server.speech_coordinator is not None
                and self.path
                == '/v1/speech/scripted/trusted-result-tts/terminal'
            ):
                self._handle_scripted_trusted_result_tts_terminal(body)
            elif (
                self.server.speech_coordinator is not None
                and self.path == '/v1/speech/scripted/tts/terminal'
            ):
                self._handle_scripted_tts_terminal(body)
            elif (
                self.server.speech_coordinator is not None
                and self.path
                == '/v1/speech/scripted/sessions/close'
            ):
                self._handle_scripted_speech_close(body)
            else:
                self._send_error(
                    HTTPStatus.NOT_FOUND,
                    'not_found',
                    'Endpoint not found.',
                )
        except GatewayConflictError as error:
            self._send_error(
                HTTPStatus.CONFLICT,
                'request_conflict',
                str(error),
            )
        except ConversationNotFoundError as error:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                'conversation_not_found',
                str(error),
            )
        except ConversationChangedError as error:
            self._send_error(
                HTTPStatus.CONFLICT,
                'conversation_changed',
                str(error),
            )
        except ConversationConflictError as error:
            self._send_error(
                HTTPStatus.CONFLICT,
                'conversation_conflict',
                str(error),
            )
        except ConversationStateError as error:
            self._send_error(
                HTTPStatus.CONFLICT,
                'conversation_state',
                str(error),
            )
        except ExpiredDecisionError as error:
            self._send_error(
                HTTPStatus.CONFLICT,
                'expired_decision',
                str(error),
            )
        except MemoryChangedError as error:
            self._send_error(
                HTTPStatus.CONFLICT,
                'memory_changed',
                str(error),
            )
        except ValidationError as error:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                'validation_error',
                str(error),
            )
        except ProviderError:
            self._send_error(
                HTTPStatus.BAD_GATEWAY,
                'provider_error',
                'The model provider did not return a valid response.',
            )
        except GazeboSimulationExecutionError as error:
            status = (
                HTTPStatus.CONFLICT
                if error.code == 'gazebo_simulation_not_authorized'
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._send_error(
                status,
                error.code,
                'Gazebo simulation execution is unavailable.',
            )
        except json.JSONDecodeError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                'invalid_json',
                'Request body is not valid JSON.',
            )
        except RequestBodyError as error:
            self.close_connection = True
            self._send_error(error.status, error.code, error.message)
        except TimeoutError:
            self.close_connection = True
            self._send_error(
                HTTPStatus.REQUEST_TIMEOUT,
                'request_timeout',
                'Request body was not received in time.',
            )
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                'internal_error',
                'Unexpected server error.',
            )

    def _authorize_request(self) -> bool:
        """Authenticate once and emit a content-free bounded failure."""
        authorization = self.headers.get('Authorization', '')
        result = self.server.authorize(authorization)
        if result == AUTHORIZATION_ALLOWED:
            return True
        self.close_connection = True
        if result == AUTHORIZATION_RATE_LIMITED:
            self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                'auth_rate_limited',
                'Authentication attempt rate limit exceeded.',
            )
            return False
        self._send_error(
            HTTPStatus.UNAUTHORIZED,
            'unauthorized',
            'Authentication is required.',
        )
        return False

    def _read_json_body(self) -> Dict[str, Any]:
        content_type = self.headers.get('Content-Type', '')
        if content_type.split(';', 1)[0].strip() != 'application/json':
            raise RequestBodyError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                'unsupported_media_type',
                'Content-Type must be application/json.',
            )
        raw_length = self.headers.get('Content-Length')
        if raw_length is None:
            raise RequestBodyError(
                HTTPStatus.LENGTH_REQUIRED,
                'length_required',
                'Content-Length is required.',
            )
        try:
            length = int(raw_length)
        except ValueError as error:
            raise RequestBodyError(
                HTTPStatus.BAD_REQUEST,
                'invalid_content_length',
                'Content-Length is invalid.',
            ) from error
        if length < 1:
            raise RequestBodyError(
                HTTPStatus.BAD_REQUEST,
                'empty_body',
                'Request body must not be empty.',
            )
        if length > self.server.max_request_bytes:
            raise RequestBodyError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                'body_too_large',
                'Request body exceeds the configured limit.',
            )
        try:
            decoded = self.rfile.read(length).decode('utf-8')
        except UnicodeDecodeError as error:
            raise RequestBodyError(
                HTTPStatus.BAD_REQUEST,
                'invalid_encoding',
                'Request body must be valid UTF-8.',
            ) from error

        def reject_constant(value: str) -> None:
            raise ValidationError(
                f'non-finite JSON number is not allowed: {value}'
            )

        body = json.loads(decoded, parse_constant=reject_constant)
        if not isinstance(body, dict):
            raise ValidationError('request body must be an object')
        return body

    def _handle_agent(self, body: Dict[str, Any]) -> None:
        request = AgentRequest.from_dict(body)
        self._require_allowed_user(request.user_id)
        result = self.server.orchestrator.handle(request)
        self._send_json(HTTPStatus.OK, result.to_dict())

    def _handle_tool_query(self, body: Dict[str, Any]) -> None:
        """Run only read-only or explicit side-effect-free simulations."""
        query = ToolQuery.from_dict(body)
        self._require_allowed_user(query.user_id)
        result, cached = (
            self.server.tool_gateway.query_with_cache_state(query)
        )
        self._send_json(
            HTTPStatus.OK,
            result.to_dict(cached=cached),
        )

    def _handle_create_conversation(
        self,
        body: Dict[str, Any],
    ) -> None:
        allowed = {'user_id', 'conversation_id'}
        self._reject_unknown_fields(
            body,
            allowed,
            'conversation create',
        )
        user_id = validate_user_id(body.get('user_id'))
        self._require_allowed_user(user_id)
        conversation_id = body.get('conversation_id')
        if conversation_id is not None:
            conversation_id = validate_conversation_id(
                conversation_id
            )
        session = self.server.conversation_store.create(
            user_id,
            conversation_id,
        )
        self._send_json(
            HTTPStatus.CREATED,
            {'conversation': session.to_dict()},
        )

    def _handle_get_conversation(
        self,
        body: Dict[str, Any],
    ) -> None:
        allowed = {'user_id', 'conversation_id', 'limit'}
        self._reject_unknown_fields(
            body,
            allowed,
            'conversation get',
        )
        user_id, conversation_id = (
            self._validated_conversation_identity(body)
        )
        snapshot = self.server.conversation_store.snapshot(
            user_id,
            conversation_id,
            limit=body.get('limit', 100),
        )
        messages = [
            message
            for turn in snapshot.turns
            for message in turn.to_messages()
        ]
        self._send_json(
            HTTPStatus.OK,
            {
                'conversation': snapshot.session.to_dict(),
                'turns': [
                    turn.to_dict()
                    for turn in snapshot.turns
                ],
                'messages': messages,
                'summary': (
                    snapshot.summary.to_dict()
                    if snapshot.summary is not None
                    else None
                ),
            },
        )

    def _handle_reset_conversation(
        self,
        body: Dict[str, Any],
    ) -> None:
        self._reject_unknown_fields(
            body,
            {'user_id', 'conversation_id'},
            'conversation reset',
        )
        user_id, conversation_id = (
            self._validated_conversation_identity(body)
        )
        session = self.server.conversation_store.reset(
            user_id,
            conversation_id,
        )
        self._send_json(
            HTTPStatus.OK,
            {
                'conversation': session.to_dict(),
                'turns': [],
                'messages': [],
                'summary': None,
            },
        )

    def _handle_close_conversation(
        self,
        body: Dict[str, Any],
    ) -> None:
        self._reject_unknown_fields(
            body,
            {'user_id', 'conversation_id'},
            'conversation close',
        )
        user_id, conversation_id = (
            self._validated_conversation_identity(body)
        )
        session = self.server.conversation_store.close_session(
            user_id,
            conversation_id,
        )
        self._send_json(
            HTTPStatus.OK,
            {'conversation': session.to_dict()},
        )

    def _handle_delete_conversation(
        self,
        body: Dict[str, Any],
    ) -> None:
        self._reject_unknown_fields(
            body,
            {'user_id', 'conversation_id'},
            'conversation delete',
        )
        user_id, conversation_id = (
            self._validated_conversation_identity(body)
        )
        deleted = self.server.conversation_store.delete(
            user_id,
            conversation_id,
        )
        if not deleted:
            raise ConversationNotFoundError(
                'conversation was not found'
            )
        self._send_json(
            HTTPStatus.OK,
            {
                'deleted': True,
                'conversation_id': conversation_id,
            },
        )

    def _handle_gazebo_simulation_consume_and_prepare(
        self,
        body: Dict[str, Any],
        seam: GazeboSimulationExecutionSeam,
    ) -> None:
        """Explicitly consume one approval and attempt one UDS prepare."""
        self._reject_unknown_fields(
            body,
            {'confirmation_request_id'},
            'Gazebo simulation consume and prepare',
        )
        result = GazeboSimulationExecutionSeam.consume_and_prepare(
            seam,
            body.get('confirmation_request_id'),
        )
        self._send_json(
            HTTPStatus.OK,
            GazeboSimulationExecutionResult.to_public_dict(result),
        )

    def _handle_scripted_speech_open(
        self,
        body: Dict[str, Any],
    ) -> None:
        """Open one authenticated, server-identity-bound test session."""
        self._reject_unknown_fields(
            body,
            {'speech_session_id', 'conversation_id'},
            'scripted speech session open',
        )
        binding = TrustedSpeechBinding.from_dict(
            {
                'user_id': self.server.allowed_user_id,
                'speaker_id': SCRIPTED_SPEAKER_ID,
                'speech_session_id': body.get('speech_session_id'),
                'conversation_id': body.get('conversation_id'),
                'source': SCRIPTED_SOURCE,
            }
        )
        coordinator = self._scripted_speech_coordinator()
        result = coordinator.open_session(binding)
        self._send_json(
            HTTPStatus.OK,
            self._scripted_speech_response(
                {
                    'binding': binding.to_dict(),
                    'result': result.to_dict(),
                }
            ),
        )

    def _handle_scripted_speech_transcript(
        self,
        body: Dict[str, Any],
    ) -> None:
        """Process text only; never accept RobotState or Tool authority."""
        allowed = {
            'schema_version',
            'utterance_id',
            'speech_session_id',
            'conversation_id',
            'sequence',
            'capture_epoch',
            'source_timestamp_ns',
            'text',
            'confidence',
            'is_final',
            'capture_origin',
            'audio_metadata',
        }
        self._reject_unknown_fields(
            body,
            allowed,
            'scripted speech transcript',
        )
        event = SpeechTranscriptEvent.from_dict(
            {
                **body,
                'speaker_id': SCRIPTED_SPEAKER_ID,
                'source': SCRIPTED_SOURCE,
            }
        )
        capabilities = (
            self.server.orchestrator.capability_registry.to_dict()[
                'capabilities'
            ]
        )
        available_tools = tuple(
            item['name']
            for item in capabilities
            if item['available_for_proposal'] is True
        )
        coordinator = self._scripted_speech_coordinator()
        self._require_scripted_speech_session_owner(
            coordinator,
            event.speech_session_id,
        )
        result = coordinator.handle_transcript(
            event,
            # Deliberately omit RobotState.  HTTP text is not a trusted
            # robot-state or Tool-capability source; the advertised Tool
            # subset comes only from the server registry.
            available_tools=available_tools,
        )
        self._send_json(
            HTTPStatus.OK,
            self._scripted_speech_response(
                {'result': result.to_dict()}
            ),
        )

    def _handle_scripted_tts_terminal(
        self,
        body: Dict[str, Any],
    ) -> None:
        """Acknowledge text playback so the next scripted turn can run."""
        self._reject_unknown_fields(
            body,
            {'speech_session_id', 'tts_request_id'},
            'scripted TTS terminal',
        )
        speech_session_id = body.get('speech_session_id')
        coordinator = self._scripted_speech_coordinator()
        self._require_scripted_speech_session_owner(
            coordinator,
            speech_session_id,
        )
        result = coordinator.mark_tts_terminal(
            speech_session_id,
            body.get('tts_request_id'),
        )
        self._send_json(
            HTTPStatus.OK,
            self._scripted_speech_response(
                {'result': result.to_dict()}
            ),
        )

    def _handle_scripted_trusted_result_tts_claim(
        self,
        body: Dict[str, Any],
    ) -> None:
        """Explicitly claim one durable simulation notification."""
        self._reject_unknown_fields(
            body,
            {
                'speech_session_id',
                'claim_request_id',
                'lease_seconds',
            },
            'scripted trusted result TTS claim',
        )
        speech_session_id = body.get('speech_session_id')
        coordinator = self._scripted_speech_coordinator()
        self._require_scripted_speech_session_owner(
            coordinator,
            speech_session_id,
        )
        result = (
            coordinator.claim_trusted_result_tts(
                speech_session_id,
                body.get('claim_request_id'),
                body.get('lease_seconds', 30),
            )
        )
        self._send_json(
            HTTPStatus.OK,
            self._scripted_speech_response(
                {'result': result.to_dict()}
            ),
        )

    def _handle_scripted_trusted_result_tts_terminal(
        self,
        body: Dict[str, Any],
    ) -> None:
        """ACK downstream terminal state, never audible playback proof."""
        self._reject_unknown_fields(
            body,
            {
                'speech_session_id',
                'tts_request_id',
                'terminal_request_id',
            },
            'scripted trusted result TTS terminal',
        )
        speech_session_id = body.get('speech_session_id')
        coordinator = self._scripted_speech_coordinator()
        self._require_scripted_speech_session_owner(
            coordinator,
            speech_session_id,
        )
        result = (
            coordinator.mark_trusted_result_tts_terminal(
                speech_session_id,
                body.get('tts_request_id'),
                body.get('terminal_request_id'),
            )
        )
        self._send_json(
            HTTPStatus.OK,
            self._scripted_speech_response(
                {'result': result.to_dict()}
            ),
        )

    def _handle_scripted_speech_close(
        self,
        body: Dict[str, Any],
    ) -> None:
        """Close an in-memory voice binding and its conversation session."""
        self._reject_unknown_fields(
            body,
            {'speech_session_id', 'control_id'},
            'scripted speech session close',
        )
        speech_session_id = body.get('speech_session_id')
        coordinator = self._scripted_speech_coordinator()
        self._require_scripted_speech_session_owner(
            coordinator,
            speech_session_id,
        )
        result = coordinator.close_session(
            speech_session_id,
            body.get('control_id'),
        )
        self._send_json(
            HTTPStatus.OK,
            self._scripted_speech_response(
                {'result': result.to_dict()}
            ),
        )

    def _scripted_speech_coordinator(
        self,
    ) -> SpeechConversationCoordinator:
        """Return the opt-in dependency after route-level gating."""
        coordinator = self.server.speech_coordinator
        if coordinator is None:
            raise RuntimeError('scripted speech runtime is disabled')
        return coordinator

    def _require_scripted_speech_session_owner(
        self,
        coordinator: SpeechConversationCoordinator,
        speech_session_id: Any,
    ) -> None:
        """Hide unknown and cross-user speech sessions behind one result."""
        if not coordinator.is_speech_session_bound_to_user(
            speech_session_id,
            self.server.allowed_user_id,
        ):
            raise ConversationNotFoundError(
                'speech session was not found'
            )

    @staticmethod
    def _scripted_speech_response(
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Label evidence so it cannot be mistaken for physical input."""
        return {
            'runtime': SCRIPTED_SPEECH_RUNTIME,
            'physical_authority': False,
            'physical_audio_verified': False,
            **payload,
        }

    def _validated_conversation_identity(
        self,
        body: Dict[str, Any],
    ) -> Tuple[str, str]:
        user_id = validate_user_id(body.get('user_id'))
        self._require_allowed_user(user_id)
        conversation_id = validate_conversation_id(
            body.get('conversation_id')
        )
        return user_id, conversation_id

    def _reject_unknown_fields(
        self,
        body: Dict[str, Any],
        allowed: set,
        operation: str,
    ) -> None:
        unknown = set(body) - allowed
        if unknown:
            names = ', '.join(sorted(unknown))
            raise ValidationError(
                f'unknown {operation} fields: {names}'
            )

    def _require_allowed_user(self, user_id: str) -> None:
        if not hmac.compare_digest(
            user_id.encode('utf-8'),
            self.server.allowed_user_id.encode('utf-8'),
        ):
            raise ValidationError(
                'user_id does not match the server identity'
            )

    def _send_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        self._send_json(
            status,
            {
                'error': {
                    'code': code,
                    'message': message,
                },
            },
        )

    def _send_json(
        self,
        status: HTTPStatus,
        payload: Dict[str, Any],
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
        self.send_response(int(status))
        self.send_header(
            'Content-Type',
            'application/json; charset=utf-8',
        )
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


class RequestBodyError(Exception):
    """HTTP-aware request parsing error."""

    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> None:
        """Create a safe client-facing parse error."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def make_server(
    host: str,
    port: int,
    orchestrator: AgentOrchestrator,
    max_request_bytes: int = 65536,
    auth_token: str = '',
    allowed_user_id: str = 'local-user',
    max_concurrent_requests: int = 8,
    requests_per_minute: int = 60,
    failed_auth_attempts_per_minute: int = (
        DEFAULT_FAILED_AUTH_ATTEMPTS_PER_MINUTE
    ),
    socket_timeout_seconds: int = 10,
    tool_gateway: Optional[ToolGateway] = None,
    speech_coordinator: Optional[
        SpeechConversationCoordinator
    ] = None,
    gazebo_simulation_execution_seam: Optional[
        GazeboSimulationExecutionSeam
    ] = None,
) -> AgentHTTPServer:
    """Build a server without starting its event loop."""
    return AgentHTTPServer(
        (host, port),
        orchestrator=orchestrator,
        max_request_bytes=max_request_bytes,
        auth_token=auth_token,
        allowed_user_id=allowed_user_id,
        max_concurrent_requests=max_concurrent_requests,
        requests_per_minute=requests_per_minute,
        failed_auth_attempts_per_minute=(
            failed_auth_attempts_per_minute
        ),
        socket_timeout_seconds=socket_timeout_seconds,
        tool_gateway=tool_gateway,
        speech_coordinator=speech_coordinator,
        gazebo_simulation_execution_seam=(
            gazebo_simulation_execution_seam
        ),
    )
