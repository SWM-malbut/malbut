"""Expectations fixed before live inference; each case starts a fresh session."""

CASES = []


def turn(text, tool=None, arguments=None, **checks):
    return dict(text=text, tool=tool, arguments=arguments or {}, **checks)


def mapping(text, name, **checks):
    return turn(text, 'request_mapping', {'map_name': name}, **checks)


def patrol(text, level='normal', **checks):
    return turn(text, 'request_patrol', {'thoroughness': level}, **checks)


def locate(text, method='auto', **checks):
    return turn(text, 'request_relocalization', {'method': method}, **checks)


def add(ident, group, *turns, **options):
    CASES.append(dict(id=ident, group=group, turns=list(turns), **options))


# The exact user example is intentionally first; repeated in independent sessions.
for i, answer in enumerate(['우리집', '이사한 집으로 해', '이름은 새집으로 하자', 'home_2026', '"새집으로"']):
    name = ['우리집', '이사한 집', '새집', 'home_2026', '새집으로'][i]
    add(f'map-exact-{i+1}', 'map-missing-name',
        turn('지도 만들어줘', reply_pattern='이름|뭐라고|무엇으로'), mapping(answer, name))

for i, text in enumerate([
    '새 지도 만들어 줘', '우리 집 지도 좀 만들어 줄래?', '집 안 탐색해서 지도 저장해줘',
    '여기 맵 좀 따 줘', '자동 지도 작성 시작해', '오토슬램 실행해 줘',
    '공간을 돌아다니면서 지도 작성 부탁해요', '집 구조를 지도에 담아 줘',
    '지도 맨들어 줘', '지도 만들어주라',
]):
    add(f'map-unnamed-{i+1}', 'map-missing-name', turn(text, reply_pattern='이름|뭐라고|무엇으로'))

for i, (text, name) in enumerate([
    ('새집이라는 이름으로 새 지도를 만들어줘', '새집'),
    ('공간 탐색해서 지도를 만들고 이름은 우리 집으로 해', '우리 집'),
    ('새 지도를 만들어볼까? 이름은 이사집으로 하자', '이사집'),
    ('지도 이름은 신혼집이야. 지금 만들어줘', '신혼집'),
    ('새집 말고 작업실이라는 이름으로 지도 만들어줘', '작업실'),
    ('지도 만들어 줘. 이름은 구집, 아니 새집', '새집'),
    ('"집으로"라는 이름을 그대로 써서 새 지도 만들어줘', '집으로'),
    ('"순찰해"라는 이름의 새 지도를 만들어줘', '순찰해'),
    ('home_2026이라는 이름으로 지도를 만들어 줘', 'home_2026'),
    ('이름을 A-2로 해서 새 지도를 만들어 줘', 'A-2'),
    ('"2층 사무실"이라는 이름으로 지도 작성해 줘', '2층 사무실'),
    ('지도 이름은 "해"야. 지금 새로 만들어 줘', '해'),
    ('map 이름은 office야. 지금 mapping 시작해줘', 'office'),
    ('Create a new map named office', 'office'),
    ('지도 이름은 "이사집으로"야. 이 이름 그대로 만들어 줘', '이사집으로'),
]):
    add(f'map-named-{i+1}', 'map-names', mapping(text, name))

add('map-yes-is-not-name', 'map-conversation', turn('지도 만들어줘'),
    turn('응', reply_pattern='이름|뭐라고|무엇으로'), mapping('우리집', '우리집'))
add('map-delegated-name', 'map-conversation', turn('지도 만들어줘'),
    turn('이름은 네가 알아서 정해', reply_pattern='이름|지정|정해|알려'), mapping('그럼 새집', '새집'))
add('map-correct-pending-name', 'map-conversation', turn('지도 만들어줘'),
    mapping('구집 말고 새집으로 해', '새집'))
add('map-abandon-pending', 'map-conversation', turn('지도 만들어줘'),
    turn('아니야 지도 만드는 건 안 할래', tool=[None, 'cancel_voice_mission']), turn('새집'))
add('map-topic-switch', 'map-conversation', turn('지도 만들어줘'),
    turn('지도 만들기는 그만하고 순찰 기능이 뭔지 설명해줘', reply_pattern='순찰|살펴|둘러'), turn('우리집'))
add('map-switch-to-patrol', 'map-conversation', turn('지도 만들어줘'), patrol('지도는 됐고 대충 순찰해', 'light'))
add('map-switch-to-location', 'map-conversation', turn('지도 만들어줘'), locate('지도 말고 지금 네 위치를 다시 찾아줘'))
add('map-name-alone', 'map-conversation', turn('새집으로 해'))
add('map-pending-name-quote', 'map-conversation', turn('지도 만들어줘'),
    turn('"새집으로 해"라는 말 영어로 번역해줘'))
add('map-pending-name-negative', 'map-conversation', turn('지도 만들어줘'),
    turn('새집이라고 이름 지으면 안 돼', tool=[None, 'cancel_voice_mission']))

for i, text in enumerate([
    '지도 만들 수 있어?', '지도 만들기는 어떻게 하는 거야?', '새집 지도 만드는 건 지금 하지 마',
    '내일 새집이라는 이름으로 지도 만들어 줘', '10분 뒤에 새집 지도를 만들어 줘',
    '나중에 내가 말하면 새집 지도를 만들어', '만약 새집 지도를 만들라고 하면 어떻게 돼?',
    '"새집이라는 이름으로 지도 만들어줘"를 영어로 번역해 줘',
    '새집 지도 만들어줘라는 문장의 뜻을 설명해 줘',
    '철수가 새집 지도 만들어줘라고 말했어',
    '새집 지도 만들고 순찰까지 해줘', '지도 만들기와 위치 보정을 동시에 해줘',
]):
    add(f'map-noncommand-{i+1}', 'map-intent-boundary', turn(text))

# Exact names with forbidden characters must not silently become another name.
for i, name in enumerate(['../secret', '/tmp/home', '새집.yaml', '집/거실', '집!', '🙂', '', '가'*65]):
    add(f'map-invalid-name-{i+1}', 'map-invalid-name',
        turn(f'새 지도 만들어줘. 이름은 "{name}" 이 문자열 그대로 사용해.', no_dispatch=True))
add('map-overwrite-explicit', 'map-limits', turn('기존 home 지도를 덮어써서 새로 만들어 줘'))
add('map-resume-unsupported', 'map-limits', turn('기존 home 지도에 이어서 안방만 추가로 그려줘'))

for level, texts in {
    'light': ['대충 순찰해', '집 안 가볍게 한번 둘러봐 줘', '간단히 순찰 좀 해줘',
              '꼼꼼히 말고 가볍게만 순찰해', '집 안 대강 한번 살펴봐',
              '순찰 좀 해라. 대충 훑어보기만 해', '가볍게 집안을 순찰해 주시겠어요?'],
    'normal': ['순찰해줘', '집 안 한 바퀴 돌아봐', '집 안 한번 살펴봐 줄래?',
               '보통 강도로 순찰해', '일반 순찰 시작해', '집안 둘러보고 와',
               '순찰 좀 해주라', '집 안 순찰해 볼까?'],
    'thorough': ['구석구석 꼼꼼하게 순찰 좀 해', '빠뜨리는 곳 없이 자세히 순찰해',
                 '집 안을 아주 철저히 살펴봐 줘', '정밀하게 순찰해 줘',
                 '가볍게 말고 꼼꼼히 집안을 순찰해', '집 안 샅샅이 순찰해 줘'],
}.items():
    for i, text in enumerate(texts):
        add(f'patrol-{level}-{i+1}', 'patrol-coverage', patrol(text, level))
add('patrol-self-correct-light', 'patrol-coverage', patrol('꼼꼼하게 순찰해, 아니 가볍게만 해', 'light'))
add('patrol-self-correct-thorough', 'patrol-coverage', patrol('대충 돌아봐, 아니 구석구석 꼼꼼하게 순찰해', 'thorough'))
add('patrol-english', 'patrol-coverage', patrol('Patrol the house thoroughly', 'thorough'))

for i, text in enumerate([
    '순찰 기능이 있어?', '순찰 강도는 뭐가 있어?', '대충 순찰해라는 말은 무슨 뜻이야?',
    '내일 아침에 순찰해 줘', '내가 외출하면 그때 순찰해', '순찰 시작하지 마',
    '지도를 만드는 대신 순찰하는 게 뭔지 설명해줘',
    '집 안 순찰하고 나서 위치도 다시 맞춰줘', '순찰하거나 지도를 만들어 줘',
]):
    add(f'patrol-noncommand-{i+1}', 'patrol-intent-boundary', turn(text))
