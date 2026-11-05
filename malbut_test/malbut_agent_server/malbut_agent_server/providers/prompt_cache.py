"""Arrange unchanged, validated context for prefix caching; never cache answers."""

import hashlib
import json
import re


def supports_breakpoints(model):
    match = re.match(r'^gpt-(\d+)(?:\.(\d+))?(?:-|$)', model)
    return bool(match and (int(match[1]), int(match[2] or 0)) >= (5, 6))


def arrange_cached_input(payload, user_id):
    """Keep one untrusted JSON object, split only between complete values.

    Each history entry keeps its own content block as the conversation grows.
    The last two entries have explicit breakpoints; with developer + implicit
    user boundaries this remains within the four-write limit. Deletions and
    corrections change the actual input, so they cannot reuse an old prefix.
    """
    text = payload['input']
    if not isinstance(text, str):
        return
    label, separator, encoded = text.partition('\n')
    if not separator:
        return
    try:
        context = json.loads(encoded)
    except ValueError:
        return
    if not isinstance(context, dict):
        return
    history = context.get('conversation_history_untrusted')
    if not isinstance(history, list):
        return
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    leading = {key: context[key] for key in ('context_policy', 'available_tools')
               if key in context}
    rest = {key: value for key, value in context.items()
            if key not in leading and key != 'conversation_history_untrusted'}
    prefix = label + '\n' + encode(leading)[:-1]
    prefix += (',' if leading else '') + '"conversation_history_untrusted":['
    blocks = [{'type': 'input_text', 'text': prefix}]
    for index, entry in enumerate(history):
        block = {'type': 'input_text', 'text': (',' if index else '') + encode(entry)}
        if index >= len(history) - 2:
            block['prompt_cache_breakpoint'] = {'mode': 'explicit'}
        blocks.append(block)
    blocks.append({'type': 'input_text', 'text': ']' + (',' + encode(rest)[1:] if rest else '}')})
    instructions = payload.pop('instructions')
    payload['input'] = [
        {'role': 'developer', 'content': [
            {'type': 'input_text', 'text': instructions,
             'prompt_cache_breakpoint': {'mode': 'explicit'}},
        ]},
        {'role': 'user', 'content': blocks},
    ]
    # Routing hint only; every request still includes fresh validated content.
    identity = (user_id + '\0' + payload['model']).encode('utf-8')
    payload['prompt_cache_key'] = 'malbut-v1-' + hashlib.sha256(identity).hexdigest()[:40]
    payload['prompt_cache_options'] = {'mode': 'implicit', 'ttl': '30m'}
