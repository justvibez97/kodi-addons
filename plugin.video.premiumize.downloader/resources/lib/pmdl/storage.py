# -*- coding: utf-8 -*-
"""Where downloads can go: drives, free space, write access, file-size limits."""
import os
import shutil
import threading
import time

FAT32_LIMIT = 4 * 1024 ** 3 - 1


def is_android():
    try:
        import xbmc
        return bool(xbmc.getCondVisibility('System.Platform.Android'))
    except Exception:
        return os.path.isdir('/storage/emulated')


NOT_DRIVES = ('emulated', 'self', 'sdcard0', 'sdcard1', 'knox-emulated')


def usb_drives(storage_root='/storage', mounts=None):
    """Removable drives as /storage/<volume id> paths.

    Reads the /storage folder and /proc/mounts directly, so a drive shows up
    even when Android doesn't report it to Kodi's own folder browser.
    """
    names = set()
    try:
        names.update(os.listdir(storage_root))
    except OSError:
        pass
    root = storage_root.rstrip('/') + '/'
    for _dev, mnt, _typ in (_mounts() if mounts is None else mounts):
        for prefix in (root, '/mnt/media_rw/'):
            if mnt.startswith(prefix):
                names.add(mnt[len(prefix):].split('/')[0])
    out = []
    for name in sorted(names):
        if not name or name in NOT_DRIVES:
            continue
        path = storage_root.rstrip('/') + '/' + name
        if os.path.isdir(path):
            out.append(path)
    return out


def volumes(storage_root='/storage', mounts=None):
    """Drives worth offering on Android, USB drives first: [(label, root_path)]."""
    out = [('USB drive %s' % os.path.basename(p), p) for p in usb_drives(storage_root, mounts)]
    internal = storage_root.rstrip('/') + '/emulated/0'
    if os.path.isdir(internal):
        out.append(('Shield internal storage', internal))
    return out


def existing_parent(path):
    path = os.path.abspath(path)
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def free_space(path):
    try:
        return shutil.disk_usage(existing_parent(path)).free
    except OSError:
        return None


def check_writable(folder):
    """None if we can create files in ``folder``, otherwise an explanation."""
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, '.pmdl-write-test')
        with open(probe, 'wb') as f:
            f.write(b'ok')
        os.remove(probe)
        return None
    except OSError as exc:
        if is_android():
            return ('Kodi can\'t write to %s (%s).[CR][CR]On the Shield: Settings > Apps > Kodi > Permissions > '
                    'Files and media > Allow management of all files. Then try again.' % (folder, exc.strerror or exc))
        return 'Can\'t write to %s: %s' % (folder, exc.strerror or exc)


def _mounts():
    try:
        with open('/proc/mounts', 'r') as f:
            return [line.split()[:3] for line in f if len(line.split()) >= 3]
    except (IOError, OSError):
        return []


def fs_type(path):
    """Filesystem type behind ``path`` (Linux/Android only), e.g. 'vfat', 'exfat', 'ext4'."""
    real = os.path.realpath(existing_parent(path))
    mounts = _mounts()
    best, best_type = '', None
    for dev, mnt, typ in mounts:
        if (real == mnt or real.startswith(mnt.rstrip('/') + '/')) and len(mnt) > len(best):
            best, best_type = mnt, typ
    if best_type in ('fuse', 'sdcardfs', 'fuseblk') and real.startswith('/storage/'):
        # Android wraps removable drives in FUSE; the real filesystem is
        # mounted at /mnt/media_rw/<volume id>.
        vol = real.split('/')[2]
        for dev, mnt, typ in mounts:
            if mnt.rstrip('/') == '/mnt/media_rw/' + vol:
                return typ
    return best_type


def max_file_size(path):
    return FAT32_LIMIT if fs_type(path) in ('vfat', 'msdos') else None


def describe(path):
    """'1.6 TB free of 2.0 TB' (the size helps tell drives apart)."""
    from .common import human_gb
    try:
        usage = shutil.disk_usage(existing_parent(path))
    except OSError:
        return 'free space unknown'
    return '%s free of %s' % (human_gb(usage.free), human_gb(usage.total))


class WriteTest(threading.Thread):
    """Measure how fast a folder's drive really stores data.

    Writes incompressible data and forces it to the drive every 64 MB, so the
    result is the drive's own speed rather than Android's memory cache. Runs
    in its own thread because a slow drive can block a single write for a
    long time; the caller polls :meth:`stats`.
    """

    def __init__(self, folder, seconds=30, limit=1024 ** 3, block=4 * 1024 ** 2, sync_every=64 * 1024 ** 2):
        threading.Thread.__init__(self, name='pmdl-drive-test')
        self.daemon = True
        self.folder = folder
        self.seconds = seconds
        self.limit = limit
        self.block = block
        self.sync_every = sync_every
        self.samples = [(0.0, 0)]
        self.error = None
        self.finished = False
        self.stop_flag = False
        self.t0 = None

    def run(self):
        path = os.path.join(self.folder, '.pmdl-drive-test.tmp')
        fd = None
        try:
            os.makedirs(self.folder, exist_ok=True)
            data = memoryview(os.urandom(self.block))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0), 0o644)
            self.t0 = time.time()
            written = unsynced = 0
            while not self.stop_flag and written < self.limit and time.time() - self.t0 < self.seconds:
                view = data
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                    written += n
                    unsynced += n
                if unsynced >= self.sync_every:
                    os.fsync(fd)
                    unsynced = 0
                self.samples.append((time.time() - self.t0, written))
            os.fsync(fd)
            self.samples.append((time.time() - self.t0, written))
        except OSError as exc:
            self.error = exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.remove(path)
            except OSError:
                pass
            self.finished = True

    def stats(self):
        """(seconds, bytes, average B/s, recent B/s, longest pause in s)."""
        samples = list(self.samples)
        now = (time.time() - self.t0) if (self.t0 and not self.finished) else samples[-1][0]
        t, total = samples[-1]
        elapsed = max(now, t, 0.001)
        recent = [s for s in samples if s[0] >= elapsed - 3.0]
        if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
            rate_now = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
        else:
            rate_now = 0.0
        if now - t > 3.0:
            rate_now = 0.0          # a write has been blocking for a while
        gaps = [b[0] - a[0] for a, b in zip(samples, samples[1:])]
        pause = max(gaps + [now - t if not self.finished else 0.0])
        return elapsed, total, total / elapsed, rate_now, pause
