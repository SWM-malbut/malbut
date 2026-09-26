"""Offline, read-only HTTP log viewer. No ROS import or robot control API."""

import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class LogServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root):
        self.root = Path(root).expanduser().resolve()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, content, content_type='application/json; charset=utf-8'):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                         "connect-src 'self'")
        self.end_headers()
        self.wfile.write(content)

    def json(self, value):
        self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

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

    def do_GET(self):
        try:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == '/':
                self.send(Path(__file__).with_name('viewer.html').read_bytes(),
                          'text/html; charset=utf-8')
                return
            if url.path == '/api/sessions':
                sessions = []
                for path in sorted(self.server.root.glob('*/metadata.json'), reverse=True):
                    if path.is_symlink() or path.parent.is_symlink():
                        continue
                    try:
                        data = json.loads(path.read_text())
                        sessions.append({k: data.get(k) for k in
                                         ('session', 'hostname', 'started_wall_ns', 'finished')})
                    except (OSError, ValueError):
                        continue
                self.json(sessions)
                return
            session = self.session(query)
            metadata = self.metadata(session)
            if url.path == '/api/session':
                self.json(metadata)
                return
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
                       'matching_rows': count, 'malformed_rows': malformed})
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
        print(f'Read-only log viewer: http://{args.host}:{server.server_port}', flush=True)
        print('No authentication: bind to LAN only on a trusted network. No robot control.',
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
