# -*- coding: utf-8 -*-
"""Exercise engine.Downloader against a local server that misbehaves on purpose.

Run:  python -I tests/premiumize_downloader/test_engine.py <workdir>
"""
import hashlib
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'plugin.video.premiumize.downloader', 'resources', 'lib', 'pmdl'))
import engine  # noqa: E402

MB = 1024 * 1024


class Behaviour(object):
    per_conn_rate = 6 * MB        # bytes/s per connection (simulates a WAN path)
    latency = 0.05                # seconds before each response
    drop_chance = 0.0             # chance per response of cutting the connection midway
    max_conns = 99                # more concurrent requests than this get over_limit_code
    over_limit_code = 503
    valid_token = 'a'             # requests with another ?t= get HTTP 403
    ranges = True
    requests = 0
    active = 0


B = Behaviour()
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    data_path = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        with LOCK:
            B.requests += 1
            B.active += 1
            active = B.active
        try:
            self._get(active)
        finally:
            with LOCK:
                B.active -= 1

    def _get(self, active):
        time.sleep(B.latency)
        if self.path.startswith('/redirect'):
            self.send_response(302)
            self.send_header('Location', '/file.bin?t=' + B.valid_token)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        token = self.path.split('t=')[-1] if 't=' in self.path else ''
        if token != B.valid_token:
            return self._error(403)
        if active > B.max_conns:
            return self._error(B.over_limit_code)
        size = os.path.getsize(self.data_path)
        rng = self.headers.get('Range')
        start, end = 0, size - 1
        if rng and B.ranges:
            a, b = rng.split('=')[1].split('-')
            start = int(a)
            end = int(b) if b else size - 1
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        drop_at = None
        if B.drop_chance and length > 2 * MB and random.random() < B.drop_chance:
            drop_at = random.randint(MB, length - MB)
        with open(self.data_path, 'rb') as f:
            f.seek(start)
            sent = 0
            t0 = time.time()
            while sent < length:
                n = min(64 * 1024, length - sent)
                if drop_at is not None and sent >= drop_at:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                buf = f.read(n)
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                sent += n
                ahead = sent / B.per_conn_rate - (time.time() - t0)
                if ahead > 0:
                    time.sleep(ahead)

    def _error(self, code):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.end_headers()


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(4 * MB), b''):
            h.update(block)
    return h.hexdigest()


def run(label, workdir, base, size, expect_sha, connections=8, stop_after=None,
        max_buffer=64 * MB, url_path='/file.bin', refresh_token=None, resume=False):
    part = os.path.join(workdir, 'out.part')
    if not resume and os.path.exists(part):
        os.remove(part)
    state = {'refreshes': 0, 'token': 'a'}   # the link we start with may be stale

    def provider(refresh):
        if refresh:
            state['refreshes'] += 1
            if refresh_token:
                state['token'] = refresh_token
        return base + url_path + ('' if 'redirect' in url_path else '?t=' + state['token'])

    sink = engine.FileSink(part, resume=resume)
    start_offset = sink.offset
    d = engine.Downloader(provider, sink, size=size, connections=connections,
                          max_buffer=max_buffer, stall_timeout=30, log=lambda m: None)
    peak = [0]

    def watch():
        while d.state in ('idle', 'starting', 'running'):
            st = d.stats()
            with d.cond:
                peak[0] = max(peak[0], sum(s.recv - s.start for s in d.segments))
            if stop_after and st['written'] >= stop_after:
                d.stop()
                return
            time.sleep(0.05)

    t = threading.Thread(target=watch)
    t0 = time.time()
    t.start()
    try:
        result = d.run()
    except engine.DownloadError as exc:
        result = 'error: %s' % exc
    t.join()
    dt = time.time() - t0
    fetched = d.received
    ok = None
    if result == 'done':
        ok = os.path.getsize(part) == size and sha(part) == expect_sha
    print('%-34s %-8s %6.1fs %7.1f MB/s  resumed@%-6.0fMB net=%6.0fMB peakRAM=%4.0fMB refreshes=%d %s' % (
        label, result if len(result) < 9 else 'error', dt, fetched / dt / MB, start_offset / MB,
        fetched / MB, peak[0] / MB, state['refreshes'],
        '' if ok is None else ('SHA OK' if ok else 'SHA MISMATCH')))
    if result.startswith('error'):
        print('   ', result)
    return result, ok


def main():
    workdir = sys.argv[1]
    os.makedirs(workdir, exist_ok=True)
    data_path = os.path.join(workdir, 'source.bin')
    size = 200 * MB + 12345          # deliberately not a multiple of anything
    if not os.path.exists(data_path) or os.path.getsize(data_path) != size:
        with open(data_path, 'wb') as f:
            left = size
            while left:
                n = min(8 * MB, left)
                f.write(os.urandom(n))
                left -= n
    expect = sha(data_path)
    Handler.data_path = data_path
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    srv.handle_error = lambda *a: None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % srv.server_address[1]
    results = []

    B.per_conn_rate = 6 * MB
    results.append(run('1 connection (6 MB/s per conn)', workdir, base, size, expect, connections=1))
    results.append(run('4 connections', workdir, base, size, expect, connections=4))
    results.append(run('8 connections', workdir, base, size, expect, connections=8))
    results.append(run('16 connections', workdir, base, size, expect, connections=16, max_buffer=128 * MB))

    B.drop_chance = 0.3
    results.append(run('8 conns, 30% of responses cut', workdir, base, size, expect))
    B.drop_chance = 0.0

    B.max_conns = 3
    results.append(run('8 conns, server allows 3 (503)', workdir, base, size, expect))
    B.over_limit_code = 403
    results.append(run('8 conns, server allows 3 (403)', workdir, base, size, expect))
    B.over_limit_code = 503
    B.max_conns = 99

    results.append(run('stop at ~80 MB', workdir, base, size, expect, stop_after=80 * MB))
    results.append(run('  ...resume to the end', workdir, base, size, expect, resume=True))

    results.append(run('redirect, then link expires', workdir, base, size, expect,
                       url_path='/redirect', stop_after=50 * MB))
    B.valid_token = 'b'   # the old link now gets 403; the provider hands out the new token
    results.append(run('  ...resume with expired link', workdir, base, size, expect,
                       resume=True, refresh_token='b'))
    B.valid_token = 'a'

    B.ranges = False
    results.append(run('server without range support', workdir, base, size, expect))
    B.ranges = True

    # Slow head: one connection crawls while others race ahead -> RAM cap must hold.
    B.per_conn_rate = 20 * MB
    results.append(run('tight 24 MB RAM cap, 8 conns', workdir, base, size, expect, max_buffer=24 * MB))

    bad = [r for r in results if r[0] != 'done' and r[0] != 'stopped' or r[1] is False]
    print('\nFAILURES: %d' % len(bad))
    srv.shutdown()
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
