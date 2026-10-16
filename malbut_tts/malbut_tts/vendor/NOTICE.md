`n2gk.py` is copied from CoreaSpeech's `src/module/data_conditioning/normalization.py`,
revision `424229a8affea08848e2e647c0d3e471b3ef9d39`:
https://github.com/CoreaSpeech/CoreaSpeech/blob/424229a8affea08848e2e647c0d3e471b3ef9d39/src/module/data_conditioning/normalization.py

It includes the previously evaluated fix filtering `str.maketrans` keys to
length one. Trailing whitespace has also been removed. The v2.1 adapter and pronunciation dictionary live in
`korean_normalization.py` and `config/korean_pronunciation_lexicon.json`.
These are the rules selected in the local Piper listening comparison on
2026-10-16. This file records provenance, not an upstream license grant.
