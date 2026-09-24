"""Explicit conversation defaults and session-scoped response preferences."""

import json
import re

from malbut_agent_server.schemas import ValidationError, validate_user_id


DEFAULTS = {
    'tone': '편안한 존댓말', 'address': '', 'length': '상황에 맞게',
    'initiative': '자연스럽게 주고받자',
}
CHOICES = {
    'tone': ('편안한 존댓말', '친근한 반말'),
    'length': ('짧고 간단하게', '상황에 맞게', '자세하게'),
    'initiative': ('주로 들어줘', '자연스럽게 주고받자', '적극적으로 이어줘'),
}
LABELS = {'말투': 'tone', '호칭': 'address', '답변 길이': 'length',
          '대화 중 적극성': 'initiative'}
ANSWER_SCOPE = (
    r'이번\s*(?:(?:답변|응답|설명)(?:에만|만|은)|(?:한\s*)?번만)'
    r'|이번만|이\s*(?:답변|응답|설명)(?:에만|만)'
)
SESSION_SCOPE = r'앞으로|계속|이\s*대화|이번\s*대화'
SETTINGS_PROMPT = (
    '어떤 말투가 편하세요? 편안한 존댓말 / 친근한 반말. '
    '어떻게 불러드릴까요? 이름·별명 / 나중에 정하기. '
    '어느 정도로 설명해 드릴까요? 짧고 간단하게 / 상황에 맞게 / 자세하게. '
    '대화를 어떻게 이어가면 좋을까요? 주로 들어줘 / 자연스럽게 주고받자 / '
    '적극적으로 이어줘. 원하는 항목을 “초기 설정 말투=친근한 반말”처럼 '
    '알려주세요. 선택하지 않은 항목은 편안한 존댓말, 호칭 없이, 상황에 맞는 '
    '길이, 자연스럽게 주고받기로 시작해요. 기억 사용 동의는 별도로 선택해요.'
)


def initialize_schema(conn):
    """Keep settings in the existing database, separate from personal facts."""
    conn.execute('''CREATE TABLE IF NOT EXISTS conversation_preferences (
        user_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS session_response_settings (
        user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
        session_instance_id TEXT NOT NULL, generation INTEGER NOT NULL,
        settings_json TEXT NOT NULL,
        PRIMARY KEY (user_id, conversation_id),
        FOREIGN KEY (user_id, conversation_id)
            REFERENCES conversation_sessions (user_id, conversation_id)
            ON DELETE CASCADE)''')


def validate_settings(settings, *, session=False):
    """Accept only the four bounded settings; consent has its own policy."""
    allowed = set(DEFAULTS) | ({'response_mode'} if session else set())
    if type(settings) is not dict or set(settings) - allowed:
        raise ValidationError('unknown conversation setting')
    for key, value in settings.items():
        if not isinstance(value, str):
            raise ValidationError('conversation setting must be text')
        if key == 'address':
            if len(value) > 50 or any(ord(char) < 32 for char in value):
                raise ValidationError('address must be at most 50 characters')
        elif key == 'response_mode':
            if value not in {'natural', 'listen_only', 'advice_first'}:
                raise ValidationError('invalid response mode')
        elif value not in CHOICES[key]:
            raise ValidationError('invalid conversation setting choice')
    return dict(settings)


def initial_settings(conn, user_id):
    validate_user_id(user_id)
    row = conn.execute(
        'SELECT settings_json FROM conversation_preferences WHERE user_id=?',
        (user_id,),
    ).fetchone()
    return dict(DEFAULTS, **(validate_settings(json.loads(row[0])) if row else {}))


def save_initial_settings(conn, user_id, patch):
    settings = dict(initial_settings(conn, user_id), **validate_settings(patch))
    conn.execute('''INSERT INTO conversation_preferences VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json''',
        (user_id, json.dumps(settings, ensure_ascii=False)))
    return settings


