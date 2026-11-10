"""Restore relevant original exchanges without widening session or privacy scope."""

import re
import unicodedata

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



# A local match chooses the existing read-only evidence path. Independent cues
# allow Korean topic fronting and omitted subjects; uncertainty stays with the model.
_QUESTION = re.compile(r'뭐|뭔|무엇|뭘|어디|어떤|누구|누가|언제|무슨|어떻게|얼마|몇|어느|언젠')
_READ_END = re.compile(r'(?:말해|알려|들려|추천해|떠올려|기억해내)(?:줘|주라|줘요|주세요|줄래요?|'
                       r'줄수있(?:어|니|나|나요|어요|을까|을까요))$'
                       r'|(?:아세요|알고(?:있|계)[가-힣]{0,5}|떠오르[가-힣]{0,4}|알려주시겠어요)$|(?:기억(?:하고있|하|해|나|나는|나시)|생각나)(?:니|나|요|시나요|세요|지|죠|나요|있어)?$')
_QUESTION_END = re.compile(r'(?:야|니|나|지|죠|요|까|더라|더라고|던가|어|래|습니까|을까)$')
_PAST_TIME = re.compile(r'예전|이전|지난|저번|그때|그날|당시|어제|작년|옛날|(?:했을|하던|갔을|정할|갈|할)때')
_STORY_SOURCE = re.compile(r'대화|이야기|얘기|말씀(?:드린|드렸|하셨)|말(?:했|하던|한|했던|해준|해줬|해둔)|'
                           r'정(?:했|한|했던|해둔)|붙였|적어둔|써둔|적었|나눴|골(?:랐|라둔)|고른|선택|약속|의논|상의|의견|기로한|'
                           r'(?:기로|라고|다고)했|가봤|갔던|다녀|다녔|만났|준비했|예약했')
_NOT_SINGLE_READ = re.compile(
    r'방금|아까|이번|지금|현재|오늘|내일|번역|예문|예시|예를들|만약|인용|문장|뜻[이일인]|'
    r'(?:라는|란|다는)(?:말|부분|표현|질문)|무시|존댓말|반말|(?:영|한|중국|일본|외국)어로|'
    r'하지마|하지말|말하지|말고|원치않|금지|그만|대신|그리고|주고|는데|지만|한뒤|그다음|먼저|'
    r'삭제|정정|바꿔|지워|수정|저장해줘|기억해줘|기억해둬|친구(?:가|의|는|이름|별명)|'
    r'라면|란무엇|란뭐|하면좋|가면좋|어떻게해야|어떻게하면|해볼까|'
    r'(?:이동|충전|촬영|재생|주문)(?:해|하고|하자)|날씨|기온|순찰(?:기록|결과|보고|중|에서|할때|해|하고)|홈캠|촬영기록|지도에서'
)
_OWNER = re.compile(r'(?:^|[\s,])(?:내|제|나|저)(?:가|는|의|게|를|한테|에게)?(?=[\s,]|$)'
                    r'|(?:^|[\s,])(?:우리|저희)(?:가|는|의|집)?(?=[\s,]|$)')
_MULTIPLE = re.compile(r'[가-힣0-9](?:랑|하고|과|와)\s|각각|둘\s*다|두\s*가지|하나씩|차례로')
_RETROSPECTIVE = re.compile(r'(?:았|었|였|했|랐|렸|갔|썼|뒀|보던|두었)던|해두었던|(?:해|려|어|러)둔')
_KIND = {'name': r'이름|본명|성함|성명', 'nickname': r'호칭|별명|애칭|닉네임',
         'pet': r'반려|강아지|고양이', 'preference': r'취향|입맛|선호|좋아하|좋아했|좋아한|즐겨'}
_RECENT_HINT = {'name': _KIND['name'] + r'|(?:저는|나는|난)\s*[가-힣]{2,5}(?:입니다|이에요|예요|이야)',
                'nickname': _KIND['nickname'] + '|불러', 'pet': _KIND['pet'],
                'preference': _KIND['preference'] + '|좋아해'}


