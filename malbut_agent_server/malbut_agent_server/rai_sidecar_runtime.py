"""
One-request RAI runtime boundary for the isolated sidecar process.

This module intentionally has no module-level RAI or ROS imports.  A trusted
sidecar entry point supplies ``runtime_factory`` and is the only place where
``rai-core`` is loaded.  The factory adapts the selected RAI graph to the
small ``propose`` contract below; Malbut tools are never executed here.
"""

from __future__ import annotations

import importlib
from importlib import metadata as importlib_metadata
import json
import logging
import os
import sys
from typing import (
    Any,
    BinaryIO,
    Callable,
    Literal,
    Optional,
    Protocol,
    Sequence,
)
import warnings

from malbut_agent_server.rai_sidecar_protocol import (
    ActionProposal,
    MAX_REQUEST_BYTES,
    ProposalRequest,
    ProposalResponse,
    RaiSidecarProtocolError,
    RuntimeErrorResponse,
    SidecarOutput,
    TextReply,
    decode_request,
    encode_response,
)


RAI_CORE_DISTRIBUTION = 'rai-core'
REQUIRED_RAI_CORE_VERSION = '2.12.1'


class RaiRuntimeConfigurationError(RuntimeError):
    """Content-free failure while creating the isolated RAI adapter."""

    def __init__(self) -> None:
        """Do not retain dependency, credential, or configuration details."""
        super().__init__('RAI sidecar runtime is unavailable')


class RaiProposalRuntime(Protocol):
    """Minimal adapter implemented by the trusted RAI sidecar wrapper."""

    model: str

    def propose(self, request: ProposalRequest) -> SidecarOutput:
        """Return one text reply or one untrusted action proposal."""


RuntimeFactory = Callable[[Any], RaiProposalRuntime]


