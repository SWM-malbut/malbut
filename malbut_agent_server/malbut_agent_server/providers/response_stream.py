"""Bounded Responses SSE decoding with non-authoritative message previews."""

import json

from malbut_agent_server.providers.base import ProviderError


def _reject_constant(_value):
    raise ValueError('non-finite JSON')


def _string_prefix(text, start):
    """Decode a JSON string prefix only through complete escapes/code points."""
    if start >= len(text) or text[start] != '"':
        return None, False
    at = start + 1
    while at < len(text):
        char = text[at]
        if char == '"':
            return json.loads(text[start:at + 1]), True
        if ord(char) < 32:
            return None, False
        if char == '\\':
            if at + 1 >= len(text):
                break
            escape = text[at + 1]
            if escape == 'u':
                if at + 6 > len(text):
                    break
                if any(c not in '0123456789abcdefABCDEF' for c in text[at + 2:at + 6]):
                    return None, False
                at += 6
                continue
            if escape not in '"\\/bfnrt':
                return None, False
            at += 2
        else:
            at += 1
    return json.loads(text[start:at] + '"'), False


def message_candidate(text):
    """Read a top-level message string, not nested/tool text or JSON regexes.

    A partial sentence needs its following nonspace character so the TTS
    splitter sees the same trailing whitespace as the final full message.
    A closed message string can prepare its complete text instead.
    """
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    try:
        at = len(text) - len(text.lstrip())
        if at >= len(text) or text[at] != '{':
            return None
        at += 1
        message_type, seen = None, set()
        while at < len(text):
            at += len(text[at:]) - len(text[at:].lstrip())
            key, at = decoder.raw_decode(text, at)
            if not isinstance(key, str) or key in seen:
                return None
            seen.add(key)
            at += len(text[at:]) - len(text[at:].lstrip())
            if at >= len(text) or text[at] != ':':
                return None
            at += 1
            at += len(text[at:]) - len(text[at:].lstrip())
            if key == 'message':
                if message_type != 'message':
                    return None
                prefix, closed = _string_prefix(text, at)
                if not prefix:
                    return None
                prefix.encode('utf-8')  # Incomplete surrogate pairs are not candidates.
                if closed:
                    return prefix
                # Conservative boundary: punctuation outside quotes, followed
                # by whitespace and at least one character of the next sentence.
                quoted = False
                for index, char in enumerate(prefix):
                    if char in '"“”':
                        quoted = not quoted
                    if not quoted and char in '.!?。！？':
                        tail = index + 1
                        if tail >= len(prefix) or not prefix[tail].isspace():
                            continue
                        while tail < len(prefix) and prefix[tail].isspace():
                            tail += 1
                        if tail < len(prefix):
                            return prefix[:tail + 1]
                return None
            value, at = decoder.raw_decode(text, at)
            if key == 'type':
                message_type = value
            at += len(text[at:]) - len(text[at:].lstrip())
            if at >= len(text) or text[at] != ',':
                return None
            at += 1
    except (ValueError, UnicodeError, IndexError):
        return None
    return None