_READ_OR_DISCLOSE = re.compile(
    r'(?:조회|검색|확인|사용|참고|언급|공개|활용|불러오|가져오|참조|기억|찾|열람|읽|꺼내|되살리|떠올리|'
    r'알려|말|보여|들려|대답|답변|보|쓰|살펴보|들추)(?:해주|해|하|주|어주|아주|아보|어보|보)?'
    r'(?:지(?:는|도)?(?:마|말(?:아|고|라|자|았으면)|않(?:게|도록|았으면|으면좋)|못하게)|면안|금지|없이)'
    r'|(?:안(?:읽|쓰|보|꺼내|찾아보)(?:고|면좋|았으면)|(?:기억|저장정보)(?:은|는|을|를)?(?:빼고|제외하고|없이))'
)
_STORED_SUBJECT = re.compile(r'기억|저장|예전|지난|개인정보|이름|별명|호칭|애칭|입맛|취향|선호|'
                             r'좋아하|좋아했|반려|강아지|고양이|전에(?:내가|제가|우리)|(?:내가|제가)전에|전에.*(?:얘기|말|이야기)')
_EVENT_SCOPE = re.compile(r'당시|당일|그때|그날|시절|무렵|동안|그행사|(?:행사|모임|축제|대회|여행|전시)(?:에서|날|때)|(?:했을|갔을|하던|할)때|'
                          r'(?:에서|때).*(?:썼|쓴|쓰던|소개|밝힌|밝혔|불렸|불리|사용|였|었|했던)')


def speech_recall_prohibited(text):
    """Suppress a read for this turn when the speaker forbids using or disclosing it."""
    value = ''.join(unicodedata.normalize('NFKC', text).split()).casefold()
    return bool(_STORED_SUBJECT.search(value) and _READ_OR_DISCLOSE.search(value))


def event_scoped_attribute(text):
    """A current attribute alone cannot establish what was used at a past event."""
    return bool(_EVENT_SCOPE.search(''.join(unicodedata.normalize('NFKC', text).split())))


# The foreground read route can answer a question even when its topic contains
# new information. Skipping extraction requires one fully consumed question
# about a subject already present in the source-backed claim.
_NAMED_STORY_QUESTION = re.compile(
    r'(?:혹시)?(?:(?:예전|이전|지난번|저번|그때|전)에?)?'
    r'(?:(?:내가|제가|우리가)?(?:말한|말했던|정한|정했던|알려준))?'
    r'(?P<subject>[가-힣a-z0-9]{2,100}?)(?:은|는|이|가|을|를)?'
    r'(?:(?:뭐|무엇|어디|언제|무슨요일)(?:였지|였더라|였죠|였나요|이었지|이었나요|이었죠)'
    r'|(?:뭐|무엇|언제|어디)(?:로|으로|라고)(?:정했|불렀|골랐)는지(?:기억나(?:요)?|알려(?:줘|주세요)))'
    r'[?.。]?'
)


def named_story_read_only(text, stories):
    """Certify that the directly routed question carries no additional user claim."""
    def compact(value):
        return ''.join(unicodedata.normalize('NFKC', value).casefold().split())

    match = _NAMED_STORY_QUESTION.fullmatch(compact(text))
    if not match or len(stories) != 1:
        return False
    story = stories[0]
    entries = story.get('current', [])
    if len(entries) != 1 or entries[0]['actor'] != 'user' or entries[0]['status'] not in {'stated', 'confirmed'}:
        return False
    subject, claim = match['subject'], compact(entries[0]['text'])
    for label in (story['title'], *story.get('aliases', [])):
        label = compact(label)
        if subject.startswith(label):
            attribute = subject[len(label):].removeprefix('의')
            if re.match(re.escape(label) + '(?:의)?' + re.escape(attribute) + '[은는이가]', claim):
                return True
    return False


