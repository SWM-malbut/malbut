"""HTTP helpers for requests that carry the device credential."""

from urllib.request import HTTPRedirectHandler, OpenerDirector, build_opener


class _NoRedirectHandler(HTTPRedirectHandler):
    """Turn every HTTP redirect into an HTTPError without forwarding headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Refuse creation of a redirected request."""
        del req, fp, code, msg, headers, newurl
        return None


def build_no_redirect_opener() -> OpenerDirector:
    """Build the HTTPS opener used for credential-bearing device requests."""
    return build_opener(_NoRedirectHandler())