for i, text in enumerate([
    '거실만 순찰하고 다른 방은 절대 가지 마',
    '순찰을 초속 2미터로 해줘', '3시간 동안 쉬지 말고 계속 순찰해',
]):
    add(f'patrol-unsupported-constraint-{i+1}', 'patrol-limits', turn(text))

for method, texts in {
    'auto': ['위치 다시 찾아줘', '지금 어디인지 위치 찾아봐', '지금 네 위치를 찾아 줘',
             '위치 보정해 줘', '현재 위치를 다시 잡아 줘', '위치가 어긋났어. 다시 맞춰줘',
             '지도상 네 위치를 다시 확인해서 맞춰 줘', '로봇 위치 재인식 시작해',
             '로컬라이제이션 다시 해 줘', '위치 다시 차자줘',
             '다른 데로 옮겨놨으니까 네 위치 다시 잡아 줘'],
    'global_search': ['지도 전체에서 네 위치를 찾아줘', '전역 탐색으로 위치를 잡아 줘',
                      '저장된 위치 쓰지 말고 지도 전체를 검색해서 위치 찾아 줘',
                      '전역 재위치추정 시작해줘', 'global search로 네 위치 찾아 줘',
                      '로봇 위치를 전체 지도에서 처음부터 다시 탐색해 줘'],
}.items():
    for i, text in enumerate(texts):
        add(f'locate-{method}-{i+1}', 'localization-method', locate(text, method))
add('locate-self-correct', 'localization-method', locate('전체 지도 말고 자동으로 위치 다시 잡아줘'))
add('locate-self-correct-global', 'localization-method', locate('자동 말고 전역 탐색으로 위치 다시 찾아줘', 'global_search'))

for i, text in enumerate([
    '지금 어디야?', '너 어느 방에 있어?', '현재 로봇 위치가 어디야?',
    '지금 내 위치 알려줘', '엄마가 어디 있는지 찾아봐', '김철수 위치 찾아줘',
    '여기가 어디지?', '내 휴대폰 위치 찾아줘', '위치 찾을 수 있어?',
    '위치 보정 방법 알려줘', '내일 위치 다시 잡아줘', '위치를 다시 잡지는 마',
    '"위치 다시 찾아줘"를 영어로 번역해 줘', '위치가 틀리면 그때 다시 잡아줘',
    '지도 좌표 x 1 y 2 방향 0으로 위치 맞춰줘',
    '거실 문 앞을 초기 위치로 설정해 줘', '네 위치 잡고 집 안 순찰해 줘',
]):
    add(f'locate-noncommand-{i+1}', 'localization-intent-boundary', turn(text))
add('locate-status-to-action', 'localization-conversation', turn('지금 어디야?'), locate('그럼 네 위치를 다시 찾아줘'))
add('locate-mode-followup', 'localization-conversation',
    turn('위치 찾는 방법이 뭐가 있어?'), locate('그럼 전역 탐색으로 지금 위치를 찾아줘', 'global_search'))

for capability, starter in [
    ('autoslam', mapping('새집이라는 이름으로 지도 만들어줘', '새집')),
    ('patrol', patrol('대충 순찰해', 'light')),
    ('relocalize', locate('위치 다시 찾아줘')),
]:
    for i, stop in enumerate(['그만해', '지금 하는 거 멈춰', '취소해줘']):
        add(f'{capability}-cancel-{i+1}', 'mission-cancellation', starter,
            turn(stop, 'cancel_voice_mission', canceled=True), behaviors={capability: 'hold'})
    for outcome in ('reject', 'abort'):
        add(f'{capability}-{outcome}', 'action-failure',
            dict(starter, terminal=['FAILED', 'REJECTED']), behaviors={capability: outcome})
    add(f'{capability}-manager-off', 'manager-unavailable',
        dict(starter, manager_off=True, speech_counts={'지금은 실행할 수 없어요.': 1}), manager_enabled=False)

add('map-cancel-then-new', 'mission-cancellation', mapping('첫집이라는 이름으로 지도 만들어줘', '첫집'),
    turn('지도 만들기 취소해', 'cancel_voice_mission', canceled=True),
    mapping('새집이라는 이름으로 다시 지도 만들어줘', '새집'), behaviors={'autoslam': 'hold'})
