"""Small JSON HTTP server for the Malbut agent boundary."""

import hmac
import json
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)
from typing import Any, Dict, Tuple

from malbut_agent_server.conversation import (
    ConversationChangedError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStateError,
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


class AgentHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying explicit service dependencies."""

    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        orchestrator: AgentOrchestrator,
        max_request_bytes: int,
        auth_token: str = '',
        allowed_user_id: str = 'local-user',
        max_concurrent_requests: int = 8,
        requests_per_minute: int = 60,
        socket_timeout_seconds: int = 10,
    ) -> None:
        """Attach runtime services before binding the HTTP listener."""
        if max_request_bytes < 1:
            raise ValueError('max_request_bytes must be positive')
        if max_concurrent_requests < 1:
            raise ValueError(
                'max_concurrent_requests must be positive'
            )
        if requests_per_minute < 1:
            raise ValueError('requests_per_minute must be positive')
        if socket_timeout_seconds < 1:
            raise ValueError(
                'socket_timeout_seconds must be positive'
            )
        self.orchestrator = orchestrator
        self.memory_store = orchestrator.memory_store
        self.conversation_store = orchestrator.conversation_store
        self.max_request_bytes = max_request_bytes
        self.auth_token = auth_token
        self.allowed_user_id = validate_user_id(allowed_user_id)
        self.socket_timeout_seconds = socket_timeout_seconds
        self.requests_per_minute = requests_per_minute
        self._capacity = threading.BoundedSemaphore(
            max_concurrent_requests
        )
        self._rate_lock = threading.Lock()
        self._request_times = deque()
        super().__init__(address, AgentRequestHandler)

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


class AgentRequestHandler(BaseHTTPRequestHandler):
    """Bounded request handler that never logs bodies or credentials."""

    server: AgentHTTPServer
    protocol_version = 'HTTP/1.1'

    def log_message(self, format_string: str, *args: Any) -> None:
        """Suppress BaseHTTPRequestHandler logs containing user paths."""
        del format_string, args

    def do_GET(self) -> None:
        """Serve only the health endpoint."""
        if self.path != '/healthz':
            self._send_error(
                HTTPStatus.NOT_FOUND,
                'not_found',
                'Endpoint not found.',
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                'status': 'ok',
                'service': 'malbut_agent_server',
            },
        )

    def do_POST(self) -> None:
        """Route JSON-only mutation and query endpoints."""
        try:
            if not self._authorized():
                self.close_connection = True
                self._send_error(
                    HTTPStatus.UNAUTHORIZED,
                    'unauthorized',
                    'Authentication is required.',
                )
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
            else:
                self._send_error(
                    HTTPStatus.NOT_FOUND,
                    'not_found',
                    'Endpoint not found.',
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

    def _authorized(self) -> bool:
        if not self.server.auth_token:
            return True
        authorization = self.headers.get('Authorization', '')
        expected = f'Bearer {self.server.auth_token}'
        try:
            return hmac.compare_digest(
                authorization.encode('utf-8'),
                expected.encode('utf-8'),
            )
        except UnicodeError:
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
    socket_timeout_seconds: int = 10,
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
        socket_timeout_seconds=socket_timeout_seconds,
    )
