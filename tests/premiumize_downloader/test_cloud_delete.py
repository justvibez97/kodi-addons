# -*- coding: utf-8 -*-
"""Cloud delete against the mock API.  python -I test_cloud_delete.py WORKDIR VIDEO_FILE"""
import os, shutil, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, '..', '..', 'plugin.video.premiumize.downloader')
WORK, VIDEO, PORT = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]), 52994
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, 'profile'))
os.environ['PMDL_TEST_PROFILE'] = os.path.join(WORK, 'profile')
os.environ['PMDL_API_BASE'] = 'http://127.0.0.1:%d/api/' % PORT
sys.path[:0] = [os.path.join(HERE, 'kodistub'), os.path.join(ADDON, 'resources', 'lib'), ADDON]
sys.argv = ['plugin://plugin.video.premiumize.downloader/', '-1', '']
import xbmcaddon  # noqa: E402
xbmcaddon.SETTINGS.update(api_key='testkey', download_path=os.path.join(WORK, 'dl'), connections=2, buffer_mb=64,
                          max_concurrent=1, server_port=52773)
from pmdl import client, common  # noqa: E402
from pmdl.manager import Manager  # noqa: E402
from pmdl.server import LocalServer  # noqa: E402
from pmdl.api import Premiumize  # noqa: E402
import default  # noqa: E402

fails = []
def check(cond, what):
    print('  %s %s' % ('PASS' if cond else 'FAIL', what))
    if not cond:
        fails.append(what)

mock = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'mock_premiumize.py'), VIDEO, str(PORT), '0.3'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)
mgr = Manager(); srv = LocalServer(mgr, 'tok', 52773)
common.HOME.setProperty(common.PROP_PORT, str(srv.port)); common.HOME.setProperty(common.PROP_TOKEN, 'tok')
prem = Premiumize('testkey')
try:
    names = lambda fid: [e['name'] for e in prem.folder_list(fid)['content']]
    # a file that is downloading right now
    default.ARGS.clear(); default.ARGS.update(id='e2', fid='fss', path='Shows/Show S01', name='Show.S01E02.avi')
    client.cmd('enqueue', items=[default.single_item(prem)], root=os.path.join(WORK, 'dl'))
    time.sleep(2); mgr.tick(); time.sleep(2)
    check(client.jobs_by_id()['e2']['status'] == 'downloading', 'E02 is downloading before the delete')
    default.ARGS.clear(); default.ARGS.update(kind='file', id='e2', name='Show.S01E02.avi', path='Shows/Show S01')
    default.action_cloud_delete()
    time.sleep(2); mgr.tick()
    check('Show.S01E02.avi' not in names('fss') and 'Show.S01E01.avi' in names('fss'),
          'only E02 removed from the cloud: %s' % names('fss'))
    check('e2' not in client.jobs_by_id(), 'its unfinished download was cancelled')
    # a whole folder
    default.ARGS.clear(); default.ARGS.update(kind='folder', id='ftm', name='Test.Movie.2026.720p', path='Movies/Test.Movie.2026.720p')
    default.action_cloud_delete()
    check(names('fm') == [], 'folder and everything in it removed: Movies now %s' % names('fm'))
    check(names('root'), 'the rest of the cloud is untouched: %s' % names('root'))
finally:
    mgr.shutdown(); srv.stop(); mock.terminate()
print('\n%d FAILED' % len(fails) if fails else '\nALL PASSED')
sys.exit(1 if fails else 0)
