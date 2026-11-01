"""Read transient patrol pixels over a local socket, without a disk fallback."""

import hashlib
import json
import re
import socket
import struct


def _receive(stream, size):
    parts = bytearray()
    while len(parts) < size:
        part = stream.recv(min(65536, size - len(parts)))
        if not part:
            raise OSError('transient capture incomplete')
        parts.extend(part)
    return bytes(parts)


def exchange(path, suffix=None, *, limit=0):
    directory = path.parent.parent.resolve()
    address = '\0malbut-patrol-' + hashlib.sha256(str(directory).encode()).hexdigest()[:40]
    request = {'id': path.stem, 'op': 'read' if suffix else 'release', 'suffix': suffix}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(3)
            stream.connect(address)
            stream.sendall(json.dumps(request).encode() + b'\n')
            size, = struct.unpack('!I', _receive(stream, 4))
            if size == 0xffffffff:
                raise FileNotFoundError('transient capture expired or released')
            if size > limit:
                raise ValueError('transient capture too large')
            return _receive(stream, size)
    except (ConnectionError, socket.timeout) as error:
        raise FileNotFoundError('transient capture unavailable') from error


def read_media(path, suffix, limit):
    metadata = json.loads(path.read_text())
    if metadata.get('media_storage') == 'volatile_v1':
        return exchange(path, suffix, limit=limit)
    evidence = path.with_suffix(suffix)
    if evidence.is_symlink() or not 0 < evidence.stat().st_size <= limit:
        raise ValueError('invalid capture media')
    return evidence.read_bytes()


def release_media(path):
    """Release RAM and remove legacy source files after information commit or failure."""
    if path.parent.is_symlink() or not re.fullmatch('[a-f0-9]{32}', path.stem):
        raise ValueError('invalid capture release path')
    try:
        exchange(path)
    except (OSError, ValueError):
        pass  # A stopped producer has already lost its RAM buffer.
    for suffix in ('.jpg', '.depth.npy', '.lidar.npy', '.regions.json'):
        path.with_suffix(suffix).unlink(missing_ok=True)
