"""Network destination policy for remote model credentials."""

import urllib.parse


OFFICIAL_OPENAI_BASE_URL = 'https://api.openai.com/v1'


def is_official_openai_base_url(value: str) -> bool:
    """Return whether a URL is exactly the official OpenAI API base."""
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value.strip().rstrip('/'))
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == 'https'
        and parsed.hostname == 'api.openai.com'
        and port in {None, 443}
        and parsed.path.rstrip('/') == '/v1'
        and not parsed.username
        and not parsed.password
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )
