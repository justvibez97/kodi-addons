# -*- coding: utf-8 -*-
"""Background service: download queue, progress bar, local player server."""
import os
import sys
import uuid

import xbmc
import xbmcgui

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'lib'))

from pmdl import common  # noqa: E402
from pmdl.manager import Manager  # noqa: E402
from pmdl.server import LocalServer  # noqa: E402

DOWNLOADS_PATH = common.PLUGIN + '?action=downloads'


def _token():
    path = os.path.join(common.profile_dir(), 'service.json')
    data = common.read_json(path, {})
    if not data.get('token'):
        data['token'] = uuid.uuid4().hex
        common.write_json(path, data)
    return data['token']


class ProgressBar(object):
    """Kodi's small background progress bar (top corner)."""

    def __init__(self):
        self.dlg = None

    def show(self, percent, heading, message):
        if self.dlg is None:
            self.dlg = xbmcgui.DialogProgressBG()
            self.dlg.create(heading, message)
        self.dlg.update(int(max(0, min(100, percent))), heading, message)

    def hide(self):
        if self.dlg is not None:
            try:
                self.dlg.close()
            except Exception:
                pass
            self.dlg = None


def bar_text(jobs):
    active = [j for j in jobs if j['status'] == 'downloading']
    if not active:
        return None
    queued = len([j for j in jobs if j['status'] == 'queued'])
    speed = sum(j.get('speed') or 0 for j in active)
    job = active[0]
    size = job['size'] or 1
    have = job.get('have', 0)
    pct = have * 100.0 / size
    if job.get('phase') in (None, 'starting') and not speed:
        heading = 'Starting download'
        detail = job['name']
    else:
        # Skins show the percentage themselves (from the bar's value).
        heading = 'Downloading  ·  %s' % common.human_speed(speed)
        detail = '%s  ·  %s of %s' % (job['name'], common.human_gb(have), common.human_gb(size))
        if job.get('eta') is not None:
            detail += '  ·  %s left' % common.human_eta(job['eta'])
        if job.get('note') and speed < 1024 * 1024:
            detail = '%s  ·  %s' % (job['name'], job['note'])     # slow or stalled: say why
    extra = len(active) - 1 + queued
    if extra:
        detail += '  (+%d more)' % extra
    return pct, heading, detail


def announce(kind, job, player):
    if kind == 'failed':
        common.notify(job.get('error') or 'Unknown error', 'Download failed: %s' % job['name'], 8000)
        return
    mode = common.setting_int('on_complete', 0)
    if mode == 2:
        return
    if mode == 1 and not player.isPlaying():
        if xbmcgui.Dialog().yesno('Download complete', '%s is ready.[CR]Play it now?' % job['name'],
                                  nolabel='Later', yeslabel='Play'):
            item = xbmcgui.ListItem(job['name'], path=job['dest'])
            parts = (job.get('cloud_path') or '').split('/')
            common.play_meta(item, job['name'], parts[-2] if len(parts) > 1 else None)
            player.play(job['dest'], item)
        return
    common.notify(job['name'], 'Download complete', 5000)


def viewing_downloads():
    path = xbmc.getInfoLabel('Container.FolderPath')
    return path.startswith(DOWNLOADS_PATH) and not xbmc.getCondVisibility(
        'System.HasModalDialog | Window.IsActive(contextmenu) | Window.IsActive(busydialog)')


def main():
    monitor = xbmc.Monitor()
    player = xbmc.Player()
    manager = Manager()
    token = _token()
    server = LocalServer(manager, token, common.setting_int('server_port', 52770))
    common.HOME.setProperty(common.PROP_PORT, str(server.port))
    common.HOME.setProperty(common.PROP_TOKEN, token)
    common.log('service started, local server on port %d' % server.port)

    bar = ProgressBar()
    last_sig = None
    try:
        while not monitor.abortRequested():
            manager.tick()
            jobs = manager.snapshot()

            text = bar_text(jobs)
            show_bar = (text is not None and common.setting_bool('progress_bg') and
                        (common.setting_bool('progress_bg_playback') or not player.isPlayingVideo()))
            if show_bar:
                bar.show(*text)
            else:
                bar.hide()

            while manager.events:
                kind, job = manager.events.pop(0)
                announce(kind, job, player)

            # Refresh the Downloads list only when a job changes state: every
            # refresh re-runs the plugin, and Kodi shows its busy spinner meanwhile.
            # Live numbers are in the progress bar and the progress window.
            sig = tuple((j['id'], j['status']) for j in jobs)
            if sig != last_sig and last_sig is not None and viewing_downloads():
                xbmc.executebuiltin('Container.Refresh')
            last_sig = sig

            if monitor.waitForAbort(1):
                break
    finally:
        bar.hide()
        common.HOME.clearProperty(common.PROP_PORT)
        common.HOME.clearProperty(common.PROP_TOKEN)
        server.stop()           # first: frees threads blocked on player connections
        manager.shutdown()
        common.log('service stopped')


if __name__ == '__main__':
    main()
