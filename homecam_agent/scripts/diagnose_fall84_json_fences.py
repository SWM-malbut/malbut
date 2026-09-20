"""Post-hoc format-only diagnostic. Never replace strict primary eval results."""
import copy
import re

from replay_vlm_frames import assess_response


def diagnose(response, duration):
    """Remove only one exact enclosing Markdown fence; no JSON/label repair."""
    raw = copy.deepcopy(response)
    message = raw.get('message')
    text = message.get('content') if isinstance(message, dict) else None
    match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', text.strip()) if isinstance(text, str) else None
    if match:
        message['content'] = match.group(1)
    assessed = assess_response(raw, duration, evaluation_version='v2')
    return dict(removed_outer_fence=bool(match), assessment=assessed)
