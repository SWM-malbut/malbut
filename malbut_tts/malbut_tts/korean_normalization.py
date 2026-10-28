"""Local Korean normalization for the existing Piper KSS voice.

N2gk+ source is pinned in vendor/n2gk.py (see vendor/NOTICE.md). The adapter preserves
parenthetical text, removes Markdown emphasis, protects configured loanwords,
and avoids splitting mixed-case English words into a mixture of Hangul letter
names and Latin characters.
No network inference or text-result cache is used.
"""
from pathlib import Path
import json
import re
import unicodedata

ROOT = Path(__file__).resolve().parent

_EMPHASIS = (
    re.compile(r'(?<![\\*])(\*{1,3})(?![\s*])([^*\n]*?\S)(?<!\*)\1(?!\*)'),
    re.compile(r'(?<![\\A-Za-z0-9_])(_{1,3})(?![\s_])([^_\n]*?\S)(?<!_)\1(?![A-Za-z0-9_])'),
)


def _remove_markdown_emphasis(text):
    """Remove paired emphasis and star bullets only from the spoken copy.

    Keep arithmetic, intra-word underscores and parenthetical content.
    This is a speech cleanup for common LLM emphasis, not a Markdown renderer.
    """
    text = re.sub(r'(?m)^[ \t]*(?:\*[ \t]*){3,}$', '', text)
    text = re.sub(r'(?m)^([ \t]*)\*[ \t]+', r'\1', text)
    for pattern in _EMPHASIS:
        text = pattern.sub(r'\2', text)
    return text


class KoreanNormalizer:
    def __init__(self, lexicon_path=None):
        from malbut_tts.vendor import n2gk as module

        class PreservingN2gk(module.N2gkPlus):
            def remove_symbols(self, text, erase_in_parentheses=False):
                # A live assistant must not silently omit parenthetical content.
                return text

            def apply_word_mapping(self, text):
                known = {**self.COMMON_ABBR_MAPPING, **self.COMPANY_MAPPING}
                def convert(match):
                    token = match.group()
                    if token in known:
                        return known[token]
                    if token.isupper():
                        return ''.join(self.SINGLE_LETTER_MAPPING[c] for c in token)
                    return token  # Let Piper handle unknown whole English words.
                return re.sub(r'[A-Za-z]+', convert, text)

        self.normalizer = PreservingN2gk()
        # The upstream native-counter list omits the common packaging counter 팩.
        self.normalizer.UNIT_CATEGORIES.append(
            self.normalizer.UnitCategory(['팩'], 'native', self.normalizer))
        self.normalizer.unit_category_pairs = sorted(
            [(unit, cat) for cat in self.normalizer.UNIT_CATEGORIES for unit in cat.units],
            key=lambda pair: len(pair[0]), reverse=True)
        path = Path(lexicon_path) if lexicon_path else ROOT / 'config/korean_pronunciation_lexicon.json'
        self.lexicon = json.loads(path.read_text())
        self.pattern = re.compile(
            r'(?<![A-Za-z0-9_])(?:' + '|'.join(re.escape(k).replace(r'\ ', r'\s+') for k in sorted(self.lexicon, key=len, reverse=True))
            + r')(?![A-Za-z0-9_])', re.IGNORECASE)
        self.lookup = {k.casefold(): v for k, v in self.lexicon.items()}

    def prepare_numeric_readings(self, text):
        """Keep minute boundaries clear and read explicit identifiers digitwise.

        Only Arabic-number spans are rewritten, not existing Korean words such
        as 이분. Fractional minutes, signed values, and ordinal 번째 are left to
        the upstream normalizer. A small-number repetition remains native Korean.
        """
        number = r'(?<![0-9A-Za-z_.+\-])(?P<number>\d{1,3}(?:,\d{3})+|\d+)'
        def minute(match):
            return self.normalizer.to_hanja_int(int(match['number'].replace(',', ''))) + ' 분'
        text = re.sub(number + r'\s*분(?=$|[\s.,!?;:]|으로|부터|까지|마다|뒤|후|전|동안|쯤|은|는|을|에|이)', minute, text)
        def label(match):
            value = int(match['number'].replace(',', ''))
            prefix = text[:match.start()]
            suffix = text[match.end():]
            identifier = re.search(r'번호(?:는|가|를|로)?\s*$', prefix)
            if identifier:
                digits = '공일이삼사오육칠팔구'
                return ' '.join(digits[int(c)] for c in match['number'] if c.isdigit()) + ' 번'
            route = re.match(r'\s*(?:버스|출구|승강장|노선)', suffix)
            if value >= 100 or route:
                return self.normalizer.to_hanja_int(value) + '번'
            return match.group()
        return re.sub(number + r'\s*번(?!째)', label, text)

    def __call__(self, text):
        text = unicodedata.normalize('NFC', text)
        text = _remove_markdown_emphasis(text)
        text = self.pattern.sub(lambda m: self.lookup[' '.join(m.group().split()).casefold()], text)
        text = self.prepare_numeric_readings(text)
        return self.normalizer(text)

