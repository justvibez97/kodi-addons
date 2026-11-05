# -*- coding: utf-8 -*-
"""Local HTTP server run by the service (127.0.0.1 only).

* ``/stream/<job id>/<name>`` lets Kodi's player open a file that is still
  downloading. Bytes already on disk are served from disk; bytes that
  aren't there yet (including a seek far ahead) are fetched straight from
  premiumize, while the download keeps running.
* ``/api/...`` is how the plugin side (a separate Kodi Python instance)
  talks to the download queue.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import common, engine

BLOCK = 1024 * 1024
SWITCH_BACK = 8 * 1024 * 1024


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two programs share a port: don't.
    allow_reuse_address = os.name != 'nt'

    def __init__(self, *args, **kwargs):
        # Kodi waits for every Python thread before it can unload the service,
        # so each open connection is tracked and closed on shutdown. (Kodi's
        # own player keeps idle keep-alive connections open otherwise.)
        self._open = set()
        self._open_lock = threading.Lock()
        ThreadingHTTPServer.__init__(self, *args, **kwargs)

    def process_request(self, request, client_address):
        with self._open_lock:
            self._open.add(request)
        ThreadingHTTPServer.process_request(self, request, client_address)

    def shutdown_request(self, request):
        with self._open_lock:
            self._open.discard(request)
        ThreadingHTTPServer.shutdown_request(self, request)

    def close_connections(self):
        with self._open_lock:
            open_now = list(self._open)
        for sock in open_now:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def handle_error(self, request, client_address):
        pass


class _ClientGone(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PremiumizeDL/1.0'
    timeout = 20            # idle keep-alive connections end after this
    manager = None
    token = None

    def log_message(self, fmt, *args):
        common.debug('http: ' + re.sub(r'token=\w+', 'token=...', fmt % args))

    # ------------------------------------------------------------- routing

    def do_HEAD(self):
        self._route(head=True)

    def do_GET(self):
        self._route(head=False)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path != '/api/cmd':
            return self._send_json({'error': 'not found'}, 404)
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except ValueError:
            return self._send_json({'error': 'bad json'}, 400)
        if body.get('token') != self.token:
            return self._send_json({'error': 'forbidden'}, 403)
        try:
            result = self._command(body.get('cmd'), body.get('args') or {})
        except Exception as exc:
            common.log('command %s failed: %s' % (body.get('cmd'), exc))
            return self._send_json({'error': str(exc)}, 500)
        self._send_json({'ok': True, 'result': result})

    def _route(self, head):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path == '/api/status':
            query = urllib.parse.parse_qs(parts.query)
            if (query.get('token') or [''])[0] != self.token:
                return self._send_json({'error': 'forbidden'}, 403)
            return self._send_json({'jobs': self.manager.snapshot(), 'active': self.manager.active()})
        m = re.match(r'^/stream/([^/]+)/', path)
        if m:
            return self._stream(urllib.parse.unquote(m.group(1)), head)
        self._send_json({'error': 'not found'}, 404)

    def _command(self, cmd, args):
        mgr = self.manager
        job_id = args.get('id')
        if cmd == 'enqueue':
            return mgr.enqueue(args['items'], args['root'], start_id=args.get('start_id'))
        if cmd == 'start_now':
            return mgr.start_now(job_id)
        simple = {'pause': mgr.pause, 'resume': mgr.resume, 'cancel': mgr.cancel,
                  'delete_local': mgr.delete_local, 'forget': mgr.forget}
        if cmd in simple:
            return simple[cmd](job_id)
        bulk = {'pause_all': mgr.pause_all, 'resume_all': mgr.resume_all,
                'clear_finished': mgr.clear_finished}
        if cmd in bulk:
            return bulk[cmd]()
        raise ValueError('unknown command %r' % cmd)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    # ------------------------------------------------------------- streaming

    def _stream(self, job_id, head):
        src = self.manager.stream_source(job_id)
        if src is None:
            return self._send_json({'error': 'unknown file'}, 404)
        size = src.size
        start, end = 0, size - 1
        rng = self.headers.get('Range')
        partial = False
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self._range_error(size)
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                return self._range_error(size)
            partial = True
        try:
            self.send_response(206 if partial else 200)
            self.send_header('Content-Type', common.mime_for(src.name))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if partial:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            self.end_headers()
            if head:
                return
            # No timeout while streaming: a paused player stops reading for as
            # long as it likes. Shutdown still ends it by closing the socket.
            self.connection.settimeout(None)
            self._pump(src, start, end)
        except _ClientGone:
            pass
        except (ConnectionError, OSError) as exc:
            common.debug('stream %s ended: %s' % (job_id, exc))
            self.close_connection = True
        except engine.DownloadError as exc:
            common.log('stream %s failed: %s' % (job_id, exc))
            self.close_connection = True

    def _range_error(self, size):
        self.send_response(416)
        self.send_header('Content-Range', 'bytes */%d' % size)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _write(self, data):
        try:
            self.wfile.write(data)
        except (ConnectionError, OSError):
            raise _ClientGone()

    def _pump(self, src, pos, end):
        remote = None
        waiting_since = None
        misses = 0
        try:
            while pos <= end:
                have = src.written()
                if pos < have:
                    if remote is not None:
                        remote.close()
                        remote = None
                    want = min(BLOCK, have - pos, end - pos + 1)
                    try:
                        with open(src.path(), 'rb') as f:
                            f.seek(pos)
                            data = f.read(want)
                    except OSError:
                        data = b''      # renamed from .part to its final name just now
                    if not data:
                        misses += 1
                        if misses > 100:
                            raise engine.DownloadError('local file vanished')
                        time.sleep(0.05)
                        continue
                    misses = 0
                    waiting_since = None
                    self._write(data)
                    pos += len(data)
                    continue

                if remote is None:
                    # Not on disk yet. Wait if the download is about to get here,
                    # otherwise read this part straight from premiumize.
                    if src.downloading():
                        now = time.time()
                        waiting_since = waiting_since or now
                        waited = now - waiting_since
                        speed = src.speed()
                        gap = pos - have
                        soon = speed > 0 and gap / speed < 3.0
                        if (soon and waited < 8) or (speed == 0 and waited < 3):
                            time.sleep(0.1)
                            continue
                    remote = _Remote(src, pos, end)
                data = remote.read(min(BLOCK, end - pos + 1))
                self._write(data)
                pos += len(data)
                if src.written() > pos + SWITCH_BACK:
                    remote.close()      # the download overtook us: back to the local file
                    remote = None
        finally:
            if remote is not None:
                remote.close()


class _Remote(object):
    """A ranged read from premiumize for bytes the download hasn't reached."""

    def __init__(self, src, start, end):
        self.conn = engine.Connection(timeout=30)
        last = None
        for refresh in (False, True):
            url = src.link(refresh)
            for _hop in range(6):
                resp = self.conn.get(url, start, end)
                if resp.status not in (301, 302, 303, 307, 308):
                    break
                location = resp.getheader('Location')
                self.conn.close()
                url = urllib.parse.urljoin(url, location or '')
            try:
                engine.check_status(resp)
            except engine.LinkExpired as exc:
                self.conn.close()
                last = exc
                continue
            if resp.status != 206 and start > 0:
                self.conn.close()
                raise engine.DownloadError('premiumize ignored the byte range')
            self.resp = resp
            return
        raise last

    def read(self, n):
        data = self.resp.read(n)
        if not data:
            raise engine.DownloadError('premiumize closed the stream')
        return data

    def close(self):
        self.conn.close()


class LocalServer(object):
    def __init__(self, manager, token, port):
        Handler.manager = manager
        Handler.token = token
        self.httpd = None
        for candidate in (port, 0):
            try:
                self.httpd = _Server(('127.0.0.1', candidate), Handler)
                break
            except OSError as exc:
                common.log('port %d unavailable (%s)' % (candidate, exc))
        if self.httpd is None:
            raise RuntimeError('could not start the local server')
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='pmdl-http')
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        try:
            self.httpd.close_connections()
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd.close_connections()
        except Exception:
            pass
