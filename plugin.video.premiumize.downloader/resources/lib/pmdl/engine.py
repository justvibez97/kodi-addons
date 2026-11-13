# -*- coding: utf-8 -*-
"""Multi-connection, strictly ordered HTTP(S) downloader.

No Kodi imports, so it can be tested on any PC.

How it works
------------
* The file is pulled over N persistent connections, each fetching its own
  byte range ("segment").
* Bytes are written to disk strictly in order. The file on disk is
  therefore always a valid prefix of the finished file, which gives:
    - reliable resume (file size == bytes done),
    - no pre-allocation or sparse files (Android FUSE, exFAT and NTFS
      drives handle those badly),
    - a file that can be played while it is still downloading.
* Segments that finish ahead of the write position wait in RAM, capped
  at ``max_buffer``. The segment at the write position is flushed as its
  bytes arrive.
* A connection with nothing left to fetch takes over the second half of
  the largest remaining segment, so every connection stays busy until
  the last byte and the end of the download doesn't slow to a trickle.
"""
import errno
import http.client
import os
import socket
import ssl
import threading
import time
import urllib.parse

MB = 1024 * 1024
BLOCK = 256 * 1024            # network read size
USER_AGENT = 'Kodi-DebridDownloader/1.1'
RESUME_SAFETY = 1 * MB        # re-fetch the tail of a partial file on resume
DRAIN_LIMIT = 512 * 1024      # read and discard up to this much to keep a connection alive

_SSL_CTX = None


def _ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _backoff_delay(failures):
    return min(30.0, 0.5 * (2 ** min(max(failures, 1), 6)))


class DownloadError(Exception):
    """A download problem. ``permanent`` errors are not retried."""

    def __init__(self, message, permanent=False, code=None):
        Exception.__init__(self, message)
        self.permanent = permanent
        self.code = code


class LinkExpired(DownloadError):
    pass


class Throttled(DownloadError):
    pass


class Stopped(Exception):
    pass


def disk_error_message(exc, path=''):
    """Turn an OSError raised while writing into a message a person can act on."""
    code = getattr(exc, 'errno', None)
    if code == errno.ENOSPC:
        return 'The drive is full.'
    if code == errno.EFBIG:
        return ('This drive can\'t hold files over 4 GB (it is probably FAT32). '
                'Reformat it as exFAT or NTFS, or pick another folder.')
    if code in (errno.EACCES, errno.EPERM, errno.EROFS):
        return ('Kodi isn\'t allowed to write to %s. On the Shield: Settings > Apps > Kodi > '
                'Permissions > Files and media > Allow management of all files.' % (path or 'this folder'))
    return 'Disk error: %s' % exc


# --------------------------------------------------------------------------- HTTP


def _split_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        raise DownloadError('Unsupported link: %s' % parts.scheme, permanent=True)
    path = parts.path or '/'
    # http.client only sends ASCII; quote anything that is not already quoted.
    path = urllib.parse.quote(path, safe="/%:@&=+$,;~!*'()")
    if parts.query:
        path += '?' + urllib.parse.quote(parts.query, safe="/%:@&=+$,;~!*'()?")
    return parts.scheme, parts.netloc, path


