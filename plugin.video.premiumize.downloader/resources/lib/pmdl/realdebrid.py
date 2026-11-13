# -*- coding: utf-8 -*-
"""Real-Debrid API client (stdlib only, no Kodi imports).

Sign-in uses Real-Debrid's device pairing for open-source apps: the user
enters a short code at real-debrid.com/device, and this device gets its own
client id/secret plus a refresh token. A private API token works too.
Credentials live in a ``store`` (anything with get(key) / set(key, value)).
"""
import json
import os
import posixpath
import time
import urllib.error
import urllib.parse
import urllib.request

from .api import ApiError as _ApiError


def ApiError(message, code=None):
    """The shared ApiError, keeping this module's Real-Debrid wording as is."""
    return _ApiError(message, code, exact=True)


# Only the test harness sets these (mock server); normal installs never do.
API = os.environ.get('PMDL_RD_BASE') or 'https://api.real-debrid.com/rest/1.0/'
OAUTH = os.environ.get('PMDL_RD_OAUTH') or 'https://api.real-debrid.com/oauth/v2/'
OPEN_CLIENT_ID = 'X245A4XAIBGVM'        # Real-Debrid's public client id for open-source apps
DEVICE_GRANT = 'http://oauth.net/grant_type/device/1.0'
USER_AGENT = 'Kodi-DebridDownloader/1.1'
PAGE = 500

# Real-Debrid error_code -> (our error code, message). Our codes match the
# premiumize ones where they mean the same, so the queue treats them alike.
ERRORS = {
    5: ('rate_limit_reached', 'Too many requests to Real-Debrid. Try again in a minute.'),
    34: ('rate_limit_reached', 'Too many requests to Real-Debrid. Try again in a minute.'),
    7: ('not_found', 'That torrent or file is no longer on Real-Debrid.'),
    8: ('authentication_failed', 'The Real-Debrid sign-in is no longer valid. Sign in again: '
                                 'add-on settings > Real-Debrid.'),
    9: ('permission_denied', 'Real-Debrid says this account can\'t do that (is premium active?).'),
    14: ('permission_denied', 'Your Real-Debrid account is locked.'),
    21: ('busy', 'Real-Debrid: too many active downloads on this account right now.'),
    22: ('permission_denied', 'Real-Debrid doesn\'t accept requests from this IP address.'),
    23: ('traffic', 'Real-Debrid: this account\'s traffic is used up.'),
    24: ('file_unavailable', 'Real-Debrid says this file is unavailable.'),
    25: ('service_down', 'Real-Debrid is having problems right now. Try again later.'),
    35: ('file_unavailable', 'Real-Debrid blocked this file.'),
    36: ('traffic', 'Real-Debrid fair-use limit reached. Try again later.'),
}

STATUS_TEXT = {
    'magnet_conversion': 'reading magnet', 'waiting_files_selection': 'waiting for file selection',
    'queued': 'queued on Real-Debrid', 'downloading': 'downloading to Real-Debrid',
    'compressing': 'packing on Real-Debrid', 'uploading': 'uploading on Real-Debrid',
    'magnet_error': 'magnet error', 'error': 'error', 'virus': 'virus found', 'dead': 'dead torrent',
}


def _request(method, url, data=None, token=None, timeout=25):
    """(parsed JSON or None, HTTP status, headers). Never raises for HTTP errors."""
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = urllib.parse.urlencode(data).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status, hdrs = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, hdrs = exc.read(), exc.code, exc.headers
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise ApiError('Can\'t reach Real-Debrid (%s). Check the internet connection.' % reason)
    if not raw.strip():
        return None, status, hdrs
    try:
        return json.loads(raw.decode('utf-8')), status, hdrs
    except ValueError:
        raise ApiError('Real-Debrid sent an unreadable reply (HTTP %d)' % status)


def _error(j, status):
    j = j if isinstance(j, dict) else {}
    num = j.get('error_code')
    if num in ERRORS:
        code, message = ERRORS[num]
        return ApiError(message, code)
    if status == 401:
        return ApiError(ERRORS[8][1], 'authentication_failed')
    if status == 429:
        return ApiError(ERRORS[34][1], 'rate_limit_reached')
    return ApiError('Real-Debrid error: %s (HTTP %d)' % (j.get('error') or 'unknown', status), j.get('error'))


def item_id(torrent_id, n):
    """Queue id of the n-th downloadable link of a torrent."""
    return 'rd:%s:%d' % (torrent_id, n)


def is_rd(any_id):
    return str(any_id).startswith('rd:')


def split_id(any_id):
    _rd, torrent_id, n = any_id.split(':')
    return torrent_id, int(n)


