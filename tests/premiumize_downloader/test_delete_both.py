# -*- coding: utf-8 -*-
"""Deleting removes a file from the device and the cloud together (premiumize and Real-Debrid mocks).

python -I test_delete_both.py WORKDIR
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, '..', '..', 'plugin.video.premiumize.downloader')
WORK = os.path.abspath(sys.argv[1])
PM_PORT, RD_PORT, LOCAL_PORT = 52981, 52982, 52775

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, 'profile'))
VIDEO = os.path.join(WORK, 'small.avi')
with open(VIDEO, 'wb') as f:
    f.write(os.urandom(12 * 1024 * 1024))
os.environ['PMDL_TEST_PROFILE'] = os.path.join(WORK, 'profile')
os.environ['PMDL_API_BASE'] = 'http://127.0.0.1:%d/api/' % PM_PORT
os.environ['PMDL_RD_BASE'] = 'http://127.0.0.1:%d/rest/1.0/' % RD_PORT
os.environ['PMDL_RD_OAUTH'] = 'http://127.0.0.1:%d/oauth/v2/' % RD_PORT
sys.path[:0] = [os.path.join(HERE, 'kodistub'), os.path.join(ADDON, 'resources', 'lib'), ADDON]
sys.argv = ['plugin://plugin.video.premiumize.downloader/', '1', '']

import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402
DL = os.path.join(WORK, 'dl')
xbmcaddon.SETTINGS.update(api_key='testkey', rd_api_token='rdkey', download_path=DL, connections=4,
                          rd_connections=4, buffer_mb=64, subtitles='true', max_concurrent=1, auto_retry='false',
                          server_port=LOCAL_PORT)
from pmdl import client, common, sources  # noqa: E402
from pmdl.manager import Manager  # noqa: E402
from pmdl.server import LocalServer  # noqa: E402
import default  # noqa: E402

FAILS = []


def check(cond, what):
    print('  %s %s' % ('PASS' if cond else 'FAIL', what))
    if not cond:
        FAILS.append(what)


def action(fn, **args):
    default.ARGS.clear()
    default.ARGS.update(args)
    return fn()


def jobs():
    return client.jobs_by_id(wait=1)


def download(item):
    client.cmd('enqueue', items=[item], root=DL, start_id=item['id'])
    end = time.time() + 60
    while time.time() < end:
        mgr.tick()
        if (jobs().get(item['id']) or {}).get('status') == 'done':
            return jobs()[item['id']]
        time.sleep(0.25)
    raise AssertionError('download of %s did not finish' % item['id'])


def settle():
    for _ in range(8):
        mgr.tick()
        time.sleep(0.25)


def pm_names(fid):
    try:
        return sorted(e['name'] for e in sources.pm().folder_list(fid)['content'])
    except Exception as exc:
        return 'gone (%s)' % getattr(exc, 'code', exc)


def asked():
    return xbmcgui.ASKED[-1][1] if xbmcgui.ASKED else ''


quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
pm_mock = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'mock_premiumize.py'), VIDEO, str(PM_PORT), '8'], **quiet)
rd_mock = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'mock_realdebrid.py'), VIDEO, str(RD_PORT), '8'], **quiet)
time.sleep(1.0)
mgr = srv = None
try:
    mgr = Manager()
    srv = LocalServer(mgr, 'tok', LOCAL_PORT)
    common.HOME.setProperty(common.PROP_PORT, str(srv.port))
    common.HOME.setProperty(common.PROP_TOKEN, 'tok')
    size = os.path.getsize(VIDEO)

    print('\n1. premiumize: deleting the only video of a folder takes the folder along')
    v1 = action(default.item_for_args, id='v1', fid='ftm', path='Movies/Test.Movie.2026.720p',
                name='Test.Movie.2026.720p.avi', size=str(size))
    j = download(v1)
    folder = os.path.dirname(j['dest'])
    check(sorted(os.listdir(folder)) == ['Test.Movie.2026.720p.2_Arabic.srt', 'Test.Movie.2026.720p.English.srt',
                                         'Test.Movie.2026.720p.avi'], 'downloaded with its two subtitles')
    action(default.action_delete, id='v1', name='Test.Movie.2026.720p.avi')
    check('from this device and from your premiumize cloud' in asked() and 'Its folder "Test.Movie.2026.720p" goes too'
          in asked(), 'asked: %s' % asked().replace('[CR]', ' | '))
    settle()
    check(pm_names('fm') == [], 'cloud folder gone with its subtitles: Movies now %s' % pm_names('fm'))
    check(not os.path.exists(folder) and 'v1' not in jobs(), 'device copy, subtitles and folder gone')

    print('\n2. premiumize: an episode next to other episodes keeps the folder')
    e1 = action(default.item_for_args, id='e1', fid='fss', path='Shows/Show S01', name='Show.S01E01.avi')
    j = download(e1)
    action(default.action_delete, id='e1', name='Show.S01E01.avi')
    check('Its subtitles go too' in asked() and 'folder' not in asked(), 'asked: %s' % asked().replace('[CR]', ' | '))
    settle()
    check(pm_names('fss') == ['Show.S01E02.avi'], 'cloud: E01 and its subtitle gone, E02 stays: %s' % pm_names('fss'))
    check(not os.path.exists(j['dest']) and 'e1' not in jobs(), 'device copy gone')

    print('\n3. premiumize: deleting a cloud folder deletes what was downloaded from it')
    e2 = action(default.item_for_args, id='e2', fid='fss', path='Shows/Show S01', name='Show.S01E02.avi')
    j = download(e2)
    action(default.action_cloud_delete, kind='folder', id='fs', name='Shows')
    check('1 file downloaded from it to this device is deleted too' in asked(), 'asked: %s' % asked().replace('[CR]', ' | '))
    settle()
    check('Shows' not in pm_names('root') and not os.path.exists(j['dest']) and 'e2' not in jobs(),
          'cloud folder and device copy gone; root now %s' % pm_names('root'))

    print('\n4. premiumize: the top level of the cloud is never deleted')
    check(default._pm_cleanup_folder(sources.pm(), 'root', set()) is None, 'root is never a cleanup folder')

    print('\n5. Real-Debrid: deleting one episode deletes the whole torrent, other copies stay')
    a = download(action(default.item_for_args, id='rd:T3:0', fid='T3', path='Show.S01.1080p', name='Show.S01E01.avi'))
    b = download(action(default.item_for_args, id='rd:T3:1', fid='T3', path='Show.S01.1080p', name='Show.S01E02.avi'))
    action(default.action_delete, id='rd:T3:0', name='Show.S01E01.avi')
    check('all 2 files of "Show.S01.1080p" go from Real-Debrid' in asked() and '1 other file from it already on this '
          'device stays' in asked(), 'asked: %s' % asked().replace('[CR]', ' | '))
    settle()
    check('T3' not in [t['id'] for t in sources.rd().torrents()], 'torrent gone from Real-Debrid')
    check(not os.path.exists(a['dest']) and os.path.exists(b['dest']) and 'rd:T3:1' in jobs(),
          'E01 gone from the device, E02 still there')

    print('\n6. Real-Debrid: a copy whose torrent is already gone is just deleted from the device')
    action(default.action_delete, id='rd:T3:1', name='Show.S01E02.avi')
    check('no longer on Real-Debrid' in asked(), 'asked: %s' % asked().replace('[CR]', ' | '))
    settle()
    check(not os.path.exists(b['dest']) and 'rd:T3:1' not in jobs(), 'E02 gone from the device')

    print('\n7. Real-Debrid: deleting a torrent deletes what was downloaded from it')
    c = download(action(default.item_for_args, id='rd:T1:0', fid='T1', path='Test.Movie.2026.1080p',
                        name='Test.Movie.2026.1080p.avi'))
    action(default.action_cloud_delete, kind='torrent', id='T1', name='Test.Movie.2026.1080p')
    check('1 file downloaded from it to this device is deleted too' in asked(), 'asked: %s' % asked().replace('[CR]', ' | '))
    settle()
    check('T1' not in [t['id'] for t in sources.rd().torrents()] and not os.path.exists(c['dest']),
          'torrent and device copy gone')

    print('\n8. Saying "Keep" deletes nothing')
    d = download(action(default.item_for_args, id='rd:T2:0', fid='T2', path='', name='Single.Movie.2025.720p.avi'))
    xbmcgui.YESNO[:] = [False]
    action(default.action_delete, id='rd:T2:0', name='Single.Movie.2025.720p.avi')
    settle()
    check(os.path.exists(d['dest']) and 'T2' in [t['id'] for t in sources.rd().torrents()], 'both still there')

    print('\n9. Menus')
    items = [(u, li) for u, li, _f in __import__('xbmcplugin').ITEMS]
    del __import__('xbmcplugin').ITEMS[:]
    action(default.downloads)
    labels = [c[0] for _u, li, _f in __import__('xbmcplugin').ITEMS for c in li.context]
    check('Delete from device and cloud' in labels and 'Delete from this device' not in labels,
          'Downloads offers "Delete from device and cloud"')
finally:
    if mgr:
        mgr.shutdown()
    if srv:
        srv.stop()
    pm_mock.terminate()
    rd_mock.terminate()

print('\n%d FAILED: %s' % (len(FAILS), FAILS) if FAILS else '\nALL PASSED')
sys.exit(1 if FAILS else 0)
