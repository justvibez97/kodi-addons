# -*- coding: utf-8 -*-
"""Real-Debrid end to end against the mock services (sign-in, menus, download, stream, delete).

python -I test_realdebrid.py WORKDIR VIDEO_FILE
"""
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, '..', '..', 'plugin.video.premiumize.downloader')
WORK, VIDEO = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
PM_PORT, RD_PORT, LOCAL_PORT = 52996, 52997, 52774

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, 'profile'))
os.environ['PMDL_TEST_PROFILE'] = os.path.join(WORK, 'profile')
os.environ['PMDL_API_BASE'] = 'http://127.0.0.1:%d/api/' % PM_PORT
os.environ['PMDL_RD_BASE'] = 'http://127.0.0.1:%d/rest/1.0/' % RD_PORT
os.environ['PMDL_RD_OAUTH'] = 'http://127.0.0.1:%d/oauth/v2/' % RD_PORT
sys.path[:0] = [os.path.join(HERE, 'kodistub'), os.path.join(ADDON, 'resources', 'lib'), ADDON]
sys.argv = ['plugin://plugin.video.premiumize.downloader/', '1', '']

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402
import xbmcplugin  # noqa: E402
xbmcaddon.SETTINGS.update(api_key='testkey', download_path=os.path.join(WORK, 'dl'), connections=8, rd_connections=10, buffer_mb=128,
                          subtitles='true', max_concurrent=1, resume_on_start='true', auto_retry='false',
                          server_port=LOCAL_PORT, click_action=0, show_all_files='false')
from pmdl import client, common, realdebrid, sources  # noqa: E402
from pmdl.api import ApiError  # noqa: E402
from pmdl.manager import Manager  # noqa: E402
from pmdl.server import LocalServer  # noqa: E402
import default  # noqa: E402

realdebrid.PAGE = 2         # exercise paging with five torrents
FAILS = []


def check(cond, what):
    print('  %s %s' % ('PASS' if cond else 'FAIL', what))
    if not cond:
        FAILS.append(what)


def ctl(query=''):
    import json
    with urllib.request.urlopen('http://127.0.0.1:%d/ctl?%s' % (RD_PORT, query)) as r:
        return json.loads(r.read().decode())


def listing(fn, **args):
    """Run a plugin listing and return [(label, url, is_folder, ListItem)]."""
    del xbmcplugin.ITEMS[:]
    default.ARGS.clear()
    default.ARGS.update(args)
    fn()
    return [(li.getLabel(), u, folder, li) for u, li, folder in xbmcplugin.ITEMS]


def labels(items):
    return [lab for lab, _u, _f, _li in items]


def action(fn, **args):
    default.ARGS.clear()
    default.ARGS.update(args)
    return fn()


def sha(path, n=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read() if n is None else f.read(n))
    return h.hexdigest()


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


def get_range(url, start, end):
    req = urllib.request.Request(url, headers={'Range': 'bytes=%d-%d' % (start, end)})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read()


def source_bytes(start, n):
    with open(VIDEO, 'rb') as f:
        f.seek(start)
        return f.read(n)


quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pm_mock = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'mock_premiumize.py'), VIDEO, str(PM_PORT), '3'], **quiet)
rd_mock = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'mock_realdebrid.py'), VIDEO, str(RD_PORT), '4'], **quiet)
time.sleep(1.0)
mgr = srv = None
size = os.path.getsize(VIDEO)
try:
    mgr = Manager()
    srv = LocalServer(mgr, 'tok', LOCAL_PORT)
    common.HOME.setProperty(common.PROP_PORT, str(srv.port))
    common.HOME.setProperty(common.PROP_TOKEN, 'tok')

    print('\n1. Before sign-in: main menu offers "Add Real-Debrid"')
    menu = labels(listing(default.root))
    check('premiumize.me cloud' in menu and 'Real-Debrid torrents' not in menu and 'Add Real-Debrid' in menu,
          'menu: %s' % menu)
    try:
        sources.rd()
        check(False, 'no client without sign-in')
    except ApiError as exc:
        check(exc.code == 'no_key', 'not signed in: "%s"' % exc)

    print('\n2. Sign in with a code (approved on the "phone" after ~2 s)')
    threading.Timer(2.0, lambda: ctl('approve=1')).start()
    t0 = time.time()
    action(default.action_rd_pair)
    s = xbmcaddon.SETTINGS
    check(s.get('rd_client_id') == 'CID123' and s.get('rd_client_secret') == 'SECRET456' and
          s.get('rd_refresh') == 'REFRESH000' and s.get('rd_token', '').startswith('tok'),
          'credentials saved (%.1fs, %d polls)' % (time.time() - t0, ctl()['polls']))
    check(float(s.get('rd_expires') or 0) > time.time() + 3000, 'token expiry saved')
    check(any('mockuser' in m for _h, m in xbmcgui.NOTES), 'says who signed in')
    menu = labels(listing(default.root))
    check('Real-Debrid torrents' in menu and 'Add Real-Debrid' not in menu, 'menu now: %s' % menu)

    print('\n3. Torrent list (newest first, paged 2 at a time)')
    items = listing(default.rd_list)
    names = labels(items)
    print('   ', names)
    check(len(items) == 5, '5 torrents over 3 pages')
    check(names[0].startswith('Still.Downloading.2026') and 'downloading to Real-Debrid 42%' in names[0],
          'unfinished torrent shows its state')
    check('action=rd_status' in items[0][1] and not items[0][2], 'and only explains itself when selected')
    single = items[1]
    check(single[0] == 'Single.Movie.2025.720p.avi' and not single[2] and 'action=choose' in single[1]
          and 'id=rd%3AT2%3A0' in single[1], 'single-file torrent plays directly (asks first): %s' % single[1])
    check([n for n, _u, f, _li in items[2:]] == ['Show.S01.1080p', 'Test.Movie.2026.1080p', 'Packed.Pack']
          and all(f for _n, _u, f, _li in items[2:]), 'multi-file torrents open as folders')
    ctx = [c[0] for c in single[3].context]
    check('Delete torrent from Real-Debrid' in ctx and 'Speed test with this file' in ctx, 'file menu: %s' % ctx)

    print('\n4. Inside torrents')
    t1 = listing(default.rd_torrent, id='T1')
    check(labels(t1) == ['Test.Movie.2026.1080p.avi'], 'T1 shows its video, subtitle hidden: %s' % labels(t1))
    realdebrid.PAGE = 100
    full = sources.rd().torrents()
    check(all(t['links'] for t in full if t['status'] == 'downloaded'),
          'pages of 100 keep the download links (Real-Debrid drops them above 100)')
    realdebrid.PAGE = 2
    t4 = sources.rd().files(sources.rd().torrent_info('T4'))
    check([e['name'] for e in t4] == ['Packed.Pack.rar'], 'packed torrent: names learnt from unrestrict')

    print('\n5. Queue items: folders, subtitles, link')
    it1 = action(default.item_for_args, id='rd:T1:0', fid='T1', path='Test.Movie.2026.1080p',
                 name='Test.Movie.2026.1080p.avi', size=str(size))
    check(it1['rel'] == ['Test.Movie.2026.1080p'] and it1['src_link'].endswith('/d/L1'), 'saved under the torrent name')
    check([x['target'] for x in it1['subs']] == ['Test.Movie.2026.1080p.English.srt'], 'subtitle renamed for Kodi')
    it2 = action(default.item_for_args, id='rd:T2:0', fid='T2', path='', name='Single.Movie.2025.720p.avi')
    check(it2['rel'] == [] and it2['size'] == size, 'single-file torrent saved without an extra folder')

    print('\n6. Play now: stream while downloading, then finish')
    ctl('reset_counts=1')
    client.cmd('enqueue', items=[it1], root=os.path.join(WORK, 'dl'), start_id='rd:T1:0')
    url = client.stream_url('rd:T1:0', it1['name'])
    status, data = get_range(url, 0, 16 * 1024 * 1024 - 1)
    check(status == 206 and data == source_bytes(0, 16 << 20), 'first 16 MB play from the stream URL')
    status, data = get_range(url, size - (2 << 20), size - 1)
    check(data == source_bytes(size - (2 << 20), 2 << 20), 'seek to the end served before it is downloaded')
    wait(lambda: (job('rd:T1:0') or {}).get('speed'), 20)
    j = job('rd:T1:0')
    check(j['status'] == 'downloading' and j.get('conns') == 10, 'Real-Debrid setting used: %s connections at %s' % (
        j.get('conns'), common.human_speed(j.get('speed'))))
    check(wait(lambda: (job('rd:T1:0') or {}).get('status') == 'done', 240), 'finished')
    requests = ctl()['requests']
    check(any('using long requests' in m for m in xbmc.LOG), 'noticed the server closes every connection')
    # Before 1.1.2 a piece was at most 8 MB and at most half the buffer spread over the connections.
    old_piece = min(8.0, 128.0 / (2 * 10))
    old_requests = (size >> 20) / old_piece
    check(requests < old_requests * 0.75, 'long requests: %d requests for %d MB (old sizing: ~%d)' % (
        requests, size >> 20, old_requests))
    j = job('rd:T1:0')
    check(sha(j['dest']) == sha(VIDEO), 'file is byte-identical')
    folder = os.path.dirname(j['dest'])
    check(sorted(os.listdir(folder)) == ['Test.Movie.2026.1080p.English.srt', 'Test.Movie.2026.1080p.avi'],
          'folder: %s' % sorted(os.listdir(folder)))
    listed = listing(default.rd_torrent, id='T1')
    check('(downloaded)' in listed[0][0] and listed[0][3].props.get('IsPlayable') == 'true',
          'list marks it downloaded and plays it directly')

    print('\n7. Real-Debrid connection limit is respected')
    ctl('chunks=3')
    it4 = action(default.item_for_args, id='rd:T3:0', fid='T3', path='Show.S01.1080p', name='Show.S01E01.avi')
    client.cmd('enqueue', items=[it4], root=os.path.join(WORK, 'dl'))
    wait(lambda: (job('rd:T3:0') or {}).get('speed'), 20)
    j = job('rd:T3:0')
    check(j['status'] == 'downloading' and j.get('conns') == 3, 'uses 3 connections (%s)' % j.get('conns'))
    check(any('Real-Debrid allows 3 connections' in m for m in xbmc.LOG), 'logged why')

    print('\n8. Token refresh')
    before = ctl()['refreshes']
    ctl('revoke=1')
    check(sources.rd().user()['username'] == 'mockuser', 'revoked token: refreshed and retried')
    xbmcaddon.SETTINGS['rd_expires'] = str(int(time.time()) - 10)
    sources.rd().torrents()
    check(ctl()['refreshes'] == before + 2, 'expired token: refreshed before the call (%d refreshes)' % (
        ctl()['refreshes'] - before))

    print('\n9. Search looks in both clouds')
    found = listing(default.search, q='show s01')
    check([lab for lab in labels(found) if 'Real-Debrid' in lab] == ['Show.S01.1080p  [COLOR grey](Real-Debrid)[/COLOR]'],
          'Real-Debrid torrent found: %s' % labels(found))
    check(any(lab.startswith('Show S01') for lab in labels(found)), 'premiumize folder found too')
    found = labels(listing(default.search, q='single movie'))
    check(any(lab.startswith('Single.Movie.2025.720p.avi') for lab in found), 'dots match spaces: %s' % found)

    print('\n10. Delete a torrent while one of its files downloads')
    check(job('rd:T3:0')['status'] == 'downloading', 'E01 still downloading')
    action(default.action_cloud_delete, kind='torrent', id='T3', name='Show.S01.1080p')
    wait(lambda: job('rd:T3:0') is None, 15)
    check(job('rd:T3:0') is None, 'its download was cancelled')
    check('T3' not in [t['id'] for t in sources.rd().torrents()], 'torrent gone from Real-Debrid')
    try:
        sources.rd().torrent_info('T3')
        check(False, 'deleted torrent is not found')
    except ApiError as exc:
        check(exc.code == 'not_found' and 'Real-Debrid' in str(exc), 'deleted torrent: "%s"' % exc)
    check(os.path.exists(job('rd:T1:0')['dest']), 'other downloads untouched')

    print('\n11. A file Real-Debrid no longer has fails with a clear message')
    bad = dict(it2, id='rd:T9:0', src_link='https://real-debrid.com/d/NOPE', subs=[])
    client.cmd('enqueue', items=[bad], root=os.path.join(WORK, 'dl'))
    wait(lambda: (job('rd:T9:0') or {}).get('status') == 'failed', 20)
    err = (job('rd:T9:0') or {}).get('error') or ''
    check('unavailable' in err and not job('rd:T9:0').get('retry_at'), 'failed for good: "%s"' % err)
    client.cmd('cancel', id='rd:T9:0')

    print('\n12. Speed test and stream-only use a fresh Real-Debrid link')
    link, chunks = sources.resolve('rd:T2:0')
    check(link.startswith('http://127.0.0.1:%d/dl/L3/' % RD_PORT) and chunks == 3, 'resolve: %s (%d)' % (link, chunks))
    del xbmcplugin.RESOLVED[:]
    action(default.action_play, id='rd:T2:0', fid='T2', path='', name='Single.Movie.2025.720p.avi', mode='stream')
    check(xbmcplugin.RESOLVED and xbmcplugin.RESOLVED[-1][1].path == link, 'stream plays the Real-Debrid link')

    print('\n12b. A speed test on a Real-Debrid file changes only the Real-Debrid setting')
    xbmcgui.YESNO[:] = [True]
    action(default.action_speedtest, id='rd:T2:0', name='Single.Movie.2025.720p.avi')
    st = xbmcaddon.SETTINGS
    check(str(st.get('rd_connections')) != '10' and str(st.get('connections')) == '8',
          'Real-Debrid now %s, premiumize still %s' % (st.get('rd_connections'), st.get('connections')))

    print('\n13. Sign out, then a private API token instead')
    action(default.action_rd_unpair)
    check(not any(xbmcaddon.SETTINGS.get(k) for k in sources.RD_KEYS), 'all Real-Debrid settings cleared')
    check('Add Real-Debrid' in labels(listing(default.root)), 'menu offers sign-in again')
    xbmcaddon.SETTINGS['rd_api_token'] = 'rdkey'
    check(len(listing(default.rd_list)) == 4, 'API token works (4 torrents left)')
    xbmcaddon.SETTINGS['rd_api_token'] = 'wrong'
    try:
        sources.rd().user()
        check(False, 'bad token rejected')
    except ApiError as exc:
        check(exc.code == 'authentication_failed' and 'Real-Debrid' in str(exc), 'bad token: "%s"' % exc)

    print('\n14. premiumize still works next to it')
    pm = listing(default.cloud)
    check(labels(pm) == ['Movies', 'Shows', 'Readme.txt'] or labels(pm)[:2] == ['Movies', 'Shows'],
          'premiumize root: %s' % labels(pm))
finally:
    if mgr:
        mgr.shutdown()
    if srv:
        srv.stop()
    pm_mock.terminate()
    rd_mock.terminate()

print('\n%d FAILED: %s' % (len(FAILS), FAILS) if FAILS else '\nALL PASSED')
sys.exit(1 if FAILS else 0)
