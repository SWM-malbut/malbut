"""HTTP transport helpers for the OpenAI Responses API adapter."""

import http.client
import json
from typing import Any, Callable, Dict, Optional
import urllib.error
import urllib.request

from malbut_agent_server.providers.base import ProviderError
from malbut_agent_server.providers.http_pool import (
    HTTPConnectionPool,
    uses_environment_proxy,
)
from malbut_agent_server.providers.openai.constants import (
    MAX_PROVIDER_RESPONSE_BYTES,
    Transport,
)
from malbut_agent_server.providers.openai.parsing import decode_response_body
from malbut_agent_server.providers.reliable import (
    NormalizedProviderError,
    ProviderFailureCode,
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent Authorization headers from following redirects."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        """Return no follow-up request for every redirect status."""
        del request, file_pointer, code, message, headers, new_url
        return None


def _insufficient_quota(error: urllib.error.HTTPError) -> bool:
    """Whether a 429 means "no credit left" rather than "slow down".

    Only the error code is read; the body is never kept or logged.
    """
    try:
        body = json.loads(error.read(4097)[:4096] or b'{}')
    except (OSError, ValueError):
        return False
    detail = body.get('error') if isinstance(body, dict) else None
    return isinstance(detail, dict) and 'insufficient_quota' in (
        detail.get('code'), detail.get('type'),
    )


def encode_json_payload(payload: Dict[str, Any]) -> bytes:
    """Encode JSON as ASCII UTF-8 bytes to keep image-heavy buffers compact."""
    # Keep image-heavy JSON in a one-byte string even with Unicode prompts.
    # JSON escapes preserve text while avoiding a widened base64 buffer.
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(',', ':'),
    ).encode('utf-8')


def urllib_transport(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: float,
    *,
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    decode_body: Optional[Callable[[bytes], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Execute one non-pooled POST request without following redirects."""
    request = urllib.request.Request(
        url,
        data=encode_json_payload(payload),
        headers=headers,
        method='POST',
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(
            request,
            timeout=timeout_seconds,
        ) as response:
            response_body = response.read(max_response_bytes + 1)
    except urllib.error.HTTPError as error:
        if error.code == 429 and _insufficient_quota(error):
            raise NormalizedProviderError(
                ProviderFailureCode.QUOTA
            ) from error
        raise ProviderError(
            'OpenAI request failed with HTTP status '
            f'{error.code}'
        ) from error
    except urllib.error.URLError as error:
        raise ProviderError(
            'OpenAI request failed due to a network error'
        ) from error
    if decode_body is not None:
        return decode_body(response_body)
    return decode_response_body(
        response_body,
        max_response_bytes=max_response_bytes,
    )


def pooled_post(
    http_pool: HTTPConnectionPool,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: float,
    *,
    max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    fallback_transport: Transport,
    decode_body: Callable[[bytes], Dict[str, Any]],
    force_fallback: bool = False,
    stream_turn: Any = None,
    observer: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Send a JSON POST over a keep-alive pool with proxy and SSE support."""
    streaming = stream_turn is not None or observer is not None
    try:
        http_pool.check_open()
        if force_fallback or uses_environment_proxy(url):
            if streaming:
                payload = dict(payload)
                payload.pop('stream', None)
            return fallback_transport(url, headers, payload, timeout_seconds)
        reader = None
        if streaming:
            from malbut_agent_server.providers.response_stream import read_response_stream

            def _read_stream(response: Any, limit: int) -> Dict[str, Any]:
                if observer is not None:
                    observer('headers')
                return read_response_stream(response, limit, stream_turn, observer)

            reader = _read_stream
        response_body = http_pool.post(
            url,
            headers,
            encode_json_payload(payload),
            timeout_seconds,
            max_response_bytes,
            reader=reader,
        )
    except urllib.error.HTTPError as error:
        try:
            if error.code == 429 and _insufficient_quota(error):
                raise NormalizedProviderError(ProviderFailureCode.QUOTA) from error
            raise ProviderError(
                f'OpenAI request failed with HTTP status {error.code}'
            ) from error
        finally:
            error.close()
    except http.client.HTTPException as error:
        raise ProviderError('OpenAI request failed due to a network error') from (
            urllib.error.URLError(error)
        )
    except OSError as error:
        # A disconnected keep-alive socket may fail after the server read
        # the POST. Leave retry decisions to the existing reliability layer.
        raise ProviderError('OpenAI request failed due to a network error') from error
    if streaming:
        return response_body
    return decode_body(response_body)