def _standalone_speech_focus(text):
    """Select the evidence type for a self-contained long-term question."""
    raw = unicodedata.normalize('NFKC', text).strip()
    # A leading 그리고 can introduce a new, complete question about the speaker's drink.
    # Keep embedded conjunctions and requests with another clause on the normal path.
    followup = re.fullmatch(
        r'그리고[\s,]+((?:내가|제가)\s*(?:좋아하는|선호하는|즐겨\s*마시는)\s*'
        r'(?:음료수?|차|커피)(?:[은는이가])?\s*(?:뭐|무엇|뭔)[가-힣]{0,6}[.?!。]?)', raw)
    if followup:
        raw = followup[1]
    value = re.sub(r'[\s,]+', '', raw.rstrip('.?!。！？')).casefold()
    if (not re.fullmatch('[가-힣a-z0-9]+', value) or _NOT_SINGLE_READ.search(value)
            or _MULTIPLE.search(raw) or speech_recall_prohibited(raw)
            or (re.search(r'기억해(?:요)?$', value) and not raw.endswith('?'))):
        return None
    if not (_READ_END.search(value) or (_QUESTION.search(value) and _QUESTION_END.search(value))
            or (raw.endswith('?') and re.search(r'[은는]$', value))):
        return None

    owner = bool(_OWNER.search(raw))
    kinds = {kind for kind, pattern in _KIND.items() if re.search(pattern, value)}
    if 'pet' in kinds:
        if not re.search(r'이름|품종|종류|생일|몇살|나이', value):
            kinds.discard('pet')
        else:
            kinds.discard('name')  # The pet's name is one personal attribute.
    if len(kinds) > 1:
        return None
    focus = next(iter(kinds), None)
    if focus in ('name', 'nickname'):
        label = '(?:' + _KIND[focus] + ')'
        if not (re.search(r'(?:내|제|나의|저의)(?:진짜|원래)?' + label, value)
                or re.match(label + r'(?:이|가|은|는|을|를|뭐|기억)', value)
                or (focus == 'nickname' and owner and re.search(r'불(?:러|렀)|부르|붙여', value))):
            focus = None
    elif focus and not (owner or re.match(r'(?:내|제)(?:취향|입맛|반려|강아지|고양이)', value)):
        focus = None

    if focus == 'preference' and re.search(r'면서|틀고|켜고|끄고|순찰|지도|경로|위치|주문|결제', value):
        return None
    if not focus:
        source = _STORY_SOURCE.search(value)
        earlier = _PAST_TIME.search(value) or re.search(r'(?:^|\s)전에(?=\s|$)|(?:내가|제가)전에', raw)
        past = _RETROSPECTIVE.search(value)
        if not ((source and (earlier or owner or past)) or (past and (earlier or owner))):
            return None
        # An overt third-person subject before the past event can be a public fact.
        before_event = raw.split((source or past).group(), 1)[0]
        if not owner and re.search(r'(?:^|\s)[가-힣]{2,}[이가]\s', before_event):
            return None
        focus = 'related'

    return focus


# Anchor ownership to the name noun. A friend's, pet's or event-specific name
# must retain its own lookup scope even when the utterance also contains "내".
_SELF_NAME_SUBJECT = re.compile(
    r'(?:새대화야[.])?(?:혹시)?(?:지금|현재)?(?:장기기억에)?(?:내가|제가)?'
    r'(?:전에|지난번에|예전에)?(?:알려준|알려드린|말한|말했던|저장한|저장된|저장해둔)?'
    r'(?:내|제|나의|저의)(?:진짜|정식)?(?:이름|본명|성명|성함)(?:이|은|을|를)?'
)
_NAME_READ_VERB = r'(?:말해|알려|가르쳐)(?:줘요?|주세요|줄래요?)'
_SELF_NAME_PREDICATE = re.compile(
    r'(?:뭐(?:야|예요|지|죠|였지|였죠|였더라|였나요|였어요|였어)|뭔가요|'
    r'무엇(?:인가요|이었지|이었죠)|(?:뭔지|무엇인지)' + _NAME_READ_VERB
    + r'|기억나(?:요)?|' + _NAME_READ_VERB + r'|(?:어떻게|뭐라고)저장(?:돼|되어)있(?:어|나요|어요))'
)


def _self_name_predicate(utterance):
    value = ''.join(unicodedata.normalize('NFKC', utterance).split()).rstrip('.?!。')
    subject = _SELF_NAME_SUBJECT.match(value)
    return value[subject.end():] if subject else None


