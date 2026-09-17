"""Lossless, bounded text units for sequential speech synthesis.

This module chooses audio work units, not conversational intent. It does not
normalize, strip, translate, or otherwise rewrite the supplied text.
"""


_ENDINGS = frozenset('.!?。！？')
_CLOSERS = frozenset('\"\'”’»›」』】）》〉)]}')
_ENDING_SUFFIX = _ENDINGS | _CLOSERS
_SOFT_BREAKS = frozenset(',，、;；:：')


def _internal_dot(text, index):
    """Keep decimal/domain-like dots between ASCII letters or digits intact."""
    if not 0 < index < len(text) - 1:
        return False
    before, after = text[index - 1], text[index + 1]
    return (before.isascii() and before.isalnum()
            and after.isascii() and after.isalnum())


def _sentence_boundary(text, start, limit):
    """Find the first fitting sentence end, retaining its punctuation/spacing."""
    content_seen = False
    for index in range(start, limit):
        char = text[index]
        content_seen = content_seen or not char.isspace()
        if not content_seen:
            continue
        if char in '\r\n':
            end = index + 1
            if char == '\r' and end < len(text) and text[end] == '\n':
                end += 1
        elif char in _ENDINGS and not (char == '.' and _internal_dot(text, index)):
            end = index + 1
            while (end < len(text) and end <= limit
                   and text[end] in _ENDING_SUFFIX):
                end += 1
        else:
            continue
        if end > limit:
            return None
        while end < limit and text[end].isspace():
            end += 1
        return end
    return None


def _bounded_boundary(text, start, limit):
    """Prefer the nearest whitespace/comma-like break before the hard limit."""
    first_content = start
    while text[first_content].isspace():
        first_content += 1
    for end in range(limit, first_content + 1, -1):
        if text[end - 1].isspace() or text[end - 1] in _SOFT_BREAKS:
            # A CRLF is one boundary when there is room to keep it together.
            if text[end - 1] == '\r' and text[end:end + 1] == '\n':
                if end - 1 > first_content:
                    return end - 1
                continue
            return end
    return limit


def _segment_ends(text, max_chars, last_content):
    """Compute boundaries with constant storage and reject stranded spacing."""
    start = 0
    while start < len(text):
        limit = min(start + max_chars, len(text))
        # Keep at least the final content character with trailing whitespace.
        if limit < len(text):
            limit = min(limit, last_content)
        first_content = start
        while first_content < limit and text[first_content].isspace():
            first_content += 1
        if first_content == limit:
            raise ValueError('Speech text spacing cannot fit bounded speech segments')
        end = _sentence_boundary(text, start, limit)
        if end is None:
            end = (limit if limit == len(text)
                   else _bounded_boundary(text, start, limit))
        yield end
        start = end


def iter_speech_segments(text, max_chars=80):
    """Yield original substrings, in order, without an intermediate chunk list.

    For text containing a non-whitespace character, every yielded segment
    contains non-whitespace, has at most ``max_chars`` Unicode code points,
    and concatenating all segments reproduces ``text`` exactly. Sentence
    punctuation/newlines are preferred; an oversized sentence is split near
    whitespace or commas, or at the hard limit when no such break exists.
    Dots inside decimal/domain-like ASCII tokens are not sentence boundaries.

    Empty or whitespace-only text yields nothing. ``max_chars`` must be an
    actual integer from 16 through 512. For other text, a whitespace run of
    ``max_chars`` or more is conservatively rejected with ``ValueError``:
    hard length bounds, losslessness, and no whitespace-only chunks cannot
    always coexist. This includes some theoretically splittable whitespace
    runs, deliberately keeping the contract simple and predictable. Spacing
    that would leave a whitespace-only segment under this deterministic
    partition is also rejected, including a lone character surrounded by
    more whitespace than one chunk can hold.

    Validation scans the input before the first yield (constant extra space),
    so invalid spacing never fails after a partial utterance has been yielded.
    This is not a full linguistic or grapheme-cluster parser; a hard split may
    divide an unusually long unbroken token or punctuation/grapheme sequence.
    """
    if not isinstance(text, str):
        raise TypeError('Speech text must be a string')
    if type(max_chars) is not int or not 16 <= max_chars <= 512:
        raise ValueError('max_chars must be an integer from 16 through 512')
    if not text or text.isspace():
        return

    whitespace_run = 0
    last_content = 0
    for index, char in enumerate(text):
        if char.isspace():
            whitespace_run += 1
            if whitespace_run >= max_chars:
                raise ValueError('Speech text contains an oversized whitespace run')
        else:
            whitespace_run = 0
            last_content = index

    # Validate the exact partition without allocating or retaining substrings.
    # Recomputing cheap boundaries keeps storage constant and errors atomic.
    for _ in _segment_ends(text, max_chars, last_content):
        pass
    start = 0
    for end in _segment_ends(text, max_chars, last_content):
        yield text[start:end]
        start = end