def preference_request(text):
    """Recognize explicit controls, never reported or hypothetical choices."""
    # General conversational wording is still interpreted by the answer model;
    # only clear direct commands can modify the durable defaults/session state.
    if any(char in text for char in ('"', "'", '`', '“', '”', '‘', '’', '「', '」')) or re.search(
        r'예시|예를\s*들|만약|가정|라고\s*(했|말|적|써|요청)'
        r'|(?:해|불러|들어)\s*달(?:라는|라며|래)'
        r'|(?:줘|주세요)(?:라는|라며)|친구가|친구는', text,
    ):
        return None
    defaults = bool(re.match(r'^\s*(?:초기|기본)\s*설정', text))
    if defaults and '=' not in text:
        instruction = re.split(
            f'{ANSWER_SCOPE}|{SESSION_SCOPE}|[.!?\n]', text, maxsplit=1,
        )[0]
        # Naming stored defaults does not authorize changing them.
        defaults = not re.search(
            r'(?:바꾸|변경하|수정하|저장하)지\s*(?:마|말|않)'
            r'|그대로\s*(?:두|둬|유지)|유지(?:해|하|할)', instruction,
        )
    if not defaults:
        # Split at explicit scope changes, keeping shared clauses together.
        clauses = [part for part in re.split(
            f'(?=(?:{ANSWER_SCOPE}|{SESSION_SCOPE})(?:은|는|부터)?(?:\\s|$))', text,
        ) if part.strip()]
        if len(clauses) > 1:
            session_patch, answer_patch = {}, {}
            for clause in clauses:
                edit = preference_request(clause)
                if edit and edit['scope'] != 'invalid':
                    target = (answer_patch if edit['scope'] == 'answer'
                              else session_patch)
                    target.update(edit['patch'])
            if not session_patch and not answer_patch:
                return None
            return {
                'scope': 'session' if session_patch else 'answer',
                'patch': dict(session_patch, **answer_patch),
                'session_patch': session_patch,
            }
    patch = {}
    if defaults and '=' in text:
        body = re.sub(r'^\s*(?:초기|기본)\s*설정\s*[:：]?\s*', '', text)
        for item in re.split(r'[,;\n]+', body):
            pair = item.strip().split('=', 1)
            if len(pair) != 2 or pair[0].strip() not in LABELS:
                return {'scope': 'invalid', 'patch': {}}
            key, value = LABELS[pair[0].strip()], pair[1].strip()
            patch[key] = '' if key == 'address' and value == '나중에 정하기' else value
    else:
        patterns = {
            'tone': ((r'(?<![가-힣])(?:친근한\s*)?반말(?:로\s*(?:말|대답|답|해)|\s*해\s*줘|\s*$)',
                      '친근한 반말'),
                     (r'(?:편안한\s*)?존댓말(?:로\s*(?:말|대답|답|해)|\s*$)',
                      '편안한 존댓말')),
            'length': ((r'짧고\s*간단하게|짧게\s*(?:말|설명|답|해)', '짧고 간단하게'),
                       (r'자세하게|자세히\s*(?:말|설명|답|해)', '자세하게'),
                       (r'(?:길이는?\s*|답변은?\s*)?상황에\s*맞게', '상황에 맞게')),
            'initiative': ((r'(?:주로|그냥)\s*들어\s*줘', '주로 들어줘'),
                           (r'자연스럽게\s*주고받자', '자연스럽게 주고받자'),
                           (r'적극적으로\s*(?:이어\s*줘|대화를?\s*이끌어\s*줘)',
                            '적극적으로 이어줘')),
        }
        for key, options in patterns.items():
            matches = []
            for pattern, value in options:
                match = re.search(pattern, text)
                if match and not re.match(
                    r'.{0,5}(?:하지\s*마|하지\s*말|주지\s*마|말고|않|금지)',
                    text[match.end():],
                ):
                    matches.append(value)
            if len(matches) == 1:
                patch[key] = matches[0]
        address = re.search(
            r'(?:나를|저를|호칭은?)\s*([가-힣A-Za-z0-9_ -]{1,50}?)'
            r'(?:이?라고|으로|로)\s*불러\s*(?:줘|주세요)', text,
        )
        if address:
            patch['address'] = address[1].strip()
        if re.search(r'호칭(?:은|을)?\s*(?:없이|나중에)', text):
            patch['address'] = ''
        if not defaults:
            for pattern, mode in (
                (r'(?:그냥|말고|만)\s*들어\s*줘|듣기만\s*해\s*줘', 'listen_only'),
                (r'조언(?:을)?\s*해\s*줘|해결\s*방법부터|해결책(?:을)?\s*알려\s*줘',
                 'advice_first'),
            ):
                match = re.search(pattern, text)
                if match and not re.match(
                    r'.{0,5}(?:하지\s*마|하지\s*말|말고|않|금지)', text[match.end():],
                ):
                    patch['response_mode'] = mode
                    break
    try:
        validate_settings(patch, session=not defaults)
    except ValidationError:
        return {'scope': 'invalid', 'patch': {}} if defaults else None
    if not patch and not defaults:
        return None
    recipient = re.search(
        r'(?<![가-힣A-Za-z0-9_])([가-힣A-Za-z0-9_]+?)(?:에게|한테)'
        r'(?:는|만|도)?(?=\s*(?:(?:친근한|편안한|편한)\s*)?(?:반말|존댓말))', text,
    )
    artifact_request = re.search(
        r'(?<![가-힣])(?:본문|글|초대문)\s*(?:에만|만은?)(?=\s|[,.!?]|$)', text,
    ) or (
        re.search(r'(?:써|작성해|고쳐|수정해|번역해|다듬어)\s*(?:줘|주세요)', text)
        and not re.search(SESSION_SCOPE, text)
    ) or (
        recipient and recipient[1] not in {'나', '저'}
    )
    scope = 'defaults' if defaults else (
        'answer' if artifact_request or re.search(ANSWER_SCOPE, text)
        else 'session'
    )
    return {'scope': scope, 'patch': patch}