def is_self_name_question(utterance):
    predicate = _self_name_predicate(utterance)
    return predicate is not None and _SELF_NAME_PREDICATE.fullmatch(predicate) is not None


def self_name_lookup(utterance):
    """Scope a model-selected name lookup when the speaker explicitly owns the name."""
    predicate = _self_name_predicate(utterance)
    return (predicate is not None and not predicate.startswith(('으로', '로', '처럼', '보다', '말고'))
            and not event_scoped_attribute(utterance) and not speech_recall_prohibited(utterance)
            and not re.search(r'친구|가족|엄마|아빠|동생|언니|누나|형제|반려|강아지|고양이|'
                              r'이름|본명|성명|성함|호칭|별명|애칭|닉네임|각각|둘다|그리고|번역|인용|예시|대신|[“”"‘’]', predicate))


def previous_self_name_id(text, turns, summary):
    """Find a prior lookup receipt for a standalone question about the speaker's own name."""
    if summary is None and is_self_name_question(text):
        for turn in reversed(turns):
            if not is_self_name_question(turn.user_content):
                continue
            public = turn.response.get('public')
            if not isinstance(public, dict):
                continue
            memory, decision = public.get('memory'), public.get('decision')
            ids = memory.get('ids') if isinstance(memory, dict) else None
            if (isinstance(decision, dict) and decision.get('type') == 'message'
                    and isinstance(ids, list) and len(ids) == 1 and isinstance(ids[0], str)):
                return ids[0]
    return None


def speech_recall_focus(text, turns, summary):
    """Choose a read-only path; recent user corrections remain part of the answer."""
    focus = _standalone_speech_focus(text)
    if focus is None:
        return None
    if focus == 'related':
        terms = {term for term in _query_terms(text) if len(term) >= 2 and term != '전에'
                 and not _QUESTION.match(term) and not _PAST_TIME.match(term)}
        if summary is None:
            for turn in turns:
                user = getattr(turn, 'user_content', '')
                receipt = getattr(turn, 'response', {})
                # Only a persisted, story-backed answer can establish a repeat.
                # Shared words select a fresh lookup, never the earlier answer.
                if ((receipt.get('story_binding') or {}).get('readset')
                        and receipt.get('public', {}).get('decision', {}).get('type') == 'message'
                        and sum(term in user for term in terms) >= 2
                        and _standalone_speech_focus(user) == 'related'):
                    return 'story_repeat'
        if any(term in getattr(summary, 'content', '') for term in terms):
            return None
        for turn in turns:
            user = getattr(turn, 'user_content', '')
            answer = getattr(turn, 'assistant_content', '')
            shared = {term for term in terms if term in user or term in answer}
            if not shared:
                continue
            response = getattr(turn, 'response', {})
            decision = response.get('public', response).get('decision', {})
            # A tool receipt with one word in common does not answer a
            # historical question. The exchange stays in the model's input.
            if decision.get('type') == 'tool_call' and len(shared) == 1:
                continue
            # The prior question itself is not evidence of the value. Keep
            # recent user statements and corrections eligible even on a single match.
            if (_QUESTION.search(user) and not _NOT_SINGLE_READ.search(''.join(user.split()))
                    and not re.search(r'이제|바[뀌뀐꿔꿨꾼]|정정|아니|[.!].+|,', user)
                    and (len(shared) == 1 or re.search(
                        r'확인할 수 없|찾을 수 없|기억나지 않|알 수 없|모르겠', answer))):
                continue
            return None
    elif turns or summary:
        recent = ' '.join(getattr(turn, 'user_content', '') + ' ' +
                          getattr(turn, 'assistant_content', '') for turn in turns)
        recent += ' ' + getattr(summary, 'content', '')
        if re.search(_RECENT_HINT[focus], recent):
            return None
    return focus


def recall_originals(personal_memory, request, token, snapshot, token_budget=2048):
    """Read original turns through the same deletion/consent filter as inference.

    This changes only this answer's input, never the persisted compacted context.
    Whole exchanges are retained; the provider handles an oversized exchange.
    """
    store = personal_memory.conversations
    if (getattr(snapshot, 'recall_blocked', False) or not store.semantic_context or snapshot.summary is None
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
