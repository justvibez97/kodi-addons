# -*- coding: utf-8 -*-
"""Drive detection against a fake Android /storage layout.  python -I test_storage.py"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, 'kodistub'),
                os.path.join(HERE, '..', '..', 'plugin.video.premiumize.downloader', 'resources', 'lib')]
from pmdl import storage  # noqa: E402

fails = []


def check(cond, what):
    print('  %s %s' % ('PASS' if cond else 'FAIL', what))
    if not cond:
        fails.append(what)


with tempfile.TemporaryDirectory() as tmp:
    root = os.path.join(tmp, 'storage').replace('\\', '/')
    for d in ('emulated/0', 'self/primary', '1A2B-3C4D', '9F00-AB12'):
        os.makedirs(os.path.join(root, d))
    mounts = [
        ['/dev/fuse', root + '/emulated', 'fuse'],
        ['/dev/fuse', root + '/1A2B-3C4D', 'fuse'],
        ['/dev/block/vold/public:8,1', '/mnt/media_rw/1A2B-3C4D', 'exfat'],
        ['/dev/block/vold/public:8,17', '/mnt/media_rw/9F00-AB12', 'vfat'],
        ['/dev/block/vold/public:8,33', '/mnt/media_rw/DEAD-BEEF', 'ntfs'],   # no /storage folder
    ]
    vols = storage.volumes(root, mounts)
    labels = [v[0] for v in vols]
    check(labels == ['USB drive 1A2B-3C4D', 'USB drive 9F00-AB12', 'Shield internal storage'],
          'USB drives first, then internal: %s' % labels)
    check(all(not v[1].endswith(('/emulated', '/self')) for v in vols), 'emulated/self are not offered as drives')

    os.rename(os.path.join(root, '9F00-AB12'), os.path.join(tmp, 'gone'))
    labels = [v[0] for v in storage.volumes(root, mounts)]
    check(labels == ['USB drive 1A2B-3C4D', 'Shield internal storage'], 'a mount without a /storage folder is skipped')

    os.chmod(root, 0o755)
    only_mounts = os.path.join(tmp, 'storage2').replace('\\', '/')
    os.makedirs(os.path.join(only_mounts, 'CAFE-0001'))
    found = storage.usb_drives(only_mounts, [['/dev/block/vold/public:8,1', '/mnt/media_rw/CAFE-0001', 'exfat']])
    check([os.path.basename(p) for p in found] == ['CAFE-0001'], 'drive found from /proc/mounts as well')

    desc = storage.describe(root)
    check(' free of ' in desc, 'describe shows free and total: "%s"' % desc)

print('\n%d FAILED' % len(fails) if fails else '\nALL PASSED')
sys.exit(1 if fails else 0)
