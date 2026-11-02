"""Resolve a caller's location without guessing coordinates or invoking a model."""

import re
import unicodedata


def caller_location(text, names):
    """Accept one explicitly named current location, never a negated/ambiguous one."""
    if not isinstance(text, str) or not 0 < len(text) <= 256:
        return None
    text = unicodedata.normalize('NFC', text).strip(' .!?。')
    if re.search(r'아니|말고|없|가지|오지|나중|갈게|갈 거|갔다|있었|모르|이랑|하고|사이|또는'
                 r'|안\s*있|않|아닌|아마|같아|같아요|있나', text):
        return None
    matches = []
    for name in names:
        # Korean particles/endings are permitted, but not a room-name substring.
        pattern = r'(?<![가-힣\w])' + re.escape(name) + r'(?=$|\s|[,.!?]|에|야|이야|이에요|예요)'
        if re.search(pattern, text):
            matches.append(name)
    return matches[0] if len(matches) == 1 else None


def cancel_approach(text):
    return bool(re.search(r'멈춰|멈추|오지\s*마|오지\s*않|취소|그만|거기\s*있어', text))


def prepare_caller_navigation(missions, request_id, text):
    location = caller_location(text, missions.navigation_locations())
    if location is None:
        return None
    return missions.prepare_conversation_navigation(request_id, location)
