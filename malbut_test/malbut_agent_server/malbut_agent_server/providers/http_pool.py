"""Bounded direct HTTP connections, without implicit request retries."""

import http.client
import io
import select
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def uses_environment_proxy(url):
    """Keep urllib's environment/system proxy and bypass behavior."""
    parsed = urllib.parse.urlsplit(url)
    return bool(urllib.request.getproxies().get(parsed.scheme)
                and not urllib.request.proxy_bypass(parsed.netloc))


class HTTPConnectionPool:
    """Lease each connection to one caller; never replay a failed POST.

    Only direct connections use this pool. Proxy requests continue through
    the existing urllib transport, including its authentication/TLS policy.
    Closing rejects new work and wakes waiters; active requests retain their
    socket timeout and discard their connection when returning it.
    """

    def __init__(self, *, max_connections=4, idle_timeout=30.0):
        """Create no socket until a request actually needs one."""
        if type(max_connections) is not int or max_connections < 1:
            raise ValueError('max_connections must be a positive integer')
        self._condition = threading.Condition()
        self._idle = []
        self._count = 0
        self._closed = False
        self._max_connections = max_connections
        self._idle_timeout = idle_timeout

    def check_open(self):
        """Reject requests after explicit shutdown, including proxy calls."""
        with self._condition:
            if self._closed:
                raise OSError('HTTP transport is closed')

    def close(self):
        """Close idle sockets now and active sockets when their lease ends."""
        with self._condition:
            self._closed = True
            for _origin, connection, _at in self._idle:
                connection.close()
                self._count -= 1
            self._idle.clear()
            self._condition.notify_all()

    @staticmethod
    def _usable(connection):
        sock = connection.sock
        if sock is None:
            return False
        try:
            # No response data should remain after a complete bounded read.
            # Readability while idle also detects peer EOF before sending.
            return not select.select([sock], [], [], 0)[0]
        except (OSError, ValueError):
            return False

    def _acquire(self, origin, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._closed:
                    raise OSError('HTTP transport is closed')
                now = time.monotonic()
                retained = []
                for old_origin, connection, at in self._idle:
                    if now - at >= self._idle_timeout or not self._usable(connection):
                        connection.close()
                        self._count -= 1
                    else:
                        retained.append((old_origin, connection, at))
                self._idle = retained
                remaining = deadline - now
                if remaining <= 0:
                    raise TimeoutError('HTTP connection lease timed out')
                for index, (old_origin, connection, _at) in enumerate(self._idle):
                    if old_origin == origin:
                        self._idle.pop(index)
                        connection.timeout = remaining
                        connection.sock.settimeout(remaining)
                        return connection
                # Another origin's idle connection must not block this origin.
                if self._count >= self._max_connections and self._idle:
                    _, connection, _ = self._idle.pop(0)
                    connection.close()
                    self._count -= 1
                if self._count < self._max_connections:
                    scheme, host, port = origin
                    cls = (http.client.HTTPSConnection if scheme == 'https'
                           else http.client.HTTPConnection)
                    # HTTPSConnection defaults to certificate/hostname checking
                    # and the system trust store, as urllib's HTTPS handler does.
                    connection = cls(host, port, timeout=remaining)
                    self._count += 1
                    return connection
                self._condition.wait(remaining)

    def post(self, url, headers, body, timeout, max_bytes):
        """Send exactly one POST and return a complete, size-bounded body."""
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            raise ValueError('invalid direct HTTP URL')
        origin = (parsed.scheme, parsed.hostname,
                  parsed.port or (443 if parsed.scheme == 'https' else 80))
        target = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        connection = self._acquire(origin, timeout)
        reusable = False
        try:
            connection.request('POST', target, body=body, headers=headers)
            response = connection.getresponse()
            try:
                if not 200 <= response.status < 300:
                    # Only quota classification needs an error body. Redirects
                    # are never followed, even on the same origin.
                    detail = b''
                    if response.status == 429:
                        try:
                            detail = response.read(4097)
                        except (OSError, http.client.HTTPException):
                            pass  # A broken quota body must not erase HTTP 429.
                    raise urllib.error.HTTPError(
                        url, response.status, response.reason,
                        response.headers, io.BytesIO(detail),
                    )
                result = response.read(max_bytes + 1)
                if len(result) <= max_bytes and response.length not in (None, 0):
                    raise http.client.IncompleteRead(result, response.length)
                reusable = (len(result) <= max_bytes and not response.will_close
                            and connection.sock is not None)
                return result
            finally:
                response.close()
        finally:
            with self._condition:
                if reusable and not self._closed:
                    self._idle.append((origin, connection, time.monotonic()))
                else:
                    connection.close()
                    self._count -= 1
                self._condition.notify_all()
