"""Speech addressee classification service coordination."""

from typing import Any, Dict

from malbut_agent_server.speech_dialogue import validate_interruption_input


MAX_PENDING_ADDRESSEE_REQUESTS = 128


async def classify_addressee_request(
    node: Any,
    request: Any,
    response: Any,
    *,
    response_cls: Any,
    future_cls: Any,
    max_pending: int = MAX_PENDING_ADDRESSEE_REQUESTS,
) -> Any:
    """Yield to the executor while the dialogue worker classifies."""
    response.decision = response_cls.UNKNOWN
    if node._closing or not node.context.ok():
        return response
    if node.situation is not None and node.situation.active:
        return response
    if node._addressee_callbacks >= max_pending:
        return response
    try:
        validate_interruption_input(
            request.utterance_id, request.playback_id, request.text,
        )
        accepted = node.dialogue.submit_interruption(
            request.utterance_id, request.playback_id, request.text,
        )
    except ValueError:
        accepted = False
    if not accepted:
        return response
    key = (request.utterance_id, request.playback_id)
    future = future_cls(executor=node.executor)
    node._addressee_waiters.setdefault(key, []).append(future)
    node._addressee_callbacks += 1
    try:
        response.decision = await future
        if node._closing:
            response.decision = response_cls.UNKNOWN
        return response
    finally:
        node._addressee_callbacks -= 1
        waiters = node._addressee_waiters.get(key, [])
        if future in waiters:
            waiters.remove(future)
            if not waiters:
                del node._addressee_waiters[key]


def resolve_addressee_result(
    node: Any,
    result: Dict[str, Any],
    *,
    response_cls: Any,
) -> None:
    """Resolve pending addressee classification futures for one worker result."""
    decision = result['decision']
    if decision not in (
        response_cls.ADDRESSED,
        response_cls.NOT_ADDRESSED,
        response_cls.UNKNOWN,
    ):
        decision = response_cls.UNKNOWN
    key = (result['utterance_id'], result['playback_id'])
    for future in node._addressee_waiters.pop(key, []):
        if not future.done():
            future.set_result(decision)


def cancel_addressee_waiters(node: Any, *, unknown_decision: Any) -> None:
    """Complete every pending addressee waiter as UNKNOWN and clear the map."""
    for waiters in node._addressee_waiters.values():
        for future in waiters:
            if not future.done():
                future.set_result(unknown_decision)
    node._addressee_waiters.clear()
