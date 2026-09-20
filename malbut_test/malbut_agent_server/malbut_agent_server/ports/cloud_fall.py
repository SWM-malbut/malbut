"""Cloud-only runtime port. No local VLM fallback or evaluation dependency."""

from typing import Protocol

from malbut_agent_server.domain.fall_monitoring import (
    CloudFallReply,
    CloudFallRequest,
)


class CloudFallProviderError(RuntimeError):
    """Bounded, credential-free operational error; never a normal assessment."""

    def __init__(self, code):
        if code not in {
            'cloud_invalid_response', 'cloud_input_invalid', 'cloud_timeout',
            'cloud_auth_required', 'cloud_payment_required', 'cloud_quota_exhausted',
            'cloud_http_error', 'cloud_transport_error',
        }:
            code = 'cloud_transport_error'
        self.code = code
        super().__init__(code)


class CloudFallProvider(Protocol):
    # Adapters must also enforce HTTPS/approved endpoints and bounded I/O.
    execution_target: str

    async def analyze(self, request: CloudFallRequest) -> CloudFallReply:
        """One attempt only; propagate errors, never silently retry locally.

        Implementations must be nonblocking and honor cancellation. Synchronous
        SDKs in background threads are NOT sufficient to abort an upload.
        """
        ...