class RealDebrid(object):
    def __init__(self, store):
        self.store = store
        if not configured(store):
            raise ApiError('Real-Debrid isn\'t set up yet. Select "Add Real-Debrid" in the main menu, '
                           'or open the add-on settings > Real-Debrid.', 'no_key')

    # ------------------------------------------------------------- tokens

    def _token(self, refresh=False):
        if not self.store.get('rd_refresh'):
            return self.store.get('rd_api_token').strip()
        token = self.store.get('rd_token')
        try:
            expires = float(self.store.get('rd_expires') or 0)
        except ValueError:
            expires = 0
        if token and not refresh and time.time() < expires - 300:
            return token
        j, status, _h = _request('POST', OAUTH + 'token', data={
            'client_id': self.store.get('rd_client_id'), 'client_secret': self.store.get('rd_client_secret'),
            'code': self.store.get('rd_refresh'), 'grant_type': DEVICE_GRANT})
        if not (j or {}).get('access_token'):
            if status in (400, 401, 403):
                raise ApiError(ERRORS[8][1], 'authentication_failed')
            raise _error(j, status)
        save_token(self.store, j)
        return j['access_token']

    def _call(self, method, path, params=None, data=None, with_headers=False):
        url = API + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        token = self._token()
        j, status, hdrs = _request(method, url, data, token)
        if status == 401 and self.store.get('rd_refresh'):
            # Expired early (or revoked): one refresh, then give up.
            j, status, hdrs = _request(method, url, data, self._token(refresh=True))
        elif status == 401:
            raise ApiError('Real-Debrid rejected the private API token. Check it in the add-on settings > '
                           'Real-Debrid, or sign in with a code instead.', 'authentication_failed')
        if status >= 400:
            raise _error(j, status)
        return (j, hdrs) if with_headers else j

    # ------------------------------------------------------------- calls

    def user(self):
        return self._call('GET', 'user')

    def torrents(self):
        """Every torrent in the account, newest first."""
        out, offset = [], 0
        while True:
            batch, hdrs = self._call('GET', 'torrents', {'offset': offset, 'limit': PAGE}, with_headers=True)
            batch = batch or []
            out += batch
            try:
                total = int(hdrs.get('X-Total-Count'))
            except (TypeError, ValueError):
                total = None
            offset += len(batch)
            if len(batch) < PAGE or (total is not None and offset >= total) or offset > 50000:
                break
        out.sort(key=lambda t: t.get('added') or '', reverse=True)
        return out

    def torrent_info(self, torrent_id):
        return self._call('GET', 'torrents/info/' + urllib.parse.quote(torrent_id, safe=''))

    def delete_torrent(self, torrent_id):
        return self._call('DELETE', 'torrents/delete/' + urllib.parse.quote(torrent_id, safe=''))

    def unrestrict(self, link):
        j = self._call('POST', 'unrestrict/link', data={'link': link})
        if not (j or {}).get('download'):
            raise ApiError('Real-Debrid returned no download link for this file')
        return j

    def files(self, info, resolve_limit=50):
        """Downloadable files of a finished torrent, as cloud entries.

        Real-Debrid gives one link per selected file, in file order. When the
        counts differ (it packed small files into an archive), each link is
        unrestricted to learn its name and size instead.
        """
        tid = info['id']
        selected = [f for f in info.get('files') or [] if f.get('selected')]
        links = info.get('links') or []
        out = []
        if len(selected) == len(links):
            for n, (f, link) in enumerate(zip(selected, links)):
                path = (f.get('path') or '').lstrip('/')
                out.append({'id': item_id(tid, n), 'name': posixpath.basename(path) or info.get('filename'),
                            'size': int(f.get('bytes') or 0), 'type': 'file', 'src_link': link,
                            'dir': posixpath.dirname(path)})
            return out
        for n, link in enumerate(links[:resolve_limit]):
            u = self.unrestrict(link)
            out.append({'id': item_id(tid, n), 'name': u.get('filename') or 'file %d' % (n + 1),
                        'size': int(u.get('filesize') or 0), 'type': 'file', 'src_link': link, 'dir': '',
                        'mime_type': u.get('mimeType')})
        return out


def configured(store):
    return bool(store.get('rd_refresh') or store.get('rd_api_token').strip())


def save_token(store, j):
    store.set('rd_token', j['access_token'])
    store.set('rd_expires', '%d' % (time.time() + int(j.get('expires_in') or 3600)))
    if j.get('refresh_token'):
        store.set('rd_refresh', j['refresh_token'])


# ----------------------------------------------------------------- device pairing

def device_code():
    j, status, _h = _request('GET', OAUTH + 'device/code?' + urllib.parse.urlencode(
        {'client_id': OPEN_CLIENT_ID, 'new_credentials': 'yes'}))
    if not (j or {}).get('device_code'):
        raise _error(j, status)
    return j


def poll_credentials(code):
    """This device's own client id/secret once the user approved, None while waiting."""
    j, status, _h = _request('GET', OAUTH + 'device/credentials?' + urllib.parse.urlencode(
        {'client_id': OPEN_CLIENT_ID, 'code': code}))
    if (j or {}).get('client_secret'):
        return j
    if status == 403:
        return None             # not approved yet
    raise _error(j, status)


def device_token(client_id, client_secret, code):
    j, status, _h = _request('POST', OAUTH + 'token', data={
        'client_id': client_id, 'client_secret': client_secret, 'code': code, 'grant_type': DEVICE_GRANT})
    if not (j or {}).get('access_token'):
        raise _error(j, status)
    return j
