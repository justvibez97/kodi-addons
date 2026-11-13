# -*- coding: utf-8 -*-
"""The cloud services (premiumize.me, Real-Debrid) behind one small interface.

Queue ids tell them apart: Real-Debrid ids look like ``rd:<torrent id>:<n>``,
everything else is a premiumize item id.
"""
from . import common, realdebrid
from .api import ApiError, Premiumize

is_rd = realdebrid.is_rd
realdebrid.trace = common.debug     # Settings > Advanced > debug logging shows each Real-Debrid request


class SettingsStore(object):
    """Real-Debrid credentials kept in the add-on's (hidden) settings."""

    def get(self, key):
        return common.setting(key) or ''

    def set(self, key, value):
        common.set_setting(key, value)


RD_KEYS = ('rd_client_id', 'rd_client_secret', 'rd_refresh', 'rd_token', 'rd_expires', 'rd_api_token')


def pm():
    return Premiumize(common.api_token())


def rd():
    return realdebrid.RealDebrid(SettingsStore())


def pm_configured():
    return bool(common.api_token())


def rd_configured():
    return realdebrid.configured(SettingsStore())


def connections_key(any_id):
    """The setting that holds how many connections downloads from this file's service use."""
    return 'rd_connections' if is_rd(any_id) else 'connections'


def connections_for(any_id):
    default = 12 if is_rd(any_id) else 8
    return max(1, min(16, common.setting_int(connections_key(any_id), default)))


def service_name(any_id):
    return 'Real-Debrid' if is_rd(any_id) else 'premiumize'


def resolve(any_id, src_link=None):
    """(direct download URL, most connections the service allows or 0) for a cloud file."""
    if is_rd(any_id):
        client = rd()
        if not src_link:
            torrent_id, n = realdebrid.split_id(any_id)
            links = client.torrent_info(torrent_id).get('links') or []
            if n >= len(links):
                raise ApiError('That file is no longer on Real-Debrid.', 'not_found', exact=True)
            src_link = links[n]
        u = client.unrestrict(src_link)
        try:
            chunks = int(u.get('chunks') or 0)
        except (TypeError, ValueError):
            chunks = 0
        return u['download'], chunks
    return pm().link(any_id), 0