class Connection(object):
    """One keep-alive HTTP(S) connection that can be reused for many ranges."""

    def __init__(self, timeout=30):
        self.timeout = timeout
        self.conn = None
        self.key = None
        self.resp = None
        self.gen = 0            # bumped by the engine to make every connection start fresh

    def close(self):
        conn, self.conn, self.resp = self.conn, None, None
        if conn is not None:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _connect(self, scheme, netloc):
        if scheme == 'https':
            return http.client.HTTPSConnection(netloc, timeout=self.timeout, context=_ssl_context())
        return http.client.HTTPConnection(netloc, timeout=self.timeout)

    def get(self, url, start=None, end=None):
        """Send a GET (optionally ranged; ``end`` inclusive) and return the response."""
        scheme, netloc, path = _split_url(url)
        headers = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'}
        if start is not None:
            headers['Range'] = 'bytes=%d-%s' % (start, '' if end is None else end)
        for attempt in (1, 2):
            conn = self.conn
            if conn is None or self.key != (scheme, netloc):
                self.close()
                conn = self.conn = self._connect(scheme, netloc)
                self.key = (scheme, netloc)
                fresh = True
            else:
                fresh = False
            try:
                # Local reference: stop() may close self.conn from another thread.
                conn.request('GET', path, headers=headers)
                self.resp = conn.getresponse()
                return self.resp
            except (http.client.HTTPException, OSError, AttributeError) as exc:
                self.close()
                # A reused keep-alive connection may have been closed by the
                # server while idle: retry once on a fresh one.
                if fresh or attempt == 2:
                    raise DownloadError('Connection failed: %s' % exc)
        raise DownloadError('Connection failed')

    def finish_response(self):
        """Make the connection reusable, or close it if too much is unread."""
        resp = self.resp
        self.resp = None
        if resp is None:
            return
        try:
            left = resp.length
            if left is None or left > DRAIN_LIMIT or resp.will_close:
                self.close()
                return
            while left > 0:
                data = resp.read(min(left, BLOCK))
                if not data:
                    break
                left -= len(data)
        except (http.client.HTTPException, OSError):
            self.close()


def check_status(resp):
    status = resp.status
    if status in (200, 206):
        return
    if status in (401, 403, 404, 410):
        raise LinkExpired('Link rejected (HTTP %d)' % status, code=status)
    if status in (429, 503):
        raise Throttled('Server is busy (HTTP %d)' % status, code=status)
    if status == 416:
        raise DownloadError('Server rejected the byte range (HTTP 416)', code=status)
    raise DownloadError('HTTP %d %s' % (status, resp.reason), code=status)


def probe(url, timeout=30, max_redirects=6):
    """Follow redirects and find the file size and whether ranges work.

    Returns ``(final_url, size or None, ranges_supported)``.
    """
    conn = Connection(timeout)
    try:
        for _ in range(max_redirects):
            resp = conn.get(url, 0, 0)
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader('Location')
                conn.close()
                if not location:
                    raise DownloadError('Redirect without a location')
                url = urllib.parse.urljoin(url, location)
                continue
            check_status(resp)
            if resp.status == 206:
                crange = resp.getheader('Content-Range') or ''
                size = None
                if '/' in crange:
                    total = crange.rsplit('/', 1)[1].strip()
                    if total.isdigit():
                        size = int(total)
                return url, size, True
            length = resp.getheader('Content-Length')
            return url, int(length) if length and length.isdigit() else None, False
        raise DownloadError('Too many redirects')
    finally:
        conn.close()


# --------------------------------------------------------------------------- sinks


class FileSink(object):
    """Append-only file writer used for ``<name>.part`` files.

    Unbuffered on purpose: once :meth:`write` returns, the bytes are in the
    OS and can be read back through another handle (play while downloading).
    """

    def __init__(self, path, resume=True):
        self.path = path
        offset = 0
        if resume and os.path.exists(path):
            offset = max(0, os.path.getsize(path) - RESUME_SAFETY)
        mode = 'r+b' if offset and os.path.exists(path) else 'wb'
        self.f = open(path, mode, buffering=0)
        if offset:
            self.f.truncate(offset)
            self.f.seek(offset)
        self.offset = offset

    def reset(self):
        self.f.seek(0)
        self.f.truncate(0)
        self.offset = 0

    def write(self, data):
        view = memoryview(data)
        while view:
            n = self.f.write(view)
            if not n:
                raise OSError(errno.EIO, 'write returned 0')
            view = view[n:]
        self.offset += len(data)

    def close(self):
        try:
            try:
                os.fsync(self.f.fileno())
            except OSError:
                pass
        finally:
            self.f.close()