class ResponseEvents:
    """Accept one assistant text source; all tools and ambiguous sources disable hints."""

    def __init__(self, turn=None, observer=None):
        self.turn = turn
        self.observer = observer
        self.response_id = None
        self.item = None
        self.part = None
        self.text = ''
        self.blocked = False
        self.completed = None
        self.sequence = -1

    def _block(self):
        self.blocked = True
        self.text = ''
        if self.turn is not None:
            self.turn.fail_attempt()

    def feed(self, event):
        if not isinstance(event, dict):
            raise ProviderError('OpenAI SSE event must be an object')
        if self.completed is not None:
            raise ProviderError('OpenAI response stream continued after completion')
        sequence = event.get('sequence_number')
        if sequence is not None:
            if type(sequence) is not int or sequence <= self.sequence:
                raise ProviderError('OpenAI response stream ordering is invalid')
            self.sequence = sequence
        kind = event.get('type')
        if self.observer is not None:
            self.observer('first_event')
            if (kind in {'response.output_text.delta', 'response.function_call_arguments.delta'}
                    and isinstance(event.get('delta'), str) and event['delta']):
                self.observer('first_output_delta')
        if kind in {'error', 'response.failed', 'response.incomplete'}:
            raise ProviderError('OpenAI response stream failed')
        if kind == 'response.created':
            response = event.get('response', {})
            identity = response.get('id') if isinstance(response, dict) else None
            if not isinstance(identity, str) or not 1 <= len(identity) <= 256:
                self._block()
            elif self.response_id is not None:
                raise ProviderError('OpenAI response stream changed identity')
            else:
                self.response_id = identity
        elif kind == 'response.output_item.added':
            item = event.get('item', {})
            index = event.get('output_index')
            if not isinstance(item, dict) or type(index) is not int or index < 0:
                self._block()
            elif item.get('type') == 'message' and item.get('role') == 'assistant':
                if (self.item is not None or not isinstance(item.get('id'), str)
                        or not 1 <= len(item['id']) <= 256):
                    self._block()
                else:
                    self.item = (index, item['id'])
            elif item.get('type') != 'reasoning':
                self._block()
        elif kind == 'response.content_part.added':
            part = event.get('part', {})
            source = (event.get('output_index'), event.get('item_id'))
            if (type(event.get('output_index')) is not int
                    or type(event.get('content_index')) is not int
                    or self.item != source or event.get('content_index') != 0
                    or self.part is not None or not isinstance(part, dict)
                    or part.get('type') != 'output_text'):
                self._block()
            else:
                self.part = (*source, 0)
        elif kind == 'response.output_text.delta' and not self.blocked:
            source = (event.get('output_index'), event.get('item_id'), event.get('content_index'))
            delta = event.get('delta')
            if (type(event.get('output_index')) is not int
                    or type(event.get('content_index')) is not int
                    or self.part != source or not isinstance(delta, str)):
                self._block()
            else:
                if self.turn is not None:
                    self.turn.mark('provider_first_delta')
                if len(self.text) + len(delta) > 65536:
                    self._block()
                else:
                    self.text += delta
                    if self.response_id is not None and self.turn is not None:
                        candidate = message_candidate(self.text)
                        if candidate is not None:
                            self.turn.prepare(candidate)
        elif kind == 'response.completed':
            response = event.get('response')
            if (self.completed is not None or not isinstance(response, dict)
                    or (self.response_id is not None and response.get('id') != self.response_id)):
                raise ProviderError('OpenAI response stream completion is invalid')
            self.completed = response
            if self.turn is not None:
                self.turn.mark('provider_stream_completed')
            if self.observer is not None:
                self.observer('completed_event')


def read_response_stream(response, max_bytes, turn=None, observer=None):
    """Consume the entire bounded body, preserving keep-alive only after EOF."""
    content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if content_type != 'text/event-stream':
        # A gateway may return ordinary JSON to a streaming request. This is
        # the same response, never a second POST disguised as a fallback.
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ProviderError('OpenAI response exceeded the size limit')
        try:
            decoded = json.loads(body.decode('utf-8'), parse_constant=_reject_constant)
        except (UnicodeError, ValueError) as error:
            raise ProviderError('OpenAI response was not valid JSON') from error
        if not isinstance(decoded, dict):
            raise ProviderError('OpenAI response must be an object')
        return decoded
    parser = ResponseEvents(turn, observer)
    used, data = 0, []

    def dispatch():
        if not data:
            return
        raw = '\n'.join(data)
        data.clear()
        if raw == '[DONE]':
            return
        try:
            event = json.loads(raw, parse_constant=_reject_constant)
        except ValueError as error:
            raise ProviderError('OpenAI SSE event was not valid JSON') from error
        parser.feed(event)

    while True:
        line = response.readline(max_bytes - used + 1)
        if not line:
            break
        used += len(line)
        if used > max_bytes:
            raise ProviderError('OpenAI response exceeded the size limit')
        try:
            line = line.decode('utf-8').rstrip('\r\n')
        except UnicodeError as error:
            raise ProviderError('OpenAI SSE event was not valid UTF-8') from error
        if not line:
            dispatch()
        elif line.startswith('data:'):
            value = line[5:]
            data.append(value[1:] if value.startswith(' ') else value)
    dispatch()
    if parser.completed is None:
        raise ProviderError('OpenAI response stream ended before completion')
    return parser.completed
