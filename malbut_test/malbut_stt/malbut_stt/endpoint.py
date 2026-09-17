"""Conservative Korean sentence-ending hints for adaptive silence waiting.

This is a lexical hint, not a semantic classifier. Only explicit final endings
shorten the wait; informal fragments, connectives, quoted text and unsupported
languages keep the fallback timeout. ASR punctuation alone provides no evidence.
Even a grammatical sentence can be followed by more speech: the audio boundary
must therefore invalidate this hint whenever another voiced frame arrives.
"""

import re
import unicodedata


_FINAL_ENDING = re.compile(
    r'(?:습니다|습니까|입니다|입니까|세요|십시오|까요|나요|군요|네요|지요|죠|'
    r'예요|이에요|어요|아요|해요|돼요|줘|줄래요|줄래|지\s*마|'
    r'한다|된다|있다|없다|거야|'
    r'(?:느리|빠르|좋|나쁘|크|작|많|적|맞|틀리)다|'
    r'(?:느려|빨라|좋아|나빠|커|작아|많아|적어|맞아|틀려|싫어)(?:요)?|'
    r'뭐해|뭐하니|알겠지|해\s*봐(?:라)?|'
    r'어때|있어|없어|(?:누구|어디|뭐|몇\s*시|몇\s*살)야)$'
)


def is_complete_korean_utterance(text: str) -> bool:
    """Recognize explicit final endings without treating punctuation as proof."""
    if not isinstance(text, str):
        return False
    text = unicodedata.normalize('NFC', text).strip().rstrip('.!?…。！？~').rstrip()
    if _FINAL_ENDING.search(text):
        return True
    if len(text) < 2:
        return False
    syllable = ord(text[-2]) - 0xAC00
    if not 0 <= syllable < 11172:
        return False
    final_consonant = syllable % 28
    # Retain contractions such as 왔다/끝났어/했대/있네 and 갈까/먹을까.
    # Bare 다, 어, 네, 대 or 까 alone also occur in fragments and nouns.
    return ((text[-1] in '다어네대' and final_consonant == 20)  # ㅆ
            or (text[-1] == '까' and final_consonant == 8))  # ㄹ