class NullSink(object):
    """Discards data (speed tests)."""

    def __init__(self):
        self.offset = 0

    def reset(self):
        self.offset = 0

    def write(self, data):
        self.offset += len(data)

    def close(self):
        pass


# --------------------------------------------------------------------------- engine


STEAL_AFTER = 6.0      # a piece with no data for this long is taken over by an idle connection
SLOW_FACTOR = 8.0      # ...and so is the head piece when it runs this many times slower than the rest


class Segment(object):
    __slots__ = ('start', 'recv', 'end', 'chunks', 'worker', 'last', 'since', 'recv_since')

    def __init__(self, start, end):
        self.start = start      # first byte not yet written to disk
        self.recv = start       # next byte expected from the network
        self.end = end          # exclusive; shrinks when another connection takes the tail
        self.chunks = []
        self.assigned(None)

    def assigned(self, wid):
        self.worker = wid
        self.last = self.since = time.time()
        self.recv_since = self.recv

    @property
    def remaining(self):
        return self.end - self.recv


class Downloader(object):
    """Download ``url_provider()`` into ``sink``.

    ``url_provider(refresh)`` returns the link; with ``refresh=True`` it must
    fetch a fresh one (links can expire). ``size`` is the expected size, if
    known. Call :meth:`run` in a thread of your own, :meth:`stop` from any
    thread, and :meth:`stats` for progress.
    """

    def __init__(self, url_provider, sink, size=None, connections=8,
                 max_buffer=192 * MB, stall_timeout=180, log=None, write_stall_timeout=90,
                 max_rate=0, service='premiumize'):
        self.url_provider = url_provider
        self.service = service      # who serves the file, for status notes
        self.sink = sink
        self.size = size
        self.connections = max(1, int(connections))
        self.max_buffer = max(16 * MB, int(max_buffer))
        self.stall_timeout = stall_timeout
        self.write_stall_timeout = write_stall_timeout
        self.max_rate = max(0, int(max_rate or 0))     # bytes/s for the whole download; 0 = no limit
        self.log = log or (lambda msg: None)

        self.cond = threading.Condition(threading.Lock())
        self.segments = []
        self.frontier = 0
        self.written = 0
        self.received = 0           # bytes off the network this session
        self.chunk = 8 * MB
        self.min_split = 2 * MB
        self.url = None
        self.ranges = True
        self.state = 'idle'         # idle, starting, running, done, stopped, failed
        self.error = None
        self.active = 0             # connections currently transferring
        self.retrying = 0           # connections waiting to retry after an error
        self.retired = 0            # connections dropped after the server pushed back
        self.started_at = None
        self.start_offset = 0

        self._stop = threading.Event()
        self._conns = set()
        self._conns_lock = threading.Lock()
        self._url_lock = threading.Lock()
        self._samples = []
        self._speed = 0.0
        self._write_speed = 0.0
        self._last_progress = time.time()
        self._writing_since = None
        self._writer_thread = None
        self._note = None           # (text, time): the latest thing worth telling the viewer
        self._rate_lock = threading.Lock()
        self._rate_next = 0.0
        self.takeovers = 0
        self.reconnects = 0         # times the whole download switched to a fresh link
        self._conn_gen = 0
        self._best_speed = 0.0
        self._slow_since = None
        self._last_reconnect = 0.0
        self._last_status_log = 0.0

    # ------------------------------------------------------------ public

    def stop(self):
        self._stop.set()
        with self.cond:
            self.cond.notify_all()
        self._close_all()

    def _close_all(self):
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            conn.close()

    def _track(self, conn, add=True):
        with self._conns_lock:
            if add:
                self._conns.add(conn)
            else:
                self._conns.discard(conn)

    @property
    def stopping(self):
        return self._stop.is_set()

    def stats(self):
        with self.cond:
            buffered = sum(s.recv - s.start for s in self.segments)
            have = self.written + buffered
            size = self.size
            speed = self._speed
            live = max(0, self.connections - self.retired) if self.ranges else 1
            retrying = self.retrying
        eta = None
        if size and speed > 0:
            eta = max(0, (size - have) / speed)
        return {
            'state': self.state, 'size': size, 'written': self.written, 'have': have,
            'speed': speed, 'eta': eta, 'connections': live, 'retrying': retrying, 'error': self.error,
            'note': self.note(buffered), 'write_speed': self._write_speed,
            'buffered': buffered, 'reconnects': self.reconnects,
        }

    def _set_note(self, text):
        self._note = (text, time.time())
        self.log(text)

    def note(self, buffered=0):
        """A short reason when the download is slow or waiting, else None."""
        if self._writing_since and buffered >= self.max_buffer * 0.75:
            return 'Waiting for the drive (writing at %.0f MB/s)' % (self._write_speed / MB)
        if self._note and time.time() - self._note[1] < 20:
            return self._note[0]
        return None

    def run(self):
        """Download to completion. Returns 'done' or 'stopped'; raises DownloadError."""
        self.state = 'starting'
        self.started_at = time.time()
        try:
            self._prepare()
            if self.written >= self.size:
                self.state = 'done'
                return 'done'
            self.state = 'running'
            if self.ranges:
                self._run_parallel()
            else:
                self._run_single()
            if self.error is not None:
                raise self.error
            if self._stop.is_set() and self.written < self.size:
                self.state = 'stopped'
                return 'stopped'
            self.state = 'done'
            return 'done'
        except Stopped:
            self.state = 'stopped'
            return 'stopped'
        except DownloadError as exc:
            self.state = 'failed'
            self.error = exc
            raise
        finally:
            try:
                writer = self._writer_thread
                if writer is None or not writer.is_alive():   # never touch a file mid-hung-write
                    self.sink.close()
            except OSError as exc:
                if self.state == 'done':
                    self.state = 'failed'
                    raise DownloadError(disk_error_message(exc), permanent=True)

    # ------------------------------------------------------------ setup

    def _prepare(self):
        url = self._fresh_url(refresh=False)
        attempt = 0
        while True:
            attempt += 1
            try:
                final, size, ranges = probe(url)
                break
            except LinkExpired:
                if attempt > 2:
                    raise
                url = self._fresh_url(refresh=True, failed_url=url)
            except DownloadError as exc:
                if exc.permanent or attempt >= 5:
                    raise
                if self._stop.wait(_backoff_delay(attempt)):
                    raise Stopped()
        if self._stop.is_set():
            raise Stopped()
        self.url = final
        if size is None:
            size = self.size
        if size is None:
            raise DownloadError('The server did not report the file size', permanent=True)
        if self.size and size != self.size:
            self.log('size from server %d differs from expected %d; using server size' % (size, self.size))
        self.size = size
        self.ranges = ranges
        if not ranges:
            self.log('server ignores byte ranges: single connection, no resume')
            self.sink.reset()
        if self.sink.offset > size:
            self.sink.reset()
        self.written = self.frontier = self.start_offset = self.sink.offset
        self.chunk = self._clamp_chunk(8 * MB)

    def _fresh_url(self, refresh, failed_url=None):
        with self._url_lock:
            if refresh and failed_url is not None and self.url and self.url != failed_url:
                return self.url     # another connection already refreshed it
            url = self.url_provider(refresh)
            if refresh:
                try:
                    url = probe(url)[0]
                except DownloadError:
                    pass
            self.url = url
            return url

    def _clamp_chunk(self, value):
        cap = max(1 * MB, self.max_buffer // (2 * self.connections))
        value = min(max(int(value), 1 * MB), cap, 64 * MB)
        return (value // (64 * 1024)) * 64 * 1024 or 64 * 1024

    # ------------------------------------------------------------ parallel

    def _run_parallel(self):
        workers = [threading.Thread(target=self._worker, args=(i,), name='pmdl-conn-%d' % i)
                   for i in range(self.connections)]
        writer = threading.Thread(target=self._writer, name='pmdl-writer')
        writer.daemon = True
        self._writer_thread = writer
        writer.start()
        for t in workers:
            t.daemon = True
            t.start()
        try:
            self._monitor(writer)
        finally:
            self._stop_threads()
            for t in workers:
                t.join(10)
            # A writer stuck inside a drive write can't be interrupted; don't wait on it.
            writer.join(1 if self._writing_since else 30)

    def _stop_threads(self):
        with self.cond:
            self.cond.notify_all()
        if self.written < (self.size or 0) or self.error is not None:
            self._stop.set()
            self._close_all()

    def _monitor(self, writer):
        last_sample = 0
        while writer.is_alive():
            with self.cond:
                self.cond.wait(0.25)
            now = time.time()
            if now - last_sample >= 0.5:
                last_sample = now
                self._sample(now)
                self._check_degraded(now)
            if self.error is not None:
                return
            if self._stop.is_set():
                return
            if now - self._last_progress > self.stall_timeout:
                self._fail(DownloadError('No data received for %d seconds' % self.stall_timeout))
                return
            writing = self._writing_since
            if writing and now - writing > self.write_stall_timeout:
                self._fail(DownloadError(
                    'Writing to the drive has stopped responding (one write has taken over %d seconds). '
                    'Check the drive, or try another USB port.' % self.write_stall_timeout))
                return

    def _sample(self, now):
        with self.cond:
            received = self.received
            written = self.written
        self._samples.append((now, received, written))
        cutoff = now - 4.0
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.pop(0)
        t0, r0, w0 = self._samples[0]
        if now - t0 > 0.4:
            inst = (received - r0) / (now - t0)
            self._speed = inst if self._speed == 0 else self._speed * 0.6 + inst * 0.4
            winst = (written - w0) / (now - t0)
            self._write_speed = winst if self._write_speed == 0 else self._write_speed * 0.6 + winst * 0.4
        # Size segments so each connection works ~2 s per request: fewer
        # round trips at high speed, finer splitting at low speed.
        live = max(1, self.connections - self.retired)
        per_conn = self._speed / live
        with self.cond:
            self.chunk = self._clamp_chunk(per_conn * 2.0 if per_conn > 0 else 8 * MB)
            self.min_split = max(2 * MB, min(16 * MB, int(per_conn)))

    def _check_degraded(self, now):
        """Start over on a fresh link when the speed collapses for no visible reason.

        Seen on the Shield: a download starts at full speed, then after some
        minutes its connections deliver a fraction of that with no errors,
        while fresh connections (a new download, the speed test, another
        device) get full speed from the same file at the same time. So when
        the speed stays under half of this download's best for 45 s, and it
        isn't the drive, a speed limit or an error we already know about,
        fetch a new link (the service may hand out another server) and let
        every connection reconnect.
        """
        if self.started_at and now - self.started_at > 20:
            self._best_speed = max(self._best_speed, self._speed)
        if now - self._last_status_log >= 60:
            self._last_status_log = now
            with self.cond:
                buffered = sum(x.recv - x.start for x in self.segments)
            self.log('status: net %.1f MB/s (best %.1f), disk %.1f MB/s, %d MB waiting, %d/%d connections '
                     'active, %d retrying, %d taken over, %d reconnects' % (
                         self._speed / MB, self._best_speed / MB, self._write_speed / MB, buffered // MB,
                         self.active, self.connections - self.retired, self.retrying, self.takeovers,
                         self.reconnects))
        if self.max_rate or not self._best_speed or self._speed >= self._best_speed * 0.5:
            self._slow_since = None
            return
        with self.cond:
            buffered = sum(x.recv - x.start for x in self.segments)
        if buffered >= self.max_buffer * 0.5 or self.retrying:
            self._slow_since = None         # the drive or failing connections explain it
            return
        if self._slow_since is None:
            self._slow_since = now
            return
        if now - self._slow_since < 45 or now - self._last_reconnect < 120:
            return
        self._slow_since = None
        self._last_reconnect = now
        slow, best = self._speed, self._best_speed
        t = threading.Thread(target=self._reconnect_all, args=(slow, best), name='pmdl-reconnect')
        t.daemon = True
        t.start()

    def _reconnect_all(self, slow, best):
        old = self.url
        try:
            new = self._fresh_url(refresh=True, failed_url=old)
        except Exception as exc:  # keep going on the old link
            self.log('could not refresh the link: %s' % exc)
            new = old
        with self.cond:
            self._conn_gen += 1
            self.reconnects += 1
        old_host = urllib.parse.urlsplit(old or '').netloc
        new_host = urllib.parse.urlsplit(new or '').netloc
        self._set_note('Speed dropped to %.0f MB/s: reconnecting' % (slow / MB))
        self.log('speed fell to %.1f MB/s (best %.1f) for 45 s: fresh link (%s), all connections reconnect' % (
            slow / MB, best / MB, 'new server %s' % new_host if new_host != old_host else 'same server'))

    def _fail(self, exc):
        with self.cond:
            if self.error is None:
                self.error = exc
            self.cond.notify_all()
        self._stop.set()
        self._close_all()

    def _committed(self):
        # Worst-case RAM: every segment except the head (which is flushed as it arrives).
        return sum(s.end - s.start for s in self.segments[1:])

    def _assign(self, wid):
        """Pick the next piece of work for connection ``wid`` (lock held by caller)."""
        while True:
            if self._stop.is_set() or self.error is not None:
                return None
            if self.frontier >= self.size and not any(s.remaining > 0 for s in self.segments):
                return None
            # 1. A segment whose connection died.
            for seg in self.segments:
                if seg.worker is None and seg.remaining > 0:
                    seg.assigned(wid)
                    return seg
            # 2. Fresh range from the unassigned part of the file.
            if self.frontier < self.size:
                end = min(self.size, self.frontier + self.chunk)
                if not self.segments or self._committed() + (end - self.frontier) <= self.max_buffer:
                    seg = Segment(self.frontier, end)
                    seg.assigned(wid)
                    self.frontier = end
                    self.segments.append(seg)
                    return seg
            # 3. Take over half of a busy segment. If RAM is full, help the head
            #    so it can be flushed; near the end, take the biggest one.
            memory_full = self.frontier < self.size
            best = None
            if memory_full and self.segments and self.segments[0].remaining >= 2 * self.min_split:
                best = self.segments[0]
            else:
                for seg in self.segments:
                    if seg.remaining >= 2 * self.min_split and (best is None or seg.remaining > best.remaining):
                        best = seg
            if best is not None:
                mid = best.recv + best.remaining // 2
                mid -= mid % (64 * 1024) if mid - best.recv > 64 * 1024 else 0
                seg = Segment(mid, best.end)
                seg.assigned(wid)
                best.end = mid
                self.segments.insert(self.segments.index(best) + 1, seg)
                return seg
            # 4. Nothing new to fetch: take over a piece whose connection has
            #    stalled, or a head piece crawling far behind the others (one
            #    slow connection must not freeze the whole download).
            victim = self._stalled_segment()
            if victim is not None:
                old = victim.worker
                victim.assigned(wid)
                self.takeovers += 1
                self._set_note('A slow connection was replaced')
                self.log('connection %s took over %d-%d from connection %s' % (wid, victim.recv, victim.end, old))
                return victim
            self.cond.wait(0.5)

    def _stalled_segment(self):
        now = time.time()
        per_conn = self._speed / max(1, self.active)
        for i, seg in enumerate(self.segments):
            if seg.worker is None or seg.remaining <= 0:
                continue
            if now - seg.last > STEAL_AFTER:
                return seg
            if i == 0 and now - seg.since > 4.0 and per_conn > 0:
                rate = (seg.recv - seg.recv_since) / (now - seg.since)
                if rate * SLOW_FACTOR < per_conn:
                    return seg
        return None

    def _worker(self, wid):
        conn = Connection()
        self._track(conn)
        failures = 0
        try:
            while not self._stop.is_set():
                with self.cond:
                    seg = self._assign(wid)
                    if seg is None:
                        return
                    self.active += 1
                wait = 0
                try:
                    self._fetch(conn, seg, wid)
                    failures = 0
                except Stopped:
                    return
                except DownloadError as exc:
                    self._release(seg, wid)
                    conn.close()
                    failures += 1
                    wait = self._on_error(wid, exc, failures)
                    if wait is None:
                        return
                finally:
                    with self.cond:
                        self.active -= 1
                        self.cond.notify_all()
                if wait:
                    with self.cond:
                        self.retrying += 1
                    try:
                        if self._stop.wait(wait):
                            return
                    finally:
                        with self.cond:
                            self.retrying -= 1
        except Exception as exc:  # never let a connection thread die silently
            if not self._stop.is_set():
                self._fail(DownloadError('Connection thread crashed: %r' % exc, permanent=True))
        finally:
            conn.close()
            self._track(conn, False)

    def _on_error(self, wid, exc, failures):
        """Decide what a connection does after an error: seconds to wait, or None to quit."""
        if isinstance(exc, Throttled):
            with self.cond:
                if self.connections - self.retired - 1 >= 1:
                    self.retired += 1
                    self._set_note('%s limited connections (%s): using %d' % (
                        self.service, exc, self.connections - self.retired))
                    return None
            self._set_note('%s is busy (%s), retrying' % (self.service, exc))
            return _backoff_delay(failures)
        if isinstance(exc, LinkExpired):
            with self.cond:
                others_ok = (self.connections - self.retired - 1 >= 1
                             and time.time() - self._last_progress < 5)
                if failures >= 3 and others_ok:
                    # The link works for the other connections, so this is a
                    # per-user connection limit rather than an expired link.
                    self.retired += 1
                    self._set_note('%s limited connections (%s): using %d' % (
                        self.service, exc, self.connections - self.retired))
                    return None
            self._set_note('Refreshing the download link (%s)' % exc)
            if failures > 6:
                self._fail(DownloadError('The download link stopped working (%s)' % exc))
                return None
            try:
                self._fresh_url(refresh=True, failed_url=getattr(exc, 'url', None))
            except Exception as exc2:  # API errors become the job error
                self._fail(DownloadError('Could not refresh the link: %s' % exc2))
                return None
            return _backoff_delay(failures - 1) if failures > 1 else 0
        if exc.permanent:
            self._fail(exc)
            return None
        self._set_note('Connection problem (%s), retrying' % exc)
        return _backoff_delay(failures)

    def _throttle(self, n):
        """Hold the download to ``max_rate`` (all connections together)."""
        if not self.max_rate:
            return
        with self._rate_lock:
            now = time.time()
            self._rate_next = max(self._rate_next, now - 0.5) + n / float(self.max_rate)
            delay = self._rate_next - now
        if delay > 0 and self._stop.wait(delay):
            raise Stopped()

    def _release(self, seg, wid):
        with self.cond:
            if seg.worker == wid:
                seg.worker = None
            self.cond.notify_all()

    def _fetch(self, conn, seg, wid):
        with self.cond:
            start, end = seg.recv, seg.end
        if start >= end:
            with self.cond:
                seg.worker = None if seg.worker == wid else seg.worker
            return
        url = self.url
        if conn.gen != self._conn_gen:
            conn.close()
            conn.gen = self._conn_gen
        try:
            resp = conn.get(url, start, end - 1)
        except DownloadError:
            if self._stop.is_set():
                raise Stopped()
            raise
        try:
            check_status(resp)
        except LinkExpired as exc:
            exc.url = url
            raise
        if resp.status != 206:
            raise DownloadError('Server ignored the byte range', code=resp.status)
        crange = resp.getheader('Content-Range') or ''
        if not crange.startswith('bytes %d-' % start):
            raise DownloadError('Unexpected Content-Range: %s' % crange)
        clean = False
        try:
            while True:
                with self.cond:
                    if self._stop.is_set():
                        raise Stopped()
                    if seg.worker != wid:
                        return
                    want = min(BLOCK, seg.end - seg.recv)
                if want <= 0:
                    clean = True
                    break
                try:
                    data = resp.read(want)
                except (http.client.HTTPException, OSError) as exc:
                    if self._stop.is_set():
                        raise Stopped()
                    raise DownloadError('Read failed: %s' % exc)
                if not data:
                    raise DownloadError('Connection closed early')
                with self.cond:
                    if seg.worker != wid:
                        return          # another connection took this piece over; drop ours
                    n = min(len(data), seg.end - seg.recv)
                    if n <= 0:
                        clean = True
                        break
                    seg.chunks.append(data if n == len(data) else data[:n])
                    seg.recv += n
                    self.received += n
                    self._last_progress = seg.last = time.time()
                    if seg.recv >= seg.end:
                        seg.worker = None
                        clean = True
                    self.cond.notify_all()
                self._throttle(n)
                if clean:
                    break
        finally:
            if clean:
                conn.finish_response()
            else:
                conn.close()

    def _writer(self):
        try:
            while True:
                with self.cond:
                    while True:
                        head = self.segments[0] if self.segments else None
                        if head is not None and head.chunks:
                            chunks, head.chunks = head.chunks, []
                            break
                        if head is not None and head.start >= head.end:
                            self.segments.pop(0)
                            continue
                        if self.written >= self.size:
                            return
                        if self.error is not None or self._stop.is_set():
                            # Whatever the head had is flushed; the rest is re-fetched on resume.
                            return
                        self.cond.wait(0.5)
                n = 0
                try:
                    for data in chunks:
                        self._writing_since = time.time()
                        self.sink.write(data)
                        self._writing_since = None
                        n += len(data)
                except OSError as exc:
                    self._writing_since = None
                    self._fail(DownloadError(disk_error_message(exc, getattr(self.sink, 'path', '')), permanent=True))
                    return
                with self.cond:
                    self.written += n
                    head.start += n
                    if head.start >= head.end and self.segments and self.segments[0] is head:
                        self.segments.pop(0)
                    self.cond.notify_all()
        except Exception as exc:  # never die silently
            self._fail(DownloadError('Writer crashed: %s' % exc, permanent=True))

    # ------------------------------------------------------------ single connection

    def _run_single(self):
        conn = Connection()
        self._track(conn)
        try:
            resp = conn.get(self.url)
            check_status(resp)
            last = time.time()
            while self.written < self.size:
                if self._stop.is_set():
                    raise Stopped()
                try:
                    data = resp.read(BLOCK)
                except (http.client.HTTPException, OSError) as exc:
                    if self._stop.is_set():
                        raise Stopped()
                    raise DownloadError('Read failed: %s' % exc)
                if not data:
                    raise DownloadError('Connection closed early')
                try:
                    self.sink.write(data)
                except OSError as exc:
                    raise DownloadError(disk_error_message(exc, getattr(self.sink, 'path', '')), permanent=True)
                with self.cond:
                    self.written += len(data)
                    self.received += len(data)
                self._throttle(len(data))
                now = time.time()
                if now - last >= 0.5:
                    last = now
                    self._sample(now)
        finally:
            conn.close()
            self._track(conn, False)
