"""Bounded, expiring RAM transport for patrol media; never spill pixels to disk."""

import hashlib
import json
import os
import re
import socket
import struct
import threading
import time


RETENTION_SECONDS = 900
SUFFIXES = {'.jpg', '.depth.npy', '.lidar.npy', '.regions.json'}


def address(directory):
    return '\0malbut-patrol-' + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:40]


class TransientMedia:
    """One producer serves only the same OS user, with a hard memory/age limit."""

    def __init__(self, directory, max_bytes=128 * 1024 * 1024, *, clock=time.monotonic):
        self.clock, self.max_bytes = clock, max_bytes
        self._lock = threading.RLock()
        self._items = {}
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._socket.bind(address(directory))
            self._socket.listen(4)
            self._socket.settimeout(.2)
        except Exception:
            self._socket.close()
            raise
        self._thread = threading.Thread(target=self._serve, name='patrol-ram-media', daemon=True)
        self._thread.start()

    def _prune(self):
        now = self.clock()
        for identity, (deadline, _) in list(self._items.items()):
            if deadline <= now:
                del self._items[identity]

    def put(self, identity, files):
        with self._lock:
            self._prune()
            size = sum(len(data) for data in files.values())
            used = sum(len(data) for _, group in self._items.values() for data in group.values())
            if (self._stop.is_set() or not re.fullmatch('[a-f0-9]{32}', identity)
                    or not set(files) <= SUFFIXES or used + size > self.max_bytes):
                raise OSError('transient capture capacity unavailable')
            self._items[identity] = (self.clock() + RETENTION_SECONDS, dict(files))

    def discard(self, identity):
        with self._lock:
            self._items.pop(identity, None)

    def _serve(self):
        while not self._stop.is_set():
            with self._lock:
                self._prune()
            try:
                client, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with client:
                try:
                    client.settimeout(2)
                    _, uid, _ = struct.unpack('3i', client.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                    if uid != os.getuid():
                        continue
                    request = bytearray()
                    while not request.endswith(b'\n') and len(request) <= 256:
                        part = client.recv(1)
                        if not part:
                            break
                        request.extend(part)
                    value = json.loads(request)
                    identity = value['id']
                    if not re.fullmatch('[a-f0-9]{32}', identity):
                        continue
                    with self._lock:
                        self._prune()
                        if value.get('op') == 'release':
                            self.discard(identity)
                            data = b''
                        else:
                            data = self._items.get(identity, (0, {}))[1].get(value['suffix'])
                    if data is None:
                        client.sendall(struct.pack('!I', 0xffffffff))
                    else:
                        client.sendall(struct.pack('!I', len(data)))
                        client.sendall(data)
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Never log request contents or raw media.
                finally:
                    # A disconnected consumer must not leave the last bytes in
                    # this idle server frame after the store expires/releases them.
                    data = None

    def close(self):
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=3)
        with self._lock:
            self._items.clear()
