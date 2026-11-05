# -*- coding: utf-8 -*-
"""A tiny stand-in for the premiumize.me API + download servers, for local testing.

python -I mock_premiumize.py VIDEO_FILE [port] [MB/s per connection]

API key: "testkey". GET /ctl?rate=N changes the per-connection speed (MB/s),
/ctl?stats=1 shows request counts.
"""
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIDEO = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 52990
STATE = {'rate': float(sys.argv[3]) if len(sys.argv) > 3 else 1.5, 'requests': 0, 'active': 0, 'peak': 0,
         'bytes': 0}
LOCK = threading.Lock()
KEY = 'testkey'
SRT = (b'1\n00:00:01,000 --> 00:00:06,000\nSubtitle test line one (%s)\n\n'
       b'2\n00:00:10,000 --> 00:00:20,000\nSecond subtitle line\n\n')

# id -> (name, parent, kind, source)  kind: folder | file ; source: 'video' or bytes
TREE = {
    'root': ('root', None, 'folder', None),
    'fm': ('Movies', 'root', 'folder', None),
    'fs': ('Shows', 'root', 'folder', None),
    'readme': ('Readme.txt', 'root', 'file', b'hello'),
    'ftm': ('Test.Movie.2026.720p', 'fm', 'folder', None),
    'v1': ('Test.Movie.2026.720p.avi', 'ftm', 'file', 'video'),
    's1': ('English.srt', 'ftm', 'file', SRT % b'English'),
    'fsubs': ('Subs', 'ftm', 'folder', None),
    's2': ('2_Arabic.srt', 'fsubs', 'file', SRT % b'Arabic'),
    'fss': ('Show S01', 'fs', 'folder', None),
    'e1': ('Show.S01E01.avi', 'fss', 'file', 'video'),
    'e2': ('Show.S01E02.avi', 'fss', 'file', 'video'),
    's3': ('Show.S01E01.en.srt', 'fss', 'file', SRT % b'E01'),
}


def size_of(item_id):
    src = TREE[item_id][3]
    return os.path.getsize(VIDEO) if src == 'video' else len(src)


def entry(item_id):
    name, parent, kind, src = TREE[item_id]
    e = {'id': item_id, 'name': name, 'type': kind, 'created_at': 1730000000}
    if kind == 'file':
        e.update(size=size_of(item_id), mime_type='video/x-msvideo' if src == 'video' else 'text/plain',
                 link='http://127.0.0.1:%d/dl/%s/%s?t=abc' % (PORT, item_id, urllib.parse.quote(name)))
    return e


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(parts.query))
        path = parts.path
        if path == '/ctl':
            if 'rate' in q:
                STATE['rate'] = float(q['rate'])
            return self._json(STATE)
        if path.startswith('/api/'):
            if self.headers.get('Authorization') != 'Bearer ' + KEY:
                return self._json({'status': 'error', 'code': 'authentication_failed', 'message': 'bad key'})
            return self._api(path[5:], q)
        if path.startswith('/dl/'):
            return self._download(path.split('/')[2])
        self._json({'error': 'nope'}, 404)

    def _api(self, method, q):
        if method == 'account/info':
            return self._json({'status': 'success', 'customer_id': 1, 'premium_until': time.time() + 86400 * 200,
                               'limit_used': 0.12})
        if method == 'folder/list':
            fid = q.get('id') or 'root'
            if fid not in TREE:
                return self._json({'status': 'error', 'code': 'not_found', 'message': 'not found'})
            kids = [entry(i) for i, v in TREE.items() if v[1] == fid]
            return self._json({'status': 'success', 'content': kids, 'name': TREE[fid][0],
                               'parent_id': TREE[fid][1], 'folder_id': fid})
        if method == 'item/details':
            item_id = q.get('id')
            if item_id not in TREE or TREE[item_id][2] != 'file':
                return self._json({'status': 'error', 'code': 'not_found', 'message': 'not found'})
            e = entry(item_id)
            e['folder_id'] = TREE[item_id][1]
            e['status'] = 'success'
            return self._json(e)
        if method == 'folder/search':
            needle = q.get('q', '').lower()
            hits = [entry(i) for i, v in TREE.items() if i != 'root' and needle in v[0].lower()]
            return self._json({'status': 'success', 'content': hits, 'name': 'search'})
        return self._json({'status': 'error', 'code': 'invalid_request', 'message': method})

    def _download(self, item_id):
        if item_id not in TREE:
            return self._json({'error': 'gone'}, 404)
        src = TREE[item_id][3]
        size = size_of(item_id)
        start, end = 0, size - 1
        rng = self.headers.get('Range')
        if rng:
            a, b = rng.split('=')[1].split('-')
            if a:
                start, end = int(a), (min(int(b), size - 1) if b else size - 1)
            else:
                start = size - int(b)
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Type', 'application/octet-stream')
        self.end_headers()
        with LOCK:
            STATE['requests'] += 1
            STATE['active'] += 1
            STATE['peak'] = max(STATE['peak'], STATE['active'])
        try:
            if src != 'video':
                self.wfile.write(src[start:end + 1])
                return
            with open(VIDEO, 'rb') as f:
                f.seek(start)
                sent, t0 = 0, time.time()
                while sent < length:
                    buf = f.read(min(64 * 1024, length - sent))
                    self.wfile.write(buf)
                    sent += len(buf)
                    with LOCK:
                        STATE['bytes'] += len(buf)
                    ahead = sent / (STATE['rate'] * 1048576) - (time.time() - t0)
                    if ahead > 0:
                        time.sleep(ahead)
        except (ConnectionError, OSError):
            pass
        finally:
            with LOCK:
                STATE['active'] -= 1


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    srv.handle_error = lambda *a: None
    print('mock premiumize on http://127.0.0.1:%d/api/  (key: %s, %.1f MB/s per connection)' % (PORT, KEY, STATE['rate']),
          flush=True)
    srv.serve_forever()