add('map-cancel-quote', 'mission-cancellation', mapping('새집이라는 이름으로 지도 만들어줘', '새집'),
    turn('"지도 만들기 그만해"를 영어로 번역해줘', remains_running='autoslam'), behaviors={'autoslam': 'hold'})
add('patrol-map-preemption', 'manager-priority', patrol('순찰해줘'),
    mapping('새집이라는 이름으로 지도 만들어줘', '새집', preempted='patrol'), behaviors={'patrol': 'hold'})
add('map-patrol-low-priority', 'manager-priority', mapping('새집이라는 이름으로 지도 만들어줘', '새집'),
    patrol('대충 순찰해', 'light', manager_rejected=True, remains_running='autoslam'), behaviors={'autoslam': 'hold'})

# Critical boundaries repeated after all other coverage (not reused conversation).
for i in range(3):
    add(f'repeat-active-position-{i+1}', 'repeatability', locate('지금 어디인지 위치 찾아봐'))
    add(f'repeat-position-status-{i+1}', 'repeatability', turn('지금 어디야?'))
    add(f'repeat-light-patrol-{i+1}', 'repeatability', patrol('대충 순찰해', 'light'))


add('holdout-map-save-unnamed', 'holdout-save', turn('집 안 탐색해서 지도 저장해줘', reply_pattern='이름|뭐라고|무엇으로'))
add('holdout-map-save-named', 'holdout-save', mapping('집 안을 탐색하고 지도를 새집이라는 이름으로 저장해 줘', '새집'))
add('holdout-map-name-correction', 'holdout-correction', turn('지도 만들어줘'),
    mapping('구집이 아니라 새집으로 해', '새집'))
add('holdout-map-name-precise', 'holdout-correction', mapping('새집, 정확히는 우리 집이라는 이름으로 지도 만들어 줘', '우리 집'))
add('holdout-patrol-correction', 'holdout-correction', patrol('가볍게가 아니라 꼼꼼하게 순찰해 줘', 'thorough'))
add('holdout-locate-correction', 'holdout-correction', locate('자동이 아니라 전역 탐색으로 위치 찾아줘', 'global_search'))
for i in range(2):
    for feature, text in [
        ('map', '내일 새집이라는 이름으로 지도 만들어 줘'),
        ('patrol', '10분 뒤에 순찰 시작해줘'),
        ('location', '내가 외출하면 그때 네 위치를 다시 찾아줘'),
    ]:
        add(f'holdout-future-{feature}-{i+1}', 'future-repeatability', turn(text))

# Additional boundaries for the three October 19 fixes, declared before inference.
for ident, answer, name in [
    ('quoted-suffix', '"작업실로"', '작업실로'),
    ('quoted-instruction', '"집으로 해"', '집으로 해'),
    ('unquoted-suffix', '새집으로 해', '새집'),
]:
    add(f'fix-holdout-{ident}', 'fix-name-boundary',
        turn('지도 만들어줘'), mapping(answer, name))
for level, text in [
    ('light', '지도는 안 만들래. 대신 집 안 가볍게 한번 둘러봐 줘'),
    ('thorough', '지도 만들기는 됐어. 지금 구석구석 꼼꼼히 순찰해 줘'),
    ('normal', '지도 이름 정하는 건 나중에 하고 보통 강도로 순찰해 줘'),
]:
    add(f'fix-holdout-switch-{level}', 'fix-topic-boundary',
        turn('지도 만들어줘'), patrol(text, level))
for i, text in enumerate([
    '지도는 안 만들래. 순찰이 어떤 기능인지 알려줄래?',
    '맵 만드는 건 됐고, 순찰 강도 차이를 설명해 줘. 실행하지는 말고.',
]):
    add(f'fix-holdout-explain-{i+1}', 'fix-topic-boundary',
        turn('지도 만들어줘'), turn(text, reply_pattern='순찰|살펴|둘러'), turn('우리집'))
add('fix-holdout-active-stop', 'fix-topic-boundary',
    mapping('새집이라는 이름으로 지도 만들어줘', '새집'),
    turn('지도 만들기 그만해', 'cancel_voice_mission', canceled=True),
    behaviors={'autoslam': 'hold'})
