"""Offline log viewer with editable session labels, but no robot control API."""

import argparse
from collections import deque
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse


class LogServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root):
        self.root = Path(root).expanduser().resolve()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, content, content_type='application/json; charset=utf-8', etag=None):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        if etag:
            self.send_header('ETag', etag)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                         "connect-src 'self'")
        self.end_headers()
        self.wfile.write(content)

    def json(self, value, etag=None):
        self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), etag=etag)

    def unchanged(self, paths):
        """Skip reading unchanged logs; signatures include the requested time range."""
        signatures = []
        for path in paths:
            try:
                stat = path.stat()
                signatures.append((str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                signatures.append((str(path), None))
        etag = '"' + hashlib.sha256(repr((self.path, signatures)).encode()).hexdigest() + '"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return None
        return etag

    def session(self, query):
        name = query.get('session', [''])[0]
        if not name or Path(name).name != name:
            raise ValueError('Invalid session')
        path = (self.server.root / name).resolve()
        if path.parent != self.server.root or not path.is_dir():
            raise ValueError('Session outside log root')
        return path

    @staticmethod
    def metadata(session):
        path = session / 'metadata.json'
        if path.resolve().parent != session:
            raise ValueError('Metadata outside session')
        return json.loads(path.read_text())

    @staticmethod
    def display_name(session):
        path = session / 'viewer.json'
        if path.is_symlink():
            raise ValueError('Viewer settings must not be a symlink')
        try:
            return json.loads(path.read_text()).get('display_name', '')
        except FileNotFoundError:
            return ''

    def do_POST(self):
        """Only edit a display label; collector metadata and samples stay immutable."""
        temporary = None
        try:
            url = urlparse(self.path)
            if url.path != '/api/session/name':
                self.send_error(404)
                return
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                self.send_error(403, 'Cross-origin changes are not allowed')
                return
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('Expected application/json')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size')
            body = json.loads(self.rfile.read(length))
            name = body.get('name') if isinstance(body, dict) else None
            if (not isinstance(name, str) or len(name.strip()) > 80
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)):
                raise ValueError('Name must be at most 80 characters, without control characters')
            session = self.session(parse_qs(url.query))
            self.metadata(session)  # Only existing measurement sessions may be labelled.
            self.display_name(session)  # Reject symlinks before writing.
            with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', dir=session, prefix='.viewer-',
                    delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({'display_name': name.strip()}, stream, ensure_ascii=False)
            os.replace(temporary, session / 'viewer.json')
            temporary = None
            self.json({'session': session.name, 'display_name': name.strip()})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError, KeyError) as error:
            self.send_error(400, str(error))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def do_GET(self):
        try:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == '/':
                self.send(Path(__file__).with_name('viewer.html').read_bytes(),
                          'text/html; charset=utf-8')
                return
            if url.path == '/api/sessions':
                paths = [path for path in
                         sorted(self.server.root.glob('*/metadata.json'), reverse=True)
                         if not path.is_symlink() and not path.parent.is_symlink()]
                etag = self.unchanged([
                    file for path in paths for file in (path, path.with_name('viewer.json'))])
                if etag is None:
                    return
                sessions = []
                for path in paths:
                    try:
                        data = json.loads(path.read_text())
                        sessions.append({
                            **{k: data.get(k) for k in
                               ('session', 'hostname', 'started_wall_ns', 'finished')},
                            'display_name': self.display_name(path.parent),
                        })
                    except (OSError, ValueError):
                        continue
                self.json(sessions, etag)
                return
            session = self.session(query)
            if url.path == '/api/session':
                path = session / 'metadata.json'
                if path.resolve().parent != session:
                    raise ValueError('Metadata outside session')
                etag = self.unchanged([path, session / 'viewer.json'])
                if etag is not None:
                    self.json({**self.metadata(session),
                               'display_name': self.display_name(session)}, etag)
                return
            metadata = self.metadata(session)
            channel = query.get('channel', [''])[0]
            if channel not in metadata['channels']:
                raise ValueError('Unknown channel')
            path = (session / (channel + '.jsonl')).resolve()
            if not path.is_relative_to(session):
                raise ValueError('Channel outside session')
            if url.path == '/api/download':
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson')
                self.send_header('Content-Disposition', f'attachment; filename="{path.name}"')
                self.send_header('X-Content-Type-Options', 'nosniff')
                # A recording may still grow. Stream only the size seen at opening.
                with path.open('rb') as stream:
                    remaining = path.stat().st_size
                    self.send_header('Content-Length', str(remaining))
                    self.end_headers()
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            if url.path != '/api/data':
                self.send_error(404)
                return
            etag = self.unchanged([path])
            if etag is None:
                return
            start = float(query.get('start', ['0'])[0])
            end = float(query.get('end', ['inf'])[0])
            rows, count, malformed = deque(maxlen=50000), 0, 0
            if path.exists():
                with path.open() as stream:
                    for line in stream:
                        if not line.endswith('\n'):  # concurrent partial write, retry next read
                            break
                        try:
                            row = json.loads(line)
                        except ValueError:
                            malformed += 1
                            continue
                        if start <= row['t'] <= end:
                            count += 1
                            rows.append(row)
            self.json({'rows': list(rows), 'truncated': count > len(rows),
                       'matching_rows': count, 'malformed_rows': malformed}, etag)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError, KeyError) as error:
            self.send_error(400, str(error))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='~/.ros/malbut/resource_logs')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args(argv)
    with LogServer((args.host, args.port), args.root) as server:
        print(f'Log viewer (session labels editable): http://{args.host}:{server.server_port}',
              flush=True)
        print('No authentication: bind to LAN only on a trusted network. No robot control.',
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
