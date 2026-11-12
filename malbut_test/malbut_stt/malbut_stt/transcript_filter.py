"""Recognize recurring Whisper boilerplate before forwarding speech."""

import re
import unicodedata


_THANKS = re.compile(r'(?:감사합니다)+')
# NFKC converts compatibility jamo such as ㅋ into the U+1100 jamo block.
_REPEATED_SYLLABLE = re.compile(r'([가-힣\u1100-\u11ff])\1{11,}')
_SUBSCRIBE = re.compile(r'구독(?:과|와)?좋아요|좋아요(?:와|과)?구독')
_NEWS = re.compile(
    r'(?:mbc|kbs|sbs|ytn|jtbc|엠비씨|케이비에스|에스비에스)뉴스'
    r'[가-힣a-z0-9]{0,20}?입니다')


def boilerplate_reason(text):
    """Return a stable reason, without modifying the original transcript.

    Bare/repeated thanks are blocked deliberately, even when actually spoken.
    Ordinary sentences containing thanks, news or subscriptions remain valid.
    """
    if not isinstance(text, str):
        return None
    compact = ''.join(char for char in unicodedata.normalize('NFKC', text).casefold()
                      if char.isalnum())
    if _THANKS.fullmatch(compact):
        return 'standalone_thanks'
    if _REPEATED_SYLLABLE.search(compact):
        return 'repeated_syllable'
    if '시청해주셔서' in compact:
        return 'viewing_outro'
    if _SUBSCRIBE.search(compact):
        return 'subscribe_like_outro'
    if _NEWS.search(compact):
        return 'news_signoff'
    return None