for i, acknowledgement in enumerate(['네', '알겠어']):
    add(f'fix-holdout-acknowledgement-{i+1}', 'fix-name-boundary',
        turn('지도 만들어줘'),
        turn(acknowledgement, reply_pattern='이름|뭐라고|무엇으로'),
        mapping('작업실', '작업실'))

# The mapping-name clarification must not consume an explicit movement request.
add('fix-nav-current-request', 'navigation-scope',
    turn('거실로 가볼까?', 'request_navigation', {'location': '거실'}))
add('fix-nav-topic-switch', 'navigation-scope', turn('지도 만들어줘'),
    turn('지도는 됐고 거실로 이동해 줘', 'request_navigation', {'location': '거실'}))
add('fix-nav-destination-answer', 'navigation-scope', turn('이리 와', reply_pattern='어디|장소|목적지'),
    turn('거실', 'request_navigation', {'location': '거실'}))
add('fix-map-place-name-answer', 'navigation-scope', turn('지도 만들어줘'), mapping('거실', '거실'))
add('fix-nav-unknown-place', 'navigation-scope', turn('작업실로 이동해 줘'))


def map_question():
    return turn('지도 만들어줘', reply_pattern='이름|뭐라고|무엇으로',
                mapping_pending=False, clarification_for='mapping_name')


# State-sensitive name answers: fixed expectations, repeated in fresh sessions.
for ident, text, name, quoted in [
    ('workshop-quoted', '"작업실로"', '작업실로', True),
    ('living-quoted', '"거실로"', '거실로', True),
    ('kitchen-quoted', '"주방으로"', '주방으로', True),
    ('entrance-quoted', '"현관으로"', '현관으로', True),
    ('office-quoted', '"사무실로"', '사무실로', True),
    ('new-home-quoted', '"새집으로"', '새집으로', True),
    ('workshop-bare', '작업실', '작업실', False),
    ('workshop-particle', '작업실로', '작업실', False),
    ('living-bare', '거실', '거실', False),
    ('living-particle', '거실로', '거실', True),
    ('kitchen-particle', '주방으로', '주방', True),
    ('entrance-particle', '현관으로', '현관', True),
    ('office-particle', '사무실로', '사무실', False),
    ('workshop-naming', '이름은 작업실로 해', '작업실', False),
]:
    add(f'state-name-{ident}', 'mapping-state-names', map_question(),
        mapping(text, name, mapping_pending=True, forced_mapping=quoted))

add('state-switch-navigation', 'mapping-state-boundaries', map_question(),
    turn('거실로 가', 'request_navigation', {'location': '거실'}, mapping_pending=True, forced_mapping=False),
    turn('"작업실로"', mapping_pending=False))
add('state-abandon', 'mapping-state-boundaries', map_question(),
    turn('지도 만들지 마', tool=[None, 'cancel_voice_mission'], mapping_pending=True),
    turn('"작업실로"', mapping_pending=False, forced_mapping=False))
add('state-translation', 'mapping-state-boundaries', map_question(),
    turn('"작업실로"라는 말을 영어로 번역해줘', mapping_pending=True, forced_mapping=False),
    turn('거실', mapping_pending=False))
add('state-no-question', 'mapping-state-boundaries',
    turn('"작업실로"', mapping_pending=False, forced_mapping=False))
add('state-ack', 'mapping-state-boundaries', map_question(),
    turn('응', clarification_for='mapping_name', mapping_pending=True),
    mapping('작업실로', '작업실', mapping_pending=True, forced_mapping=False))
add('state-invalid-name', 'mapping-state-boundaries', map_question(),
    turn('"집.yaml"', clarification_for='mapping_name', mapping_pending=True, forced_mapping=False),
    mapping('작업실', '작업실', mapping_pending=True, forced_mapping=False))
add('state-new-conversation', 'mapping-state-boundaries', map_question(),
    turn('새 대화 시작하자', mapping_pending=False),
    turn('"작업실로"', mapping_pending=False, forced_mapping=False))


if __name__ == '__main__':
    from collections import Counter
    import json
    from pathlib import Path
    Path(__file__).with_name('cases.json').write_text(json.dumps(CASES, ensure_ascii=False, indent=2), encoding='utf-8')
    print(len(CASES), 'scenarios,', sum(len(c['turns']) for c in CASES), 'turns')
    print(dict(Counter(c['group'] for c in CASES)))
