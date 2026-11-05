# -*- coding: utf-8 -*-
"""Headless integration test: Manager + LocalServer + client + plugin helpers against the mock API.

python -I test_service.py WORKDIR VIDEO_FILE
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, '..', '..', 'plugin.video.premiumize.downloader')
WORK = os.path.abspath(sys.argv[1])
VIDEO = os.path.abspath(sys.argv[2])
MOCK_PORT = 52991

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, 'profile'))
os.environ['PMDL_TEST_PROFILE'] = os.path.join(WORK, 'profile')
os.environ['PMDL_API_BASE'] = 'http://127.0.0.1:%d/api/' % MOCK_PORT
sys.path[:0] = [os.path.join(HERE, 'kodistub'), os.path.join(ADDON, 'resources', 'lib'), ADDON]

import xbmcaddon  # noqa: E402  (stub)
xbmcaddon.SETTINGS.update(api_key='testkey', download_path=os.path.join(WORK, 'dl'), connections=8,
                          buffer_mb=192, subtitles='true', max_concurrent=1, resume_on_start='true',
                          auto_retry='true', progress_bg='true', server_port=52771)
sys.argv = ['plugin://plugin.video.premiumize.downloader/', '-1', '']

from pmdl import client, common  # noqa: E402
from pmdl.manager import Manager  # noqa: E402
from pmdl.server import LocalServer  # noqa: E402
from pmdl.api import Premiumize  # noqa: E402
import default  # noqa: E402

FAILS = []


def check(cond, what):
    print('  %s %s' % ('PASS' if cond else 'FAIL', what))
    if not cond:
        FAILS.append(what)


def source_bytes(start, n):
    with open(VIDEO, 'rb') as f:
        f.seek(start)
        return f.read(n)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(8 << 20), b''):
            h.update(b)
    return h.hexdigest()


def get_range(url, start, end):
    req = urllib.request.Request(url, headers={'Range': 'bytes=%d-%d' % (start, end)})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        return r.status, r.headers.get('Content-Range'), data, time.time() - t0


def wait(pred, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        mgr.tick()
        if pred():
            return True
        time.sleep(step)
    return False


def job(job_id):
    return client.jobs_by_id(wait=1).get(job_id)


def mock_rate(rate):
    urllib.request.urlopen('http://127.0.0.1:%d/ctl?rate=%s' % (MOCK_PORT, rate)).read()


mock = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'mock_premiumize.py'), VIDEO, str(MOCK_PORT), '2'])
time.sleep(1.0)
mgr = srv = None
try:
    mgr = Manager()
    srv = LocalServer(mgr, 'tok', 52771)
    common.HOME.setProperty(common.PROP_PORT, str(srv.port))
    common.HOME.setProperty(common.PROP_TOKEN, 'tok')
    expect = sha(VIDEO)
    size = os.path.getsize(VIDEO)
    prem = Premiumize('testkey')

    print('\n1. Plugin builds the queue item (subtitle matching, folder mirroring)')
    default.ARGS.clear()
    default.ARGS.update(id='v1', fid='ftm', path='Movies/Test.Movie.2026.720p', name='Test.Movie.2026.720p.avi',
                        size=str(size))
    item = default.single_item(prem)
    targets = sorted(s['target'] for s in item['subs'])
    check(item['rel'] == ['Movies', 'Test.Movie.2026.720p'], 'mirrors cloud folders: %s' % item['rel'])
    check(targets == ['Test.Movie.2026.720p.2_Arabic.srt', 'Test.Movie.2026.720p.English.srt'],
          'subtitles renamed for Kodi: %s' % targets)
    default.ARGS.update(id='e1', fid='fss', path='Shows/Show S01', name='Show.S01E01.avi')
    ep = default.single_item(prem)
    check([s['target'] for s in ep['subs']] == ['Show.S01E01.en.srt'], 'episode gets only its own subtitle')

    print('\n2. Play now: stream while downloading at 8 x 2 MB/s')
    t0 = time.time()
    client.cmd('enqueue', items=[item], root=os.path.join(WORK, 'dl'), start_id='v1')
    url = client.stream_url('v1', item['name'])
    status, crange, data, dt = get_range(url, 0, 32 * 1024 * 1024 - 1)
    check(status == 206 and data == source_bytes(0, 32 << 20),
          'first 32 MB correct (HTTP %d, %.1fs after the click)' % (status, time.time() - t0))
    status, crange, data, dt = get_range(url, size - 4 * 1024 * 1024, size - 1)
    check(status == 206 and data == source_bytes(size - (4 << 20), 4 << 20),
          'seek to the very end (index) served before it is downloaded (%.2fs)' % dt)
    mid = size // 2
    status, crange, data, dt = get_range(url, mid, mid + 16 * 1024 * 1024 - 1)
    j = job('v1')
    check(data == source_bytes(mid, 16 << 20), 'seek to the middle served from premiumize while downloading '
                                               '(%.2fs, download at %d%%)' % (dt, j['have'] * 100 // size))
    j = job('v1')
    check(j['status'] == 'downloading' and (j.get('conns') or 0) >= 6,
          'download running with %s connections at %s' % (j.get('conns'), common.human_speed(j.get('speed'))))
    req = urllib.request.Request(url, method='HEAD')
    with urllib.request.urlopen(req) as r:
        check(r.headers['Content-Length'] == str(size) and r.headers['Accept-Ranges'] == 'bytes',
              'HEAD gives size and range support')

    print('\n3. Finish, rename, subtitles on disk')
    check(wait(lambda: (job('v1') or {}).get('status') == 'done', 180), 'v1 finished in %.0fs' % (time.time() - t0))
    j = job('v1')
    check(os.path.exists(j['dest']) and not os.path.exists(j['part']), 'renamed .part to %s' % os.path.basename(j['dest']))
    check(sha(j['dest']) == expect, 'downloaded file is byte-identical')
    subs = sorted(os.listdir(os.path.dirname(j['dest'])))
    check(subs == ['Test.Movie.2026.720p.2_Arabic.srt', 'Test.Movie.2026.720p.English.srt', 'Test.Movie.2026.720p.avi'],
          'folder content: %s' % subs)
    status, crange, data, dt = get_range(url, 1000, 2000)
    check(data == source_bytes(1000, 1001), 'stream URL still works after completion (served from the final file)')

    print('\n4. Pause / resume')
    mock_rate(4)
    client.cmd('enqueue', items=[ep], root=os.path.join(WORK, 'dl'))
    wait(lambda: ((job('e1') or {}).get('have') or 0) > 100 * 1024 * 1024, 60)
    client.cmd('pause', id='e1')
    check(wait(lambda: job('e1')['status'] == 'paused' and not mgr.runners.get('e1'), 15), 'paused')
    part = job('e1')['part']
    s1 = os.path.getsize(part)
    time.sleep(2)
    check(os.path.getsize(part) == s1 and s1 > 0, 'nothing written while paused (%d MB on disk)' % (s1 >> 20))
    client.cmd('resume', id='e1')
    check(wait(lambda: job('e1')['status'] == 'done', 120), 'resumed and finished')
    check(sha(job('e1')['dest']) == expect, 'resumed file is byte-identical')

    print('\n5. Restart in the middle (Kodi closed), resume on next start')
    default.ARGS.update(id='e2', fid='fss', path='Shows/Show S01', name='Show.S01E02.avi')
    ep2 = default.single_item(prem)
    client.cmd('enqueue', items=[ep2], root=os.path.join(WORK, 'dl'))
    wait(lambda: ((job('e2') or {}).get('have') or 0) > 150 * 1024 * 1024, 60)
    t0 = time.time()
    mgr.shutdown()
    srv.stop()
    check(time.time() - t0 < 6, 'service shut down in %.1fs (Kodi allows ~5s)' % (time.time() - t0))
    on_disk = os.path.getsize(os.path.join(WORK, 'dl', 'Shows', 'Show S01', 'Show.S01E02.avi.part'))
    mgr = Manager()
    srv = LocalServer(mgr, 'tok', 52771)
    common.HOME.setProperty(common.PROP_PORT, str(srv.port))
    check(mgr.jobs['e2']['status'] == 'queued' and mgr.jobs['e2']['written'] == on_disk,
          'job reloaded as queued with %d MB done' % (on_disk >> 20))
    check(wait(lambda: mgr.jobs['e2']['status'] == 'done', 120), 'resumed after restart and finished')
    check(sha(mgr.jobs['e2']['dest']) == expect, 'file is byte-identical after restart')

    print('\n6. Cancel deletes the partial file; delete_local removes the download')
    mgr.jobs.pop('e2')
    mgr.delete_local('e1')
    check(not os.path.exists(os.path.join(WORK, 'dl', 'Shows', 'Show S01', 'Show.S01E01.avi')), 'e1 deleted')
    client.cmd('enqueue', items=[ep], root=os.path.join(WORK, 'dl'))
    wait(lambda: ((job('e1') or {}).get('have') or 0) > 30 * 1024 * 1024, 60)
    client.cmd('cancel', id='e1')
    check(wait(lambda: job('e1') is None, 15), 'job gone after cancel')
    check(not os.path.exists(os.path.join(WORK, 'dl', 'Shows', 'Show S01', 'Show.S01E01.avi.part')), 'partial file deleted')

    print('\n7. Errors are explained')
    bad = dict(ep, id='nope', name='Missing.avi')
    client.cmd('enqueue', items=[bad], root=os.path.join(WORK, 'dl'))
    wait(lambda: (job('nope') or {}).get('status') == 'failed', 30)
    check('no longer exists' in (job('nope') or {}).get('error', ''), 'missing file: "%s"' % (job('nope') or {}).get('error'))
    client.cmd('cancel', id='nope')
finally:
    if mgr:
        mgr.shutdown()
    if srv:
        srv.stop()
    mock.terminate()

print('\n%d FAILED: %s' % (len(FAILS), FAILS) if FAILS else '\nALL PASSED')
sys.exit(1 if FAILS else 0)
