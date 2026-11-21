"""Arrange unchanged, validated context for prefix caching; never cache answers."""

import hashlib
import json
import re


def supports_breakpoints(model):
    match = re.match(r'^gpt-(\d+)(?:\.(\d+))?(?:-|$)', model)
    return bool(match and (int(match[1]), int(match[2] or 0)) >= (5, 6))


def _profile_fingerprint(payload, instructions):
    """Classify the static request into a coarse sticky cache-routing tier.

    Over-fragmenting prompt_cache_key on minor per-turn instruction toggles
    scatters traffic across backend shards and adds cold-shard latency. Keep
    heavy full-tool calls on one sticky key while isolating compact tool
    fast-paths, tool-free follow-ups, and background extraction.
    """
    _ = instructions
    tool_count = sum(
        1 for tool in (payload.get('tools') or ()) if isinstance(tool, dict)
    )
    text_format = (payload.get('text') or {}).get('format')
    schema_name = (
        str(text_format.get('name', ''))
        if isinstance(text_format, dict)
        else ''
    )
    if schema_name == 'malbut_memory_extraction':
        tier = 'background_extraction'
    elif tool_count >= 5:
        tier = 'full_tools'
    elif tool_count > 0:
        tier = 'compact_tools'
    else:
        tier = 'tool_free'
    return hashlib.sha256(tier.encode('utf-8')).hexdigest()[:16]


def arrange_cached_input(payload, user_id, *, followup_developer_instructions=None):
    """Keep one untrusted JSON object, split only between complete values.

    Each history entry keeps its own content block as the conversation grows.
    Stable context keys including ``conversation_summary_untrusted`` sit before
    the history array so summaries stay inside the cached prefix until updated.
    ``available_tools`` sits in the trailing block so a tool-disabled second
    pass with ``tool_choice='none'`` reuses the exact Call-1 leading + history
    prefix. Optional ``followup_developer_instructions`` are appended after the
    cached user message so grounding rules stay authoritative without breaking
    the prefix hash.
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

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    leading = {
        key: context[key]
        for key in (
            'context_policy',
            'conversation_summary_untrusted',
        )
        if key in context
    }
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
    messages = [
        {'role': 'developer', 'content': [
            {'type': 'input_text', 'text': instructions,
             'prompt_cache_breakpoint': {'mode': 'explicit'}},
        ]},
        {'role': 'user', 'content': blocks},
    ]
    if followup_developer_instructions:
        messages.append({
            'role': 'developer',
            'content': [{'type': 'input_text', 'text': followup_developer_instructions}],
        })
    payload['input'] = messages
    # Routing hint only; every request still includes fresh validated content.
    # Include the static profile fingerprint so distinct tool/instruction
    # profiles (e.g. full speech, weather fast-path, tool-free follow-ups)
    # do not contend for the same cache routing key.
    profile = _profile_fingerprint(payload, instructions)
    identity = (
        user_id + '\0' + payload['model'] + '\0' + profile
    ).encode('utf-8')
    payload['prompt_cache_key'] = 'malbut-v2-' + hashlib.sha256(identity).hexdigest()[:40]
    payload['prompt_cache_options'] = {'mode': 'implicit', 'ttl': '30m'}
