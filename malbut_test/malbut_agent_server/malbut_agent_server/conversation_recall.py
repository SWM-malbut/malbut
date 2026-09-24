"""Restore relevant original exchanges without widening session or privacy scope."""

import re

from malbut_agent_server.semantic_summary import count_tokens


_ORDINAL_NUMBERS = {
    '첫': '1', '두': '2', '둘': '2', '세': '3', '셋': '3', '네': '4', '넷': '4',
    '다섯': '5', '여섯': '6', '일곱': '7', '여덟': '8', '아홉': '9', '열': '10',
}
_ORDINAL = re.compile(
    r'(?<![가-힣0-9])(' + '|'.join(_ORDINAL_NUMBERS) + r'|[0-9]+)\s*(?:번째|째)',
)
_OPTION_REFERENCE = re.compile(
    _ORDINAL.pattern + r'\s*(?:안|방법|후보|선택지|거|것)'
    r'(?=\s|[.!?,]|$|(?:으로|부터|은|는|이|가|을|를|로|에|도|만)(?:\s|[.!?,]|$))',
)
_FIRST_REFERENCE = re.compile(
    r'처음(?:에)?\s*(?:(?:제안|추천)(?:한|했던)|보여(?:준|줬던))',
)


def recall_requested(text):
    """Recognize a request to revisit an earlier statement or its exact wording."""
    return bool(re.search(
        r'원문|그대로.*(?:말|읽|알려|보여)|정확한?\s*(?:표현|문구|말|조건)|'
        r'(?:아까|앞서|이전에|처음에|그때|지난번|말했|말한|말했던|이야기한|정했던)',
        text,
    ) or _OPTION_REFERENCE.search(text) or _FIRST_REFERENCE.search(text))


def recall_originals(personal_memory, request, token, snapshot, token_budget=2048):
    """Read original turns through the same deletion/consent filter as inference.

    This changes only this answer's input, never the persisted compacted context.
    Whole exchanges are retained; the provider handles an oversized exchange.
    """
    store = personal_memory.conversations
    if (not store.semantic_context or snapshot.summary is None
            or not recall_requested(request.utterance)):
        return
    with store._lock:
        conn = store._connection
        rows = conn.execute(
            '''SELECT * FROM conversation_turns
            WHERE user_id=? AND conversation_id=? AND session_instance_id=?
            AND generation=? AND status='completed' AND ordinal<?
            ORDER BY ordinal''',
            (request.user_id, token.conversation_id, token.session_instance_id,
             token.generation, token.ordinal),
        ).fetchall()
        originals, _, dependencies = personal_memory._context(
            request.user_id, token, snapshot.state,
            [store._turn_from_row(row) for row in rows], None, conn,
        )
    recent = {turn.ordinal: turn for turn in snapshot.history}
    archived = [turn for turn in originals if turn.ordinal not in recent
                and (turn.user_content or turn.assistant_content)]
    terms = _query_terms(request.utterance)
    # ponytail: lexical recall can miss paraphrases. Add semantic retrieval only
    # when measured misses warrant another model/index; never invent a quote.
    ordinal_terms = {term for term in terms if re.fullmatch(r'[0-9]+번째', term)}

    def rank(turn):
        text = _search_text(turn.user_content + ' ' + turn.assistant_content)
        return (sum(' ' + term + ' ' in text for term in ordinal_terms),
                sum(term in text for term in terms), turn.ordinal)

    ranked = sorted(archived, key=rank, reverse=True)
    selected, used = [], 0
    for turn in ranked:
        size = count_tokens(turn.user_content + '\n' + turn.assistant_content)
        if selected and used + size > token_budget:
            continue
        selected.append(turn)
        used += size
        if used >= token_budget:
            break
    snapshot.history = sorted([*selected, *recent.values()], key=lambda t: t.ordinal)
    snapshot.dependencies.update(dependencies)
    snapshot.context['conversation_recall'] = {
        'scope': 'current_session_only',
        'restored_ordinals': sorted(turn.ordinal for turn in selected),
        'complete_archive': len(selected) == len(archived),
        'instruction': '조회한 원문에 없는 표현은 정확한 인용으로 제시하지 않습니다.',
    }


def _search_text(text):
    """Normalize ordinal spelling for ranking without changing stored originals."""
    return _ORDINAL.sub(
        lambda match: ' ' + _ORDINAL_NUMBERS.get(match[1], match[1]) + '번째 ', text,
    )


def _query_terms(text):
    ignored = {'아까', '앞서', '이전에', '처음에', '그때', '지난번', '내가', '우리',
               '원문', '그대로', '정확히', '다시', '말한', '말했던', '이야기한',
               '알려줘', '보여줘', '읽어줘', '뭐였지', '했지', '말해줘'}
    terms = {re.sub(r'(?:에서|으로|에게|하고|처럼|이랑|에는|은|는|을|를|의|가|이)$', '', word)
             for word in re.findall(r'[가-힣A-Za-z0-9]+', _search_text(text))
             if len(word) > 1 and word not in ignored}
    if _FIRST_REFERENCE.search(text):
        terms.add('1번째')
    return terms - {''}
