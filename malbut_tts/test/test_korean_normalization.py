"""The selected listening comparison pronunciation regressions."""
import pytest
from malbut_tts.korean_normalization import KoreanNormalizer

cases = {
    '2026년 10월 15일': '이천이십육년 시월 십오일',
    '오전 7시 30분': '오전 일곱시 삼십 분',
    '물의 온도는 23.5도입니다.': '물의 온도는 이십삼점오도입니다.',
    '사과 3개와 우유 2팩, 12,500원': '사과 세개와 우유 두팩, 만이천오백원',
    '배터리 25%': '배터리 이십오퍼센트',
    'Wi-Fi와 Bluetooth': '와이파이와 블루투스',
    'USB, PDF, AI, CPU': '유에스비, 피디에프, 에이아이, 씨피유',
    'OpenAI라는 이름': '오픈에이아이라는 이름',
    'Python 파일': 'Python 파일',
    'myOpenAItool': 'myOpenAItool',
    '알림(중요한 내용)을 읽어 주세요.': '알림(중요한 내용)을 읽어 주세요.',
    '하하, 오늘은 기분이 좋네요. 휴, 이제 쉬어야겠어요.': '하하, 오늘은 기분이 좋네요. 휴, 이제 쉬어야겠어요.',
    '예약 번호는 204번입니다.': '예약 번호는 이 공 사 번입니다.',
    '예약 번호는 007번입니다.': '예약 번호는 공 공 칠 번입니다.',
    '15분 전에 알려 드릴게요.': '십오 분 전에 알려 드릴게요.',
    '10분 뒤, 25분 후, 30분으로': '십 분 뒤, 이십오 분 후, 삼십 분으로',
    '번호는 24번, 3번 반복': '번호는 이 사 번, 세번 반복',
    '24번 버스': '이십사번 버스',
    'ROS 2와 ROS2': '알 오 에스 투와 알 오 에스 투',
    'ros   2 노드': '알 오 에스 투 노드',
    '이분은 안내 담당자입니다.': '이분은 안내 담당자입니다.',
}

@pytest.mark.parametrize('text,expected', cases.items())
def test_selected_pronunciation(text, expected):
    assert KoreanNormalizer()(text) == expected


def test_lexicon_respects_identifier_boundaries():
    n = KoreanNormalizer()
    assert n.pattern.search('myROS2tool') is None
    assert n.pattern.search('ROS20') is None
