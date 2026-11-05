# -*- coding: utf-8 -*-
"""Where downloads can go: drives, free space, write access, file-size limits."""
import os
import shutil

FAT32_LIMIT = 4 * 1024 ** 3 - 1


def is_android():
    try:
        import xbmc
        return bool(xbmc.getCondVisibility('System.Platform.Android'))
    except Exception:
        return os.path.isdir('/storage/emulated')


def volumes():
    """Drives worth offering on Android: [(label, root_path)]."""
    out = []
    if not os.path.isdir('/storage'):
        return out
    internal = '/storage/emulated/0'
    try:
        names = sorted(os.listdir('/storage'))
    except OSError:
        names = []
    for name in names:
        if name in ('emulated', 'self', 'sdcard0', 'sdcard1'):
            continue
        path = '/storage/' + name
        if os.path.isdir(path):
            out.append(('USB drive %s' % name, path))
    if os.path.isdir(internal):
        out.append(('Internal storage', internal))
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
            return ('Kodi can\'t write to %s (%s).\n\nOn the Shield: Settings > Apps > Special app '
                    'access > All files access > Kodi > Allow. Then try again.' % (folder, exc.strerror or exc))
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
    free = free_space(path)
    from .common import human_gb
    return '%s free' % human_gb(free) if free is not None else 'free space unknown'