def response_settings(conn, token, text):
    """Freeze defaults for a session and overlay only this answer's request."""
    row = conn.execute('''SELECT settings_json FROM session_response_settings
        WHERE user_id=? AND conversation_id=? AND session_instance_id=?
        AND generation=?''', (token.user_id, token.conversation_id,
                              token.session_instance_id, token.generation)).fetchone()
    baseline = dict({'response_mode': 'natural'}, **(
        validate_settings(json.loads(row[0]), session=True)
        if row else initial_settings(conn, token.user_id)
    ))
    edit = preference_request(text)
    effective = dict(baseline)
    retained = dict(baseline)
    if edit and edit['scope'] != 'invalid':
        effective.update(edit['patch'])
        retained.update(edit.get(
            'session_patch', {} if edit['scope'] == 'answer' else edit['patch'],
        ))
    return effective, retained, edit


def response_setting_fact(fact):
    """Exclude response controls without discarding unrelated personal facts."""
    edit = preference_request(fact['evidence'])
    if not edit:
        return False
    patch = edit['patch']
    value = re.sub(r'\s+', '', fact['value'])
    if fact['kind'] == 'nickname':
        return (
            value in {
                re.sub(r'\s+', '', change.get('address', ''))
                for change in (patch, edit.get('session_patch', {}))
            }
            and not re.search(r'(?:내|제)\s*(?:별명|호칭)(?:은|는|이|가)', fact['evidence'])
        )
    if fact['kind'] != 'preference':
        return False
    cues = {
        'tone': r'말투|반말|존댓말', 'length': r'짧|간단|자세|답변.*길|설명.*길',
        'initiative': r'들어|주고받|적극|후속.*질문|대화.*(?:이어|이끌)',
        'response_mode': r'듣기|들어|조언|해결책|해결방법',
    }
    return any(key in patch and re.search(pattern, value)
               for key, pattern in cues.items())


def commit_settings(conn, token, retained, edit):
    """Write together with a successful conversation turn, never on inference."""
    if edit and edit['scope'] == 'defaults' and edit['patch']:
        save_initial_settings(conn, token.user_id, edit['patch'])
    conn.execute('''INSERT INTO session_response_settings VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, conversation_id) DO UPDATE SET
        session_instance_id=excluded.session_instance_id,
        generation=excluded.generation, settings_json=excluded.settings_json''',
        (token.user_id, token.conversation_id, token.session_instance_id,
         token.generation, json.dumps(retained, ensure_ascii=False)))