class _RaiStructuredProposalRuntime:
    """Adapt RAI's structured-output graph to one Malbut proposal."""

    def __init__(
        self,
        *,
        rai_langchain: Any,
        llm: Any,
        human_message: Callable[..., Any],
        pydantic_module: Any,
        strict_config: Any,
        model: str,
    ) -> None:
        self._rai_langchain = rai_langchain
        self._llm = llm
        self._human_message = human_message
        self._pydantic = pydantic_module
        self._strict_config = strict_config
        self.model = model

    def propose(self, request: ProposalRequest) -> SidecarOutput:
        """Invoke one non-tool-executing RAI graph and normalize its output."""
        tool_projection = json.dumps(
            list(request.tools),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        system_prompt = (
            f'{request.instructions}\n\n'
            'Return exactly one structured TextReply or ActionProposal. '
            'For kind=action_proposal, response_type must be null. For '
            'kind=text_reply, tool_name, arguments, and expires_in_ms must '
            'all be null. Never mix fields from the two result kinds. '
            'The following tools are proposals only; never execute them. '
            'Do not create approval, identity, revision, coordinates, or '
            'execution fields. In the structured arguments object, set '
            'properties belonging to other tools to null. Allowed neutral '
            'ToolSpec projection:\n'
            f'{tool_projection}'
        )
        graph = self._rai_langchain.create_structured_output_runnable(
            llm=self._llm,
            structured_output=self._output_model(request),
            system_prompt=system_prompt,
        )
        state = graph.invoke({
            'messages': [
                self._human_message(content=request.model_input),
            ],
        })
        if type(state) is not dict:
            raise ValueError('invalid structured state')
        messages = state.get('messages')
        if type(messages) is not list or not messages:
            raise ValueError('invalid structured state')
        structured = messages[-1]
        if type(structured) is not dict or (
            structured.get('parsing_error') is not None
        ):
            raise ValueError('invalid structured output')
        parsed = structured.get('parsed')
        dump = getattr(parsed, 'model_dump', None)
        if not callable(dump):
            raise ValueError('invalid structured output')
        payload = dump(mode='python')
        return self._normalize_payload(payload, request)

    def _output_model(self, request: ProposalRequest) -> Any:
        """Build a strict schema whose arguments have no free-form keys."""
        property_names = sorted({
            name
            for tool in request.tools
            for name in tool['parameters']['properties']
        })
        argument_fields = {
            name: (Optional[str], ...)
            for name in property_names
        }
        arguments_model = self._pydantic.create_model(
            'MalbutRaiArgumentsV1',
            __config__=self._strict_config,
            **argument_fields,
        )
        return self._pydantic.create_model(
            'MalbutRaiProposalV1',
            __config__=self._strict_config,
            kind=(Literal['text_reply', 'action_proposal'], ...),
            response_type=(
                Optional[
                    Literal['message', 'clarification', 'refusal']
                ],
                ...,
            ),
            message=(str, ...),
            reason=(str, ...),
            confidence=(Optional[float], ...),
            tool_name=(Optional[str], ...),
            arguments=(Optional[arguments_model], ...),
            expires_in_ms=(Optional[int], ...),
        )

    @staticmethod
    def _normalize_payload(
        payload: Any,
        request: ProposalRequest,
    ) -> SidecarOutput:
        if type(payload) is not dict:
            raise ValueError('invalid structured output')
        expected = {
            'kind',
            'response_type',
            'message',
            'reason',
            'confidence',
            'tool_name',
            'arguments',
            'expires_in_ms',
        }
        if set(payload) != expected:
            raise ValueError('invalid structured output')
        if payload['kind'] == 'text_reply':
            if any(
                payload[name] is not None
                for name in ('tool_name', 'arguments', 'expires_in_ms')
            ):
                raise ValueError('mixed structured output')
            return TextReply(
                response_type=payload['response_type'],
                message=payload['message'],
                reason=payload['reason'],
                confidence=payload['confidence'],
            )
        if payload['kind'] == 'action_proposal':
            if payload['response_type'] is not None:
                raise ValueError('mixed structured output')
            arguments = payload['arguments']
            if type(arguments) is not dict:
                raise ValueError('invalid structured arguments')
            matching = [
                tool for tool in request.tools
                if tool['name'] == payload['tool_name']
            ]
            if len(matching) != 1:
                raise ValueError('unknown structured tool')
            properties = matching[0]['parameters']['properties']
            if any(
                value is not None and name not in properties
                for name, value in arguments.items()
            ):
                raise ValueError('mixed structured arguments')
            projected_arguments = {
                name: arguments.get(name)
                for name in properties
            }
            return ActionProposal(
                tool_name=payload['tool_name'],
                arguments=projected_arguments,
                message=payload['message'],
                reason=payload['reason'],
                confidence=payload['confidence'],
                expires_in_ms=payload['expires_in_ms'],
            )
        raise ValueError('invalid structured output')


def _default_runtime_factory(rai_langchain: Any) -> RaiProposalRuntime:
    """Build the pinned RAI structured agent with an explicit OpenAI LLM."""
    model = os.environ.get('MALBUT_RAI_MODEL')
    if type(model) is not str or not model or len(model) > 256 or any(
        ord(character) < 32 or ord(character) == 127
        for character in model
    ):
        raise RaiRuntimeConfigurationError()
    try:
        openai_module = importlib.import_module('langchain_openai')
        messages_module = importlib.import_module(
            'langchain_core.messages'
        )
        pydantic_module = importlib.import_module('pydantic')
        invocation_helpers = importlib.import_module(
            'rai.agents.langchain.invocation_helpers'
        )
        invocation_helpers.get_tracing_callbacks = lambda: []
        strict_config = pydantic_module.ConfigDict(
            extra='forbid',
            strict=True,
        )
        llm = openai_module.ChatOpenAI(
            model=model,
            temperature=0,
            max_retries=0,
        )
        return _RaiStructuredProposalRuntime(
            rai_langchain=rai_langchain,
            llm=llm,
            human_message=messages_module.HumanMessage,
            pydantic_module=pydantic_module,
            strict_config=strict_config,
            model=model,
        )
    except RaiRuntimeConfigurationError:
        raise
    except Exception:
        raise RaiRuntimeConfigurationError() from None


def _disable_dependency_output() -> None:
    """Prevent dependency diagnostics from exposing prompt or Tool data."""
    logging.disable(logging.CRITICAL)
    warnings.filterwarnings('ignore')


def _validate_rai_core_distribution() -> None:
    """Require the exact reviewed rai-core distribution before importing it."""
    try:
        installed_version = importlib_metadata.version(
            RAI_CORE_DISTRIBUTION
        )
    except Exception:
        raise RaiRuntimeConfigurationError() from None
    if installed_version != REQUIRED_RAI_CORE_VERSION:
        raise RaiRuntimeConfigurationError()


def _validate_isolated_interpreter() -> None:
    """Require the sidecar to run inside a real Python virtual environment."""
    if sys.prefix == sys.base_prefix:
        raise RaiRuntimeConfigurationError()


def create_runtime(
    runtime_factory: RuntimeFactory | None = None,
) -> RaiProposalRuntime:
    """
    Lazy-load rai-core and build one explicitly configured adapter.

    An optional caller-owned factory receives only RAI's LangChain core
    module.  The default factory creates an explicit, zero-retry OpenAI chat
    model only when ``MALBUT_RAI_MODEL`` is present.  Every adapter must expose
    ``model`` plus ``propose``.  There is no automatic vendor selection and no
    generic robot Tool registration.
    """
    if runtime_factory is not None and not callable(runtime_factory):
        raise RaiRuntimeConfigurationError()
    _disable_dependency_output()
    _validate_isolated_interpreter()
    _validate_rai_core_distribution()
    try:
        rai_langchain = importlib.import_module(
            'rai.agents.langchain.core'
        )
        selected_factory = runtime_factory or _default_runtime_factory
        runtime = selected_factory(rai_langchain)
    except Exception:
        raise RaiRuntimeConfigurationError() from None
    if not callable(getattr(runtime, 'propose', None)):
        raise RaiRuntimeConfigurationError()
    model = getattr(runtime, 'model', None)
    if type(model) is not str or not model or len(model) > 256:
        raise RaiRuntimeConfigurationError()
    return runtime


def _proposal_is_bound_to_request(
    request: ProposalRequest,
    output: SidecarOutput,
) -> bool:
    """Ensure an action can name only one schema projected in the request."""
    if type(output) is TextReply:
        return True
    if type(output) is not ActionProposal:
        return False
    matching = [
        tool for tool in request.tools
        if tool['name'] == output.tool_name
    ]
    if len(matching) != 1:
        return False
    parameters = matching[0]['parameters']
    arguments = output.arguments_dict()
    properties = parameters['properties']
    if parameters['additionalProperties'] is False and (
        set(arguments) - set(properties)
    ):
        return False
    if set(parameters['required']) - set(arguments):
        return False
    for name, value in arguments.items():
        schema = properties.get(name)
        if schema is None:
            continue
        allowed = schema['type']
        allowed_types = [allowed] if type(allowed) is str else allowed
        if value is None:
            if 'null' not in allowed_types:
                return False
        elif type(value) is not str or 'string' not in allowed_types:
            return False
    return True


def run_once(
    raw_request: bytes,
    *,
    runtime_factory: RuntimeFactory | None = None,
) -> bytes:
    """Handle one bounded request and always return one strict envelope."""
    try:
        request = decode_request(raw_request)
    except RaiSidecarProtocolError:
        return encode_response(RuntimeErrorResponse('invalid_request'))

    try:
        runtime = create_runtime(runtime_factory)
    except RaiRuntimeConfigurationError:
        return encode_response(RuntimeErrorResponse('runtime_unavailable'))

    try:
        output = runtime.propose(request)
        if type(output) not in {TextReply, ActionProposal}:
            return encode_response(
                RuntimeErrorResponse('invalid_runtime_output')
            )
        if not _proposal_is_bound_to_request(request, output):
            return encode_response(
                RuntimeErrorResponse('invalid_runtime_output')
            )
        response = ProposalResponse(
            request_id=request.request_id,
            model=runtime.model,
            output=output,
        )
        return encode_response(response)
    except RaiSidecarProtocolError:
        return encode_response(RuntimeErrorResponse('invalid_runtime_output'))
    except Exception:
        return encode_response(RuntimeErrorResponse('runtime_failed'))


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Read one request from stdin, write one response, and then terminate."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout.buffer if stdout is None else stdout
    if arguments:
        response = encode_response(RuntimeErrorResponse('invalid_request'))
    else:
        try:
            raw_request = input_stream.read(MAX_REQUEST_BYTES + 1)
        except Exception:
            raw_request = b''
        response = run_once(
            raw_request,
            runtime_factory=runtime_factory,
        )
    try:
        output_stream.write(response)
        output_stream.flush()
    except Exception:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
