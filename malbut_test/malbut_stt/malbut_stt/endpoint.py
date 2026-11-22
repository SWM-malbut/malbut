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
    r'예요|이에요|어요|아요|해요|돼요|줘|줄래요|줄래|지(?:는|도)?\s*마|'
    r'한다|된다|있다|없다|거야|더라|'
    r'(?:하|가|보|말|쉬|멈추)자|'
    r'(?:느리|빠르|좋|나쁘|크|작|많|적|맞|틀리)다|'
    r'(?:느려|빨라|좋아|나빠|커|작아|많아|적어|맞아|틀려|싫어)(?:요)?|'
    r'뭐해|뭐하니|알겠지|해\s*봐(?:라)?|'
    r'않아|같아|알아|따라와|고마워|기억해|기억나|(?:으면|면)\s*해|'
    r'어때(?:요)?|있어|없어|좋아져|(?:누구|어디|뭐|몇\s*시|몇\s*살)야|몇\s*도(?:야|래))$'
)

# Conservative mid-sentence connectives, multi-character postpositions, and
# conjunction fillers that signal the speaker is pausing mid-thought rather
# than delivering a complete short answer (e.g. "삼각형", "고양이", "라면").
_INCOMPLETE_FRAGMENT = re.compile(
    r'(?:(?:하고|말고|이고|아니고|되고|있고|없고|닫고|열고|가고|오고|보고|주고|'
    r'먹고|쉬고|빼고|넣고|켜고|끄고|듣고|걷고|라고|다고|하고요|'
    r'면서|으면|다면|니까|지만|거나|든지|도록|려고|려면|다가|자마자|'
    r'는데|은데|인데|는데요|은데요|인데요|'
    r'아서|어서|여서|해서|라서|려서|져서|워서|와서|가서|봐서|줘서|꿔서|'
    r'아도|어도|여도|해도|더라도|'
    r'라는|다는|라며|에서는|에서도|에서|에게는|에게|한테|부터|까지는|까지|'
    r'처럼|보다|으로는|으로)|'
    r'(?:오|가|하|되|보|주|닫히|열리|끝나|바꾸|쉬|걷|오르|내리)면|'
    r'[가-힣]를|'
    r'(?:^|\s)(?:그리고|그런데|근데|그러면|그럼|그래서|하지만|그러나|또는|'
    r'혹시|잠깐만|잠깐|저기요|저기|그러니까))$'
)


def _normalize_for_endpoint(text: str) -> str:
    return unicodedata.normalize('NFC', text).strip().rstrip('.!?…。！？~').rstrip()


def is_complete_korean_utterance(text: str) -> bool:
    """Recognize explicit final endings without treating punctuation as proof."""
    if not isinstance(text, str):
        return False
    text = _normalize_for_endpoint(text)
    if _FINAL_ENDING.search(text):
        return True
    if len(text) >= 3 and text.endswith(('니다', '니까')):
        # Catch vowel-stem formal endings such as 합니다/됩니다/풀립니다/가능합니까.
        formal_stem = ord(text[-3]) - 0xAC00
        if 0 <= formal_stem < 11172 and formal_stem % 28 == 17:  # ㅂ
            return True
    if len(text) < 2:
        return False
    syllable = ord(text[-2]) - 0xAC00
    if not 0 <= syllable < 11172:
        return False
    final_consonant = syllable % 28
    # Retain contractions such as 왔다/끝났어/했대/있네/뭐였지 and 갈까/먹을까.
    # Bare 다, 어, 네, 대, 지 or 까 alone also occur in fragments and nouns.
    return ((text[-1] in '다어네대지' and final_consonant == 20)  # ㅆ
            or (text[-1] == '까' and final_consonant == 8))  # ㄹ


def is_incomplete_korean_fragment(text: str) -> bool:
    """Detect unambiguous mid-sentence connectives/particles to veto early cuts."""
    if not isinstance(text, str):
        return False
    text = _normalize_for_endpoint(text)
    if not text or is_complete_korean_utterance(text):
        return False
    if _INCOMPLETE_FRAGMENT.search(text):
        return True
    # Catch adjective/verb -ㄴ데(요) connectives such as 필요한데요 / 좋은데 / 작은데.
    stem_idx = -3 if text.endswith('데요') and len(text) >= 3 else -2 if text.endswith('데') and len(text) >= 2 else None
    if stem_idx is not None:
        syllable = ord(text[stem_idx]) - 0xAC00
        if 0 <= syllable < 11172 and syllable % 28 == 4:  # ㄴ
            return True
    return False
