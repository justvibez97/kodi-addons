# -*- coding: utf-8 -*-
"""Small helpers shared by the plugin and the service."""
import json
import os
import re
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON_ID = 'plugin.video.premiumize.downloader'
PLUGIN = 'plugin://%s/' % ADDON_ID
HOME = xbmcgui.Window(10000)
PROP_PORT = 'pmdl.port'
PROP_TOKEN = 'pmdl.token'

VIDEO_EXT = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.ts', '.m2ts', '.mts', '.mpg',
             '.mpeg', '.webm', '.flv', '.vob', '.divx', '.ogv', '.3gp', '.iso'}
SUB_EXT = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.smi', '.sup'}
MIME = {'.mkv': 'video/x-matroska', '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.avi': 'video/x-msvideo',
        '.mov': 'video/quicktime', '.wmv': 'video/x-ms-wmv', '.ts': 'video/mp2t', '.m2ts': 'video/mp2t',
        '.mts': 'video/mp2t', '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg', '.webm': 'video/webm',
        '.flv': 'video/x-flv', '.vob': 'video/mpeg', '.ogv': 'video/ogg', '.3gp': 'video/3gpp'}


def play_meta(li, name, folder=None):
    """Give a ListItem about to play a clean title, year and type.

    Kodi shows these during playback, and the Trakt add-on uses them to
    scrobble videos that aren't in the library (it matches title + year, or
    show + season + episode). Unreadable names keep the plain file name.
    """
    from .naming import parse
    info = parse(name, folder)
    kind = info['type']
    try:
        tag = li.getVideoInfoTag()
    except AttributeError:   # Kodi 19
        tag = None
    if kind == 'movie':
        values = {'mediatype': 'movie', 'title': info['title'], 'year': info['year']}
    elif kind == 'episode':
        values = {'mediatype': 'episode', 'tvshowtitle': info['show'], 'season': info['season'],
                  'episode': info['episode'],
                  'title': '%s S%02dE%02d' % (info['show'], info['season'], info['episode'])}
        if info['year']:
            values['year'] = info['year']
    else:
        values = {'mediatype': 'video', 'title': name}
    if tag is None:
        li.setInfo('video', values)
        return info
    tag.setMediaType(values['mediatype'])
    tag.setTitle(values['title'])
    if values.get('year'):
        tag.setYear(int(values['year']))
    if kind == 'episode':
        tag.setTvShowTitle(values['tvshowtitle'])
        tag.setSeason(int(values['season']))
        tag.setEpisode(int(values['episode']))
    return info


def addon():
    return xbmcaddon.Addon(ADDON_ID)


def setting(key):
    return addon().getSetting(key)


def setting_bool(key):
    try:
        return addon().getSettingBool(key)
    except Exception:
        return addon().getSetting(key) == 'true'


def setting_int(key, default=0):
    try:
        return addon().getSettingInt(key)
    except Exception:
        try:
            return int(addon().getSetting(key))
        except ValueError:
            return default


def set_setting(key, value):
    addon().setSetting(key, value)


def profile_dir():
    path = xbmcvfs.translatePath(addon().getAddonInfo('profile'))
    os.makedirs(path, exist_ok=True)
    return path


def icon():
    return os.path.join(xbmcvfs.translatePath(addon().getAddonInfo('path')), 'icon.png')


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[Premiumize DL] %s' % msg, level)


def debug(msg):
    if setting_bool('debug'):
        log(msg)
    else:
        xbmc.log('[Premiumize DL] %s' % msg, xbmc.LOGDEBUG)


def notify(message, heading='Premiumize Downloader', ms=4000):
    xbmcgui.Dialog().notification(heading, message, icon(), ms, False)


def api_token():
    return (setting('oauth_token') or setting('api_key')).strip()


# ----------------------------------------------------------------- formatting

def human_size(n):
    if n is None:
        return '?'
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            if unit == 'B':
                return '%d B' % n
            return ('%.1f %s' if n < 100 else '%.0f %s') % (n, unit)
        n /= 1024.0
    return '%.1f TB' % (n / 1024.0)


def human_gb(n):
    if n >= 1024 ** 4:
        return '%.1f TB' % (n / 1024.0 ** 4)
    return '%.1f GB' % (n / 1073741824.0) if n >= 1073741824 else human_size(n)


def human_speed(bps):
    if not bps:
        return '-'
    mbps = bps / 1048576.0
    if mbps >= 1:
        return ('%.1f MB/s' if mbps < 100 else '%.0f MB/s') % mbps
    return '%.0f KB/s' % (bps / 1024.0)


def human_eta(sec):
    if sec is None:
        return ''
    sec = int(sec)
    if sec < 60:
        return '%ds' % sec
    if sec < 3600:
        return '%dm %02ds' % (sec // 60, sec % 60)
    return '%dh %02dm' % (sec // 3600, (sec % 3600) // 60)


# ----------------------------------------------------------------- files

def ext(name):
    return os.path.splitext(name)[1].lower()


def is_video(name, mime=''):
    return ext(name) in VIDEO_EXT or (mime or '').startswith('video/')


def is_sub(name):
    return ext(name) in SUB_EXT


def mime_for(name):
    return MIME.get(ext(name), 'application/octet-stream')


_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name):
    """A file or folder name that FAT32, exFAT, NTFS and ext4 all accept."""
    name = _BAD.sub('_', name).strip().rstrip('.')
    return name[:240] or '_'


def safe_rel(parts):
    return os.path.join(*[safe_name(p) for p in parts if p]) if parts else ''


# ----------------------------------------------------------------- json files

def read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.1)
    os.replace(tmp, path)
