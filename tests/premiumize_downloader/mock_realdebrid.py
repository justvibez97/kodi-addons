# -*- coding: utf-8 -*-
"""A tiny stand-in for the Real-Debrid API + download servers, for local testing.

python -I mock_realdebrid.py VIDEO_FILE [port] [MB/s per connection]

Device pairing: code "ABCD1234" is approved after GET /ctl?approve=1.
Private API token: "rdkey". GET /ctl?revoke=1 makes every issued access
token invalid (as if it expired early), /ctl?rate=N changes the speed,
/ctl?chunks=N the connection limit unrestrict reports, /ctl shows counters.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIDEO = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 52995
STATE = {'rate': float(sys.argv[3]) if len(sys.argv) > 3 else 1.5, 'approved': False, 'chunks': 16,
         'tokens': [], 'refreshes': 0, 'unrestricts': 0, 'requests': 0, 'active': 0, 'peak': 0,
         'expires_in': 3600, 'polls': 0}
LOCK = threading.Lock()
API_TOKEN = 'rdkey'
CLIENT_ID, CLIENT_SECRET, DEVICE_CODE, REFRESH = 'CID123', 'SECRET456', 'DEVCODE789', 'REFRESH000'
SRT = b'1\n00:00:01,000 --> 00:00:06,000\nReal-Debrid subtitle\n\n'
RAR = b'Rar!' + b'\x00' * 2000

# restricted link code -> (file name, source)   source: 'video' or bytes
LINKS = {
    'L1': ('Test.Movie.2026.1080p.avi', 'video'),
    'L2': ('English.srt', SRT),
    'L3': ('Single.Movie.2025.720p.avi', 'video'),
    'L4': ('Show.S01E01.avi', 'video'),
    'L5': ('Show.S01E02.avi', 'video'),
    'L6': ('Packed.Pack.rar', RAR),
}


def f(fid, path, size, selected=1):
    return {'id': fid, 'path': path, 'bytes': size, 'selected': selected}


def vsize():
    return os.path.getsize(VIDEO)


def link(code):
    return 'https://real-debrid.com/d/' + code


TORRENTS = {
    'T1': {'filename': 'Test.Movie.2026.1080p', 'added': '2026-11-01T10:00:00.000Z', 'status': 'downloaded',
           'files': lambda: [f(1, '/Test.Movie.2026.1080p.avi', vsize()), f(2, '/Subs/English.srt', len(SRT)),
                             f(3, '/sample.avi', 1000, 0), f(4, '/info.nfo', 10, 0)],
           'links': ['L1', 'L2']},
    'T2': {'filename': 'Single.Movie.2025.720p.avi', 'added': '2026-11-05T10:00:00.000Z', 'status': 'downloaded',
           'files': lambda: [f(1, '/Single.Movie.2025.720p.avi', vsize())], 'links': ['L3']},
    'T3': {'filename': 'Show.S01.1080p', 'added': '2026-11-03T10:00:00.000Z', 'status': 'downloaded',
           'files': lambda: [f(1, '/Show.S01E01.avi', vsize()), f(2, '/Show.S01E02.avi', vsize())],
           'links': ['L4', 'L5']},
    'T4': {'filename': 'Packed.Pack', 'added': '2026-10-01T10:00:00.000Z', 'status': 'downloaded',
           'files': lambda: [f(1, '/a.txt', 10), f(2, '/b.txt', 10), f(3, '/c.txt', 10)], 'links': ['L6']},
    'T5': {'filename': 'Still.Downloading.2026', 'added': '2026-11-06T10:00:00.000Z', 'status': 'downloading',
           'progress': 42, 'files': lambda: [f(1, '/Still.Downloading.2026.mkv', 5000)], 'links': []},
}


def torrent_json(tid, info=False):
    t = TORRENTS[tid]
    files = t['files']()
    out = {'id': tid, 'filename': t['filename'], 'hash': 'h' + tid, 'host': 'real-debrid.com', 'split': 2000,
           'progress': t.get('progress', 100), 'status': t['status'], 'added': t['added'],
           'bytes': sum(x['bytes'] for x in files if x['selected']),
           'links': [link(c) for c in t['links']]}
    if info:
        out.update(files=files, original_filename=t['filename'], original_bytes=sum(x['bytes'] for x in files))
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, data, code=200, headers=None):
        body = b'' if data is None else json.dumps(data).encode()
        self.send_response(code)
        if data is not None:
            self.send_header('Content-Type', 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, http, error, code):
        self._json({'error': error, 'error_code': code}, http)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self._body = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode())) if length else {}
        self._route('POST')

    def do_GET(self):
        self._body = {}
        self._route('GET')

    def do_DELETE(self):
        self._body = {}
        self._route('DELETE')

    def _route(self, method):
        parts = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(parts.query))
        q.update(self._body)
        path = parts.path
        if path == '/ctl':
            for key in ('rate', 'chunks', 'expires_in'):
                if key in q:
                    STATE[key] = float(q[key]) if key == 'rate' else int(q[key])
            if q.get('approve'):
                STATE['approved'] = True
            if q.get('revoke'):
                STATE['tokens'] = []
            if q.get('reset_counts'):
                STATE.update(requests=0, peak=0)
            return self._json(dict(STATE, tokens=len(STATE['tokens'])))
        if path.startswith('/oauth/v2/'):
            return self._oauth(method, path[len('/oauth/v2/'):], q)
        if path.startswith('/rest/1.0/'):
            auth = self.headers.get('Authorization') or ''
            token = auth[7:] if auth.startswith('Bearer ') else q.get('auth_token')
            if token != API_TOKEN and token not in STATE['tokens']:
                return self._err(401, 'bad_token', 8)
            return self._rest(method, path[len('/rest/1.0/'):], q)
        if path.startswith('/dl/'):
            return self._download(path.split('/')[2])
        self._json({'error': 'nope'}, 404)

    def _issue(self):
        token = 'tok%d' % (len(STATE['tokens']) + STATE['refreshes'] + 1)
        STATE['tokens'].append(token)
        return {'access_token': token, 'expires_in': STATE['expires_in'], 'token_type': 'Bearer',
                'refresh_token': REFRESH}

    def _oauth(self, method, what, q):
        if what == 'device/code' and method == 'GET':
            return self._json({'device_code': DEVICE_CODE, 'user_code': 'ABCD1234', 'interval': 1,
                               'expires_in': 60, 'verification_url': 'https://real-debrid.com/device'})
        if what == 'device/credentials' and method == 'GET':
            STATE['polls'] += 1
            if q.get('code') != DEVICE_CODE or not STATE['approved']:
                return self._json({'error': None, 'error_code': None}, 403)
            return self._json({'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET})
        if what == 'token' and method == 'POST':
            if (q.get('client_id'), q.get('client_secret')) != (CLIENT_ID, CLIENT_SECRET) or \
                    q.get('grant_type') != 'http://oauth.net/grant_type/device/1.0':
                return self._err(400, 'wrong_parameter', 2)
            if q.get('code') == REFRESH:
                STATE['refreshes'] += 1
                return self._json(self._issue())
            if q.get('code') == DEVICE_CODE and STATE['approved']:
                return self._json(self._issue())
            return self._err(400, 'wrong_parameter', 2)
        return self._err(404, 'unknown_method', 3)

    def _rest(self, method, what, q):
        if what == 'user' and method == 'GET':
            return self._json({'id': 1, 'username': 'mockuser', 'email': 'x', 'points': 10, 'locale': 'en',
                               'avatar': '', 'type': 'premium', 'premium': 86400 * 100,
                               'expiration': '2027-02-20T10:00:00.000Z'})
        if what == 'torrents' and method == 'GET':
            items = [torrent_json(t) for t in sorted(TORRENTS)]
            limit = int(q.get('limit') or 100)
            if 'offset' in q and int(q['offset']) == 0:
                # What the real Real-Debrid does (checked 2026-11-13): offset=0 gets
                # "204 No Content" even when the account has torrents.
                return self._json(None, 204)
            if 'page' in q:
                offset = (int(q['page']) - 1) * limit
            else:
                offset = int(q.get('offset') or 0)
            page = items[offset:offset + limit]
            if limit > 100:
                # Also like the real one: asked for more than 100, it leaves out the links.
                page = [dict(t, links=[]) for t in page]
            if not page:
                return self._json(None, 204, {'X-Total-Count': str(len(items))})
            return self._json(page, 200, {'X-Total-Count': str(len(items))})
        m = re.match(r'^torrents/info/(\w+)$', what)
        if m and method == 'GET':
            if m.group(1) not in TORRENTS:
                return self._err(404, 'unknown_ressource', 7)
            return self._json(torrent_json(m.group(1), info=True))
        m = re.match(r'^torrents/delete/(\w+)$', what)
        if m and method == 'DELETE':
            if TORRENTS.pop(m.group(1), None) is None:
                return self._err(404, 'unknown_ressource', 7)
            return self._json(None, 204)
        if what == 'unrestrict/link' and method == 'POST':
            code = (q.get('link') or '').rsplit('/', 1)[-1]
            if code not in LINKS:
                return self._err(503, 'unavailable_file', 24)
            STATE['unrestricts'] += 1
            name, src = LINKS[code]
            size = vsize() if src == 'video' else len(src)
            return self._json({'id': 'D' + code, 'filename': name, 'mimeType': 'video/x-msvideo' if src == 'video'
                               else 'application/octet-stream', 'filesize': size, 'link': q['link'],
                               'host': 'real-debrid.com', 'chunks': STATE['chunks'], 'crc': 1,
                               'download': 'http://127.0.0.1:%d/dl/%s/%s' % (PORT, code, urllib.parse.quote(name)),
                               'streamable': 1})
        return self._err(404, 'unknown_method', 3)

    def _download(self, code):
        if code not in LINKS:
            return self._json({'error': 'gone'}, 404)
        src = LINKS[code][1]
        size = vsize() if src == 'video' else len(src)
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
        # Real-Debrid's download servers close the connection after every response.
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        with LOCK:
            STATE['requests'] += 1
            STATE['active'] += 1
            STATE['peak'] = max(STATE['peak'], STATE['active'])
        try:
            if src != 'video':
                self.wfile.write(src[start:end + 1])
                return
            with open(VIDEO, 'rb') as fh:
                fh.seek(start)
                sent, t0 = 0, time.time()
                while sent < length:
                    buf = fh.read(min(64 * 1024, length - sent))
                    self.wfile.write(buf)
                    sent += len(buf)
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
    print('mock Real-Debrid on http://127.0.0.1:%d/rest/1.0/ (%.1f MB/s per connection)' % (PORT, STATE['rate']),
          flush=True)
    srv.serve_forever()
