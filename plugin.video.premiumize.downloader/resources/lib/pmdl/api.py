# -*- coding: utf-8 -*-
"""premiumize.me API client (stdlib only, no Kodi imports)."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

# PMDL_API_BASE is only set by the test harness (mock server); normal installs never set it.
API = os.environ.get('PMDL_API_BASE') or 'https://www.premiumize.me/api/'
TOKEN_URL = 'https://www.premiumize.me/token'
USER_AGENT = 'Kodi-PremiumizeDownloader/1.0'

FRIENDLY = {
    'authentication_failed': 'premiumize.me rejected the API key. Check it in the add-on settings.',
    'permission_denied': 'premiumize.me says this account can\'t do that (is it premium?).',
    'not_found': 'That file or folder no longer exists in your cloud.',
    'rate_limit_reached': 'Too many requests to premiumize.me. Try again in a minute.',
    'service_down': 'premiumize.me is having problems right now. Try again later.',
}


class ApiError(Exception):
    def __init__(self, message, code=None):
        Exception.__init__(self, FRIENDLY.get(code, message))
        self.code = code


def _request(url, data=None, token=None, timeout=25):
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = urllib.parse.urlencode(data, doseq=True).encode('utf-8') if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError:
            raise ApiError('premiumize.me answered HTTP %d' % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise ApiError('Can\'t reach premiumize.me (%s). Check the internet connection.' % reason)
    try:
        return json.loads(raw.decode('utf-8'))
    except ValueError:
        raise ApiError('premiumize.me sent an unreadable reply')


class Premiumize(object):
    def __init__(self, token):
        if not token:
            raise ApiError('No API key yet. Open the add-on settings and paste your API key '
                           'from premiumize.me/account.', 'no_key')
        self.token = token

    def _call(self, path, params=None, data=None):
        url = API + path
        if params:
            url += '?' + urllib.parse.urlencode(params, doseq=True)
        j = _request(url, data=data, token=self.token)
        if j.get('status') != 'success':
            raise ApiError(j.get('message') or 'premiumize.me error', j.get('code'))
        return j

    def account_info(self):
        return self._call('account/info')

    def folder_list(self, folder_id=None, breadcrumbs=False):
        params = {}
        if folder_id:
            params['id'] = folder_id
        if breadcrumbs:
            params['includebreadcrumbs'] = 'true'
        return self._call('folder/list', params)

    def folder_search(self, query):
        return self._call('folder/search', {'q': query})

    def item_details(self, item_id):
        return self._call('item/details', {'id': item_id})

    def delete_item(self, item_id):
        return self._call('item/delete', data={'id': item_id})

    def delete_folder(self, folder_id):
        return self._call('folder/delete', data={'id': folder_id})

    def link(self, item_id):
        link = self.item_details(item_id).get('link')
        if not link:
            raise ApiError('premiumize.me returned no download link for this file')
        return link


# ----------------------------------------------------------------- OAuth device pairing

def device_code(client_id):
    j = _request(TOKEN_URL, data={'response_type': 'device_code', 'client_id': client_id})
    if 'device_code' not in j:
        raise ApiError(j.get('error_description') or j.get('error') or 'Pairing is not available')
    return j


def poll_device_token(client_id, code):
    """Returns the access token, None while pending, or raises ApiError."""
    j = _request(TOKEN_URL, data={'grant_type': 'device_code', 'code': code, 'client_id': client_id})
    if j.get('access_token'):
        return j['access_token']
    err = j.get('error')
    if err in ('authorization_pending', 'slow_down'):
        return None
    if err == 'access_denied':
        raise ApiError('Pairing was declined.')
    raise ApiError('The pairing code expired. Try again.' if err == 'invalid_grant' else (err or 'Pairing failed'))
