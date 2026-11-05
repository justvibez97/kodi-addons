# -*- coding: utf-8 -*-
"""Plugin-side access to the background service's queue and player server."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import xbmc

from . import common


class ServiceDown(Exception):
    pass


def _endpoint(wait=6.0):
    deadline = time.time() + wait
    while True:
        port = common.HOME.getProperty(common.PROP_PORT)
        token = common.HOME.getProperty(common.PROP_TOKEN)
        if port and token:
            return 'http://127.0.0.1:%s' % port, token
        if time.time() > deadline:
            raise ServiceDown('The download service isn\'t running. Restart Kodi and try again.')
        xbmc.sleep(250)


def status(wait=6.0):
    base, token = _endpoint(wait)
    try:
        with urllib.request.urlopen('%s/api/status?token=%s' % (base, token), timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise ServiceDown('The download service didn\'t answer (%s).' % exc)


def jobs_by_id(wait=0.5):
    try:
        return dict((j['id'], j) for j in status(wait)['jobs'])
    except ServiceDown:
        return {}


def cmd(name, **args):
    base, token = _endpoint()
    body = json.dumps({'token': token, 'cmd': name, 'args': args}).encode('utf-8')
    req = urllib.request.Request(base + '/api/cmd', data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode('utf-8'))
        except ValueError:
            data = {'error': 'HTTP %d' % exc.code}
    except (OSError, ValueError) as exc:
        raise ServiceDown('The download service didn\'t answer (%s).' % exc)
    if data.get('error'):
        raise ServiceDown(data['error'])
    return data.get('result')


def stream_url(job_id, name):
    base, _token = _endpoint()
    return '%s/stream/%s/%s' % (base, urllib.parse.quote(job_id, safe=''), urllib.parse.quote(name))
