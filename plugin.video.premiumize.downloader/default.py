# -*- coding: utf-8 -*-
"""Plugin entry point: cloud browser, downloads list, playback."""
import os
import sys
import threading
import time
import urllib.parse

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'lib'))

from pmdl import client, common, engine, storage  # noqa: E402
from pmdl.api import ApiError, Premiumize, device_code, poll_device_token  # noqa: E402

HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].lstrip('-').isdigit() else -1
ARGS = dict(urllib.parse.parse_qsl(sys.argv[2][1:])) if len(sys.argv) > 2 and sys.argv[2] else {}
DIALOG = xbmcgui.Dialog()

STATUS_COLOR = {'downloading': 'deepskyblue', 'queued': 'grey', 'paused': 'orange',
                'failed': 'red', 'done': 'springgreen'}
SUBS_FOLDERS = ('subs', 'subtitles', 'sub')


def url(**kw):
    return common.PLUGIN + '?' + urllib.parse.urlencode(dict((k, v) for k, v in kw.items() if v not in (None, '')))


def run(**kw):
    return 'RunPlugin(%s)' % url(**kw)


def play_cmd(**kw):
    return 'PlayMedia(%s)' % url(**kw)


def api():
    return Premiumize(common.api_token())


def set_info(li, title, plot=''):
    try:
        tag = li.getVideoInfoTag()
        tag.setTitle(title)
        tag.setPlot(plot)
        tag.setMediaType('video')
    except AttributeError:      # Kodi 19
        li.setInfo('video', {'title': title, 'plot': plot, 'mediatype': 'video'})


def item(label, icon='DefaultFolder.png', label2='', plot=''):
    li = xbmcgui.ListItem(label, label2, offscreen=True)
    li.setArt({'icon': icon, 'thumb': icon})
    if plot:
        set_info(li, label, plot)
    return li


def add(li, target, folder=True):
    xbmcplugin.addDirectoryItem(HANDLE, target, li, isFolder=folder)


def end(content='files', cache=False):
    xbmcplugin.setContent(HANDLE, content)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=cache)


def fail_listing(message):
    DIALOG.notification('Premiumize Downloader', message, xbmcgui.NOTIFICATION_ERROR, 6000)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


# ======================================================================= root

def root():
    if not common.api_token():
        li = item('[B]Set up:[/B] add your premiumize.me API key', 'DefaultAddonService.png',
                  plot='Open premiumize.me/account on your phone, copy the API key, then paste it '
                       'into this add-on\'s settings.')
        add(li, url(action='settings'), folder=False)
    jobs = client.jobs_by_id(wait=2)
    active = [j for j in jobs.values() if j['status'] == 'downloading']
    summary = ''
    if active:
        speed = sum(j.get('speed') or 0 for j in active)
        summary = '%d active  ·  %s' % (len(active), common.human_speed(speed))
    else:
        waiting = len([j for j in jobs.values() if j['status'] in ('queued', 'paused', 'failed')])
        if waiting:
            summary = '%d waiting' % waiting
    add(item('My cloud', 'DefaultNetwork.png'), url(action='cloud'))
    add(item('Downloads', 'DefaultHardDisk.png', summary), url(action='downloads'))
    add(item('Search my cloud', 'DefaultAddonsSearch.png'), url(action='search'))
    add(item('Settings', 'DefaultAddonProgram.png'), url(action='settings'), folder=False)
    end()


# ======================================================================= cloud

def live_job(job):
    """The job, or None if it finished but its file has since been deleted (treat as not downloaded)."""
    if job and job['status'] == 'done' and not os.path.exists(job['dest']):
        return None
    return job


def job_tag(job):
    if not job:
        return ''
    st = job['status']
    if st == 'done':
        text = 'downloaded'
    elif st == 'downloading':
        text = '%d%%' % (job.get('have', 0) * 100.0 / max(job['size'], 1))
    elif st == 'paused':
        text = 'paused %d%%' % (job.get('have', 0) * 100.0 / max(job['size'], 1))
    else:
        text = st
    return '  [COLOR %s](%s)[/COLOR]' % (STATUS_COLOR.get(st, 'grey'), text)


def file_context(entry, fid, path, job):
    params = dict(id=entry['id'], fid=fid, path=path, name=entry['name'], size=entry.get('size', 0))
    ctx = []
    done = job and job['status'] == 'done' and os.path.exists(job['dest'])
    if not done:
        ctx.append(('Play now (keeps downloading)', run(action='choose', pick='now', **params)))
        ctx.append(('Download', run(action='download', **params)))
    ctx.append(('Stream from cloud (no download)', run(action='choose', pick='stream', **params)))
    if job and job['status'] == 'downloading':
        ctx.append(('Download progress', run(action='progress', id=entry['id'])))
    if done:
        ctx.append(('Delete from this device', run(action='job', cmd='delete_local', id=entry['id'])))
    ctx.append(('Speed test with this file', run(action='speedtest', id=entry['id'], name=entry['name'])))
    ctx.append(('Delete from premiumize cloud', run(action='cloud_delete', kind='file', id=entry['id'],
                                                   name=entry['name'], path=path)))
    ctx.append(('Go to Downloads', 'Container.Update(%s)' % url(action='downloads')))
    return ctx


def list_entries(entries, fid, path, jobs, show_folders=True):
    show_all = common.setting_bool('show_all_files')
    ask = common.setting_int('click_action', 0) == 0
    folders = sorted([e for e in entries if e.get('type') == 'folder'], key=lambda e: e['name'].lower())
    files = sorted([e for e in entries if e.get('type') == 'file'], key=lambda e: e['name'].lower())
    if show_folders:
        for e in folders:
            if not show_all and e['name'].lower() in SUBS_FOLDERS:
                continue        # subtitles come along with their video automatically
            sub_path = (path + '/' + e['name']).strip('/')
            li = item(e['name'], 'DefaultFolder.png')
            li.addContextMenuItems([
                ('Download whole folder', run(action='download_folder', id=e['id'], path=sub_path)),
                ('Delete from premiumize cloud', run(action='cloud_delete', kind='folder', id=e['id'],
                                                     name=e['name'], path=sub_path))])
            add(li, url(action='cloud', id=e['id'], path=sub_path))
    for e in files:
        video = common.is_video(e['name'], e.get('mime_type'))
        if not video and (not show_all or common.is_sub(e['name'])):
            continue
        job = live_job(jobs.get(e['id']))
        size = e.get('size') or 0
        label = e['name'] + job_tag(job)
        li = item(label, 'DefaultVideo.png' if video else 'DefaultFile.png', common.human_size(size))
        # Some skins show the title rather than the label in video lists, so the tag goes in both.
        set_info(li, label, '%s[CR]%s' % (common.human_gb(size), e.get('path') or path or 'My cloud'))
        params = dict(id=e['id'], fid=fid or e.get('folder_id'), path=path, name=e['name'], size=size)
        if video:
            li.addContextMenuItems(file_context(e, params['fid'], path, job))
            if ask and not job:
                # Ask before playback starts. A dialog opened while Kodi is already
                # starting playback can end up invisible (seen with Umbrestuary).
                add(li, url(action='choose', **params), folder=False)
            else:
                li.setProperty('IsPlayable', 'true')
                add(li, url(action='play', **params), folder=False)
        else:
            li.addContextMenuItems([('Download', run(action='download', **params))])
            add(li, url(action='download', **params), folder=False)


def cloud():
    fid = ARGS.get('id')
    path = ARGS.get('path', '')
    try:
        data = api().folder_list(fid)
    except ApiError as exc:
        return fail_listing(str(exc))
    list_entries(data.get('content') or [], fid or data.get('folder_id'), path, client.jobs_by_id())
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL_IGNORE_FOLDERS)
    end('videos')


def _history_path():
    return os.path.join(common.profile_dir(), 'searches.json')


def search():
    """Search screen: "New search..." plus recent searches; with q=, the results."""
    query = ARGS.get('q')
    if not query:
        add(item('[B]New search...[/B]', 'DefaultAddonsSearch.png'), url(action='search_new'), folder=False)
        for q in common.read_json(_history_path(), []):
            li = item(q, 'DefaultAddonsSearch.png')
            li.addContextMenuItems([('Remove from recent searches', run(action='search_forget', q=q))])
            add(li, url(action='search', q=q))
        return end()
    try:
        data = api().folder_search(query)
    except ApiError as exc:
        return fail_listing(str(exc))
    entries = data.get('content') or []
    for e in [e for e in entries if e.get('type') == 'folder']:
        li = item(e['name'], 'DefaultFolder.png')
        li.addContextMenuItems([
            ('Download whole folder', run(action='download_folder', id=e['id'], path=e['name'])),
            ('Delete from premiumize cloud', run(action='cloud_delete', kind='folder', id=e['id'],
                                                 name=e['name'], path=e['name']))])
        add(li, url(action='cloud', id=e['id'], path=e['name']))
    list_entries(entries, None, '', client.jobs_by_id(), show_folders=False)
    if not entries:
        add(item('Nothing found for "%s"' % query, 'DefaultIconInfo.png'), url(action='search'))
    end('videos')


def action_search_new():
    query = DIALOG.input('Search my cloud').strip()
    if not query:
        return
    history = [q for q in common.read_json(_history_path(), []) if q.lower() != query.lower()]
    common.write_json(_history_path(), ([query] + history)[:15])
    xbmc.executebuiltin('Container.Update(%s)' % url(action='search', q=query))


def action_search_forget():
    history = [q for q in common.read_json(_history_path(), []) if q != ARGS.get('q')]
    common.write_json(_history_path(), history)
    xbmc.executebuiltin('Container.Refresh')


# ======================================================================= download helpers

def subtitles_for(video_name, entries, extra_entries=()):
    """Subtitle files that belong to ``video_name``, with the name to save them under."""
    base = os.path.splitext(video_name)[0]
    videos = [e for e in entries if e.get('type') == 'file' and common.is_video(e['name'], e.get('mime_type'))]
    out = []
    for e in list(entries) + list(extra_entries):
        if e.get('type') != 'file' or not common.is_sub(e['name']):
            continue
        sub_base = os.path.splitext(e['name'])[0]
        if sub_base.lower().startswith(base.lower()):
            target = e['name']
        elif len(videos) == 1:
            # "English.srt" next to the only video -> "<video>.English.srt" so Kodi picks it up.
            target = '%s.%s%s' % (base, sub_base, common.ext(e['name']))
        else:
            continue
        out.append({'id': e['id'], 'name': e['name'], 'target': target, 'link': e.get('link')})
    return out


def folder_with_subs(prem, fid):
    """Folder listing plus the content of a Subs/ sub-folder, if any."""
    entries = prem.folder_list(fid).get('content') or [] if fid else []
    extra = []
    for e in entries:
        if e.get('type') == 'folder' and e['name'].lower() in SUBS_FOLDERS:
            try:
                extra += prem.folder_list(e['id']).get('content') or []
            except ApiError:
                pass
    return entries, extra


def build_item(entry, rel_parts, entries, extra=()):
    return {'id': entry['id'], 'name': entry['name'], 'size': int(entry.get('size') or 0),
            'rel': list(rel_parts), 'subs': subtitles_for(entry['name'], entries, extra),
            'cloud_path': '/'.join(list(rel_parts) + [entry['name']])}


def single_item(prem):
    """Build a queue item for the file in ARGS (looks up its folder for subtitles)."""
    fid = ARGS.get('fid')
    path = ARGS.get('path', '')
    entry = {'id': ARGS['id'], 'name': ARGS.get('name'), 'size': int(ARGS.get('size') or 0)}
    entries, extra = [], []
    try:
        entries, extra = folder_with_subs(prem, fid)
        found = [e for e in entries if e.get('id') == entry['id']]
        if found:
            entry = found[0]
    except ApiError as exc:
        common.log('folder lookup failed: %s' % exc)
    if not entry.get('name') or not entry.get('size'):
        details = prem.item_details(entry['id'])
        entry.update(name=details['name'], size=details['size'])
    return build_item(entry, [p for p in path.split('/') if p], entries, extra)


NO_USB = ("Kodi can't see a USB drive.[CR]If the Shield's Storage settings list your drive under "
          '"Device storage", it is set up as device storage, which Android keeps private to the system. '
          'Select the drive there and format it as removable storage (this erases it), then come back here. '
          'Kodi also needs "Allow management of all files" (Settings > Apps > Kodi > Permissions > Files and media).')


def choose_root(reason=''):
    """Ask where downloads go; saves and returns the folder (or None)."""
    if reason:
        DIALOG.ok('Download folder', reason)
    options, paths = [], []
    if storage.is_android():
        vols = storage.volumes()
        if not any(label.startswith('USB') for label, _vol in vols):
            DIALOG.ok('Download folder', NO_USB)
        for label, vol in vols:
            options.append('%s  ·  %s' % (label, storage.describe(vol)))
            paths.append(os.path.join(vol, 'Premiumize'))
    options += ['Enter a folder path...', 'Browse for a folder...']
    paths += ['<enter>', None]
    choice = DIALOG.select('Where should downloads go?', options)
    if choice < 0:
        return None
    folder = paths[choice]
    if folder == '<enter>':
        folder = DIALOG.input('Folder path (e.g. /storage/1A2B-3C4D/Premiumize)',
                              common.setting('download_path') or '/storage/').strip()
        if not folder:
            return None
    elif folder is None:
        picked = DIALOG.browse(3, 'Choose the download folder', 'files', '', False, False,
                               common.setting('download_path'))
        if not picked:
            return None
        folder = xbmcvfs.translatePath(picked)
    if '://' in folder:
        DIALOG.ok('Download folder', "Pick a drive attached to this device (network shares aren't "
                                     'supported for downloads).')
        return choose_root()
    problem = storage.check_writable(folder)
    if problem and folder.startswith('/storage/') and not folder.startswith('/storage/emulated'):
        vol = '/'.join(folder.split('/')[:3])
        fallback = vol + '/Android/data/org.xbmc.kodi/files/Premiumize'
        if storage.check_writable(fallback) is None and DIALOG.yesno(
                'Download folder', problem + '[CR][CR]Or use a folder Kodi can always write to on this '
                'drive? (Android deletes it if you uninstall Kodi.)', nolabel='Cancel', yeslabel='Use it'):
            folder, problem = fallback, None
    if problem:
        DIALOG.ok('Download folder', problem)
        return None
    common.set_setting('download_path', folder)
    common.notify('Downloads will go to %s' % folder)
    return folder


def ensure_root():
    folder = common.setting('download_path')
    if not folder:
        return choose_root()
    folder = xbmcvfs.translatePath(folder)
    problem = storage.check_writable(folder)
    if problem:
        return choose_root(problem + '[CR][CR]Choose another folder.')
    return folder


def confirm_space(folder, items):
    need = sum(i['size'] for i in items)
    free = storage.free_space(folder)
    if free is not None and need > free:
        DIALOG.ok('Not enough space', 'These need %s but %s has %s free.' % (
            common.human_gb(need), folder, common.human_gb(free)))
        return False
    limit = storage.max_file_size(folder)
    if limit and any(i['size'] > limit for i in items):
        return DIALOG.yesno('FAT32 drive', 'This drive is FAT32, which can\'t store files over 4 GB, '
                            'and some of these are bigger. Those will fail. Continue anyway?')
    return True


def enqueue(items, start_id=None, quiet=False):
    folder = ensure_root()
    if not folder or not confirm_space(folder, items):
        return None
    try:
        result = client.cmd('enqueue', items=items, root=folder, start_id=start_id)
    except client.ServiceDown as exc:
        DIALOG.ok('Premiumize Downloader', str(exc))
        return None
    if not quiet:
        added, skipped = len(result['added']), len(result['skipped'])
        if added:
            common.notify('%d added to downloads' % added if added > 1 else 'Added: %s' % items[0]['name'])
        elif skipped:
            common.notify('Already downloaded or in the queue')
    return result


# ======================================================================= actions

def action_download():
    try:
        prem = api()
        it = single_item(prem)
    except ApiError as exc:
        return DIALOG.ok('Premiumize Downloader', str(exc))
    enqueue([it])


def action_download_folder():
    prem = api()
    root_path = ARGS.get('path', '')
    found = []
    progress = xbmcgui.DialogProgress()
    progress.create('Download folder', 'Looking through %s...' % (root_path or 'the folder'))

    def walk(fid, rel):
        if progress.iscanceled():
            return
        entries, extra = folder_with_subs(prem, fid)
        progress.update(0, 'Looking through %s...[CR]%d videos so far' % ('/'.join(rel), len(found)))
        for e in entries:
            if e.get('type') == 'file' and common.is_video(e['name'], e.get('mime_type')):
                found.append(build_item(e, rel, entries, extra))
        for e in entries:
            if e.get('type') == 'folder' and e['name'].lower() not in SUBS_FOLDERS:
                walk(e['id'], rel + [e['name']])

    try:
        walk(ARGS['id'], [p for p in root_path.split('/') if p])
    except ApiError as exc:
        progress.close()
        return DIALOG.ok('Premiumize Downloader', str(exc))
    canceled = progress.iscanceled()
    progress.close()
    if canceled:
        return
    if not found:
        return DIALOG.ok('Download folder', 'No videos in this folder.')
    total = sum(i['size'] for i in found)
    folder = ensure_root()
    if not folder:
        return
    if not DIALOG.yesno('Download folder', 'Download %d videos (%s)?[CR]To %s (%s)' % (
            len(found), common.human_gb(total), folder, storage.describe(folder))):
        return
    enqueue(found)


def folder_of(job=None):
    """The cloud folder name of the file being played (helps read odd file names)."""
    if job and job.get('cloud_path'):
        parts = job['cloud_path'].split('/')
        return parts[-2] if len(parts) > 1 else None
    path = ARGS.get('path') or ''
    return path.rstrip('/').split('/')[-1] or None


def resolve(path, name, mime=None, subs=None, folder=None):
    li = xbmcgui.ListItem(name, path=path, offscreen=True)
    common.play_meta(li, name, folder)
    if mime:
        li.setMimeType(mime)
        li.setContentLookup(False)
    if subs:
        li.setSubtitles(subs)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def cancel_resolve():
    xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem(offscreen=True))


def action_play():
    """Click on a cloud video: local copy if we have one, otherwise ask/play-now/stream."""
    file_id = ARGS['id']
    name = ARGS.get('name') or file_id
    mode = ARGS.get('mode', 'auto')
    job = live_job(client.jobs_by_id(wait=3).get(file_id))

    if job and job['status'] == 'done' and os.path.exists(job['dest']) and mode != 'stream':
        return resolve(job['dest'], name, folder=folder_of(job))

    if mode == 'auto':
        if job:                       # already queued or downloading: just watch it
            mode = 'now'
        else:
            choice = common.setting_int('click_action', 0)
            if choice == 0:
                # Normally answered in action_choose before playback starts; this
                # path is only reached from favourites or widgets.
                picked = DIALOG.select(name, CHOICES)
                if picked < 0:
                    return cancel_resolve()
                choice = picked + 1
            mode = {1: 'now', 2: 'download', 3: 'stream'}[choice]

    try:
        prem = api()
        if mode == 'stream':
            link = prem.link(file_id)
            return resolve(link, name, common.mime_for(name), folder=folder_of())
        it = single_item(prem)
    except ApiError as exc:
        DIALOG.ok('Premiumize Downloader', str(exc))
        return cancel_resolve()

    if mode == 'download':
        enqueue([it])
        return cancel_resolve()

    if job and job['status'] != 'done':
        client.cmd('start_now', id=file_id)          # already queued: just bring it forward
    elif enqueue([it], start_id=file_id, quiet=True) is None:
        return cancel_resolve()
    local_subs = [p for p in (job or {}).get('sub_paths') or [] if os.path.exists(p)]
    subs = local_subs or [s['link'] for s in it['subs'] if s.get('link')]
    resolve(client.stream_url(file_id, it['name']), it['name'], common.mime_for(it['name']), subs,
            folder=folder_of())


CHOICES = ['Play now (keeps downloading in the background)', 'Download only', 'Stream from cloud (no download)']


def action_choose():
    """Select on a cloud video that isn't downloaded: ask first, then start playback."""
    name = ARGS.get('name') or ARGS['id']
    params = dict((k, ARGS[k]) for k in ('id', 'fid', 'path', 'name', 'size') if ARGS.get(k))
    pick = ARGS.get('pick')
    if not pick:
        picked = DIALOG.select(name, CHOICES)
        if picked < 0:
            return
        pick = ('now', 'download', 'stream')[picked]
    if pick == 'download':
        return action_download()
    if pick == 'stream':
        return xbmc.executebuiltin('PlayMedia(%s)' % url(action='play', mode='stream', **params))
    try:
        it = single_item(api())
    except ApiError as exc:
        return DIALOG.ok('Premiumize Downloader', str(exc))
    # Drive picker and space checks happen here, before playback starts.
    if enqueue([it], start_id=it['id'], quiet=True) is None:
        return
    xbmc.executebuiltin('PlayMedia(%s)' % url(action='play', **params))


def action_playjob():
    """Play a queued/downloading job from the Downloads list."""
    job = client.jobs_by_id(wait=3).get(ARGS['id'])
    if not job:
        return cancel_resolve()
    if job['status'] == 'done':
        if os.path.exists(job['dest']):
            return resolve(job['dest'], job['name'], folder=folder_of(job))
        # Never start a whole new download behind the viewer's back.
        DIALOG.ok(short(job['name']), 'This file has been deleted from the drive. Pick it in My cloud '
                                      'to download or stream it again.')
        return cancel_resolve()
    client.cmd('start_now', id=job['id'])
    resolve(client.stream_url(job['id'], job['name']), job['name'], common.mime_for(job['name']),
            [p for p in job.get('sub_paths') or [] if os.path.exists(p)], folder=folder_of(job))


# ======================================================================= downloads list

def job_label(job):
    st = job['status']
    size = job['size'] or 1
    pct = job.get('have', 0) * 100.0 / size
    color = STATUS_COLOR.get(st, 'grey')
    if st == 'downloading':
        # The list is a snapshot (it refreshes when a job changes state), so no
        # live speed here: the progress bar and progress window show that.
        tag = 'downloading %d%%' % pct if job.get('speed') or pct else 'starting'
        right = common.human_gb(job['size'])
    elif st == 'queued':
        tag, right = 'queued', common.human_gb(job['size'])
    elif st == 'paused':
        tag, right = 'paused %d%%' % pct, common.human_gb(job['size'])
    elif st == 'failed':
        tag = 'retrying soon' if job.get('retry_at') else 'failed'
        right = common.human_gb(job['size'])
    else:
        return job['name'], common.human_gb(job['size'])
    return '[COLOR %s][%s][/COLOR]  %s' % (color, tag, job['name']), right


def short(name, n=46):
    """One line in a dialog: long release names would push the status lines out."""
    return name if len(name) <= n else name[:n - 3].rstrip('.') + '...'


def conns_text(job):
    n = job.get('conns') or 0
    text = '%d connection%s' % (n, '' if n == 1 else 's')
    if job.get('retrying'):
        text += ' (%d retrying)' % job['retrying']
    return text


def job_plot(job):
    lines = [job['name'], '%s of %s' % (common.human_gb(job.get('have', 0)), common.human_gb(job['size']))]
    if job['status'] == 'downloading' and job.get('speed'):
        lines.append('%s with %s' % (common.human_speed(job['speed']), conns_text(job)))
    if job.get('note'):
        lines.append('[COLOR orange]%s[/COLOR]' % job['note'])
    if job.get('error'):
        lines.append('[COLOR red]%s[/COLOR]' % job['error'])
    lines.append(job['dest'])
    return '[CR]'.join(lines)


def downloads():
    try:
        jobs = client.status()['jobs']
    except client.ServiceDown as exc:
        return fail_listing(str(exc))
    active = [j for j in jobs if j['status'] == 'downloading']
    if active:
        add(item('[B]Show download progress[/B]', 'DefaultAddonService.png'), url(action='progress'), folder=False)
    common_ctx = []
    if any(j['status'] in ('queued', 'downloading') for j in jobs):
        common_ctx.append(('Pause all', run(action='job', cmd='pause_all')))
    if any(j['status'] in ('paused', 'failed') for j in jobs):
        common_ctx.append(('Resume all', run(action='job', cmd='resume_all')))
    if not jobs:
        add(item('Nothing here yet. Pick a video in My cloud to download it.', 'DefaultIconInfo.png'),
            url(action='cloud'))
    for job in jobs:
        label, right = job_label(job)
        st = job['status']
        jid = job['id']
        if st == 'done':
            exists = os.path.exists(job['dest'])
            li = item(label if exists else '[COLOR grey]%s (file missing)[/COLOR]' % job['name'],
                      'DefaultVideo.png', right, job_plot(job))
            ctx = [('Delete from this device', run(action='job', cmd='delete_local', id=jid, confirm=1)),
                   ('Remove from list (keep file)', run(action='job', cmd='forget', id=jid)),
                   ('Clear finished from list', run(action='job', cmd='clear_finished'))]
            li.addContextMenuItems(ctx + common_ctx)
            if exists:
                # Resolved through the add-on so playback gets a clean title for Trakt.
                li.setProperty('IsPlayable', 'true')
                add(li, url(action='playjob', id=jid), folder=False)
            else:
                add(li, url(action='job', cmd='forget', id=jid), folder=False)
            continue
        li = item(label, 'DefaultVideo.png', right, job_plot(job))
        ctx = []
        if st == 'downloading':
            ctx += [('Show progress', run(action='progress', id=jid)),
                    ('Play now', play_cmd(action='playjob', id=jid)),
                    ('Pause', run(action='job', cmd='pause', id=jid))]
        elif st == 'queued':
            ctx += [('Start now', run(action='job', cmd='start_now', id=jid)),
                    ('Play now', play_cmd(action='playjob', id=jid)),
                    ('Pause', run(action='job', cmd='pause', id=jid))]
        else:
            ctx += [('Resume' if st == 'paused' else 'Retry', run(action='job', cmd='resume', id=jid)),
                    ('Play now', play_cmd(action='playjob', id=jid))]
        ctx.append(('Cancel and delete partial file', run(action='job', cmd='cancel', id=jid, confirm=1)))
        li.addContextMenuItems(ctx + common_ctx)
        add(li, url(action='jobmenu', id=jid), folder=False)
    end('files')


def action_jobmenu():
    """Click on an unfinished job: the most useful actions in one menu."""
    job = client.jobs_by_id(wait=3).get(ARGS['id'])
    if not job:
        return
    st = job['status']
    options = []
    if st == 'downloading':
        options += [('Show progress', 'progress'), ('Play now', 'play'), ('Pause', 'pause')]
    elif st == 'queued':
        options += [('Start now', 'start_now'), ('Play now', 'play'), ('Pause', 'pause')]
    else:
        if job.get('error'):
            DIALOG.ok(job['name'], job['error'])
        options += [('Resume' if st == 'paused' else 'Retry now', 'resume'), ('Play now', 'play')]
    options.append(('Cancel and delete partial file', 'cancel'))
    picked = DIALOG.select(job['name'], [o[0] for o in options])
    if picked < 0:
        return
    what = options[picked][1]
    if what == 'progress':
        return progress_window(job['id'])
    if what == 'play':
        return xbmc.executebuiltin(play_cmd(action='playjob', id=job['id']))
    ARGS.update(cmd=what, id=job['id'], confirm='1')
    action_job()


def action_job():
    cmd = ARGS['cmd']
    job_id = ARGS.get('id')
    if ARGS.get('confirm') and cmd in ('cancel', 'delete_local'):
        job = client.jobs_by_id(wait=3).get(job_id) or {}
        question = ('Stop this download and delete the partial file?' if cmd == 'cancel'
                    else 'Delete this file from the device?')
        if not DIALOG.yesno(job.get('name', 'Premiumize Downloader'), question):
            return
    try:
        if job_id:
            client.cmd(cmd, id=job_id)
        else:
            client.cmd(cmd)
    except client.ServiceDown as exc:
        return DIALOG.ok('Premiumize Downloader', str(exc))
    xbmc.executebuiltin('Container.Refresh')


# ======================================================================= progress window

def progress_window(job_id=None):
    dlg = xbmcgui.DialogProgress()
    dlg.create('Download progress', 'Connecting...')
    monitor = xbmc.Monitor()
    finished_at = None
    try:
        while not dlg.iscanceled() and not monitor.abortRequested():
            try:
                jobs = client.status(wait=2)['jobs']
            except client.ServiceDown as exc:
                dlg.update(0, str(exc))
                xbmc.sleep(1000)
                continue
            job = None
            if job_id:
                job = next((j for j in jobs if j['id'] == job_id), None)
            if job is None or (job['status'] != 'downloading' and not job_id):
                job = next((j for j in jobs if j['status'] == 'downloading'), job)
            if job is None:
                dlg.update(100, 'Nothing is downloading right now.')
                xbmc.sleep(1500)
                break
            size = job['size'] or 1
            have = job.get('have', 0)
            pct = int(have * 100.0 / size)
            queued = len([j for j in jobs if j['status'] == 'queued'])
            st = job['status']
            if st == 'done':
                lines = [short(job['name']), '[COLOR springgreen]Finished[/COLOR]  ·  %s' % common.human_gb(size),
                         job['dest']]
                pct = 100
                finished_at = finished_at or time.time()
            elif st == 'downloading':
                speed = job.get('speed') or 0
                lines = [short(job['name']),
                         '%s of %s  ·  %d%%' % (common.human_gb(have), common.human_gb(size), pct),
                         ('%s  ·  %s  ·  %s left' % (common.human_speed(speed), conns_text(job),
                                                    common.human_eta(job.get('eta'))))
                         if speed else 'Starting...  ·  %s' % conns_text(job)]
            else:
                lines = [short(job['name']), '%s  ·  %d%%' % (st.capitalize(), pct), job.get('error') or '']
            if queued and len(lines) >= 3:
                lines[2] += '  ·  %d more waiting' % queued
            lines = [l for l in lines if l][:3]
            if st == 'downloading' and job.get('note'):
                lines.append('[COLOR orange]%s[/COLOR]' % job['note'])     # why it's slow or waiting
            elif st == 'downloading':
                # What a photo of this window needs to show when something is off.
                lines.append('[COLOR grey]Drive %.0f MB/s  ·  %d MB waiting to save  ·  %d reconnect%s[/COLOR]' % (
                    (job.get('write_speed') or 0) / 1048576.0, (job.get('buffered') or 0) // 1048576,
                    job.get('reconnects') or 0, '' if job.get('reconnects') == 1 else 's'))
            else:
                lines.append('[COLOR grey]Cancel only closes this window. The download keeps going.[/COLOR]')
            dlg.update(pct, '[CR]'.join(l for l in lines if l))
            if finished_at and time.time() - finished_at > 3:
                break
            xbmc.sleep(500)
    finally:
        dlg.close()


# ======================================================================= settings actions

def action_account():
    try:
        info = api().account_info()
    except ApiError as exc:
        return DIALOG.ok('premiumize.me account', str(exc))
    until = info.get('premium_until')
    premium = time.strftime('%d %b %Y', time.localtime(until)) if until else 'not premium'
    used = info.get('limit_used')
    lines = ['Premium until: %s' % premium]
    if used is not None:
        lines.append('Fair use used: %d%%' % round(float(used) * 100))
    DIALOG.ok('premiumize.me account', '[CR]'.join(lines))


def action_pair():
    client_id = common.setting('oauth_client_id').strip()
    if not client_id:
        return DIALOG.ok('Pair this device', 'Enter an OAuth client ID first (Settings > Account > Advanced).')
    try:
        code = device_code(client_id)
    except ApiError as exc:
        return DIALOG.ok('Pair this device', str(exc))
    dlg = xbmcgui.DialogProgress()
    uri = code.get('verification_uri') or 'https://www.premiumize.me/device'
    dlg.create('Pair this device', 'On your phone, open[CR][B]%s[/B][CR]and enter [B]%s[/B]' % (uri, code['user_code']))
    expires = time.time() + int(code.get('expires_in') or 600)
    interval = int(code.get('interval') or 5)
    token = None
    try:
        while time.time() < expires and not dlg.iscanceled():
            left = max(0, expires - time.time())
            dlg.update(int(100 - left * 100 / 600), 'On your phone, open[CR][B]%s[/B][CR]and enter [B]%s[/B]' % (
                uri, code['user_code']))
            xbmc.sleep(interval * 1000)
            try:
                token = poll_device_token(client_id, code['device_code'])
            except ApiError as exc:
                return DIALOG.ok('Pair this device', str(exc))
            if token:
                break
    finally:
        dlg.close()
    if token:
        common.set_setting('oauth_token', token)
        common.notify('Paired with premiumize.me')


def action_unpair():
    common.set_setting('oauth_token', '')
    common.notify('Pairing removed')


def speedtest_advice(results, current):
    """Pick a connection count from [(connections, bytes/s)] and explain it."""
    top = max(sp for _n, sp in results)
    speeds = dict(results)
    # Keep at least 4 connections: with fewer, nothing can take over from one
    # that slows down. Among those within 5% of the fastest, prefer the current
    # setting, then the smallest.
    good = [n for n, sp in results if n >= 4 and sp >= top * 0.95]
    if not good:
        good = [max(results, key=lambda r: r[1])[0]]
    pick = current if current in good else min(good)
    if speeds.get(1, 0) >= top * 0.95:
        verdict = ("Even 1 connection reaches %d Mbit/s, so that is your line's limit right now."
                   % (top * 8 / 1e6))
    else:
        verdict = 'Fastest: %d connections, %d Mbit/s.' % (pick, speeds.get(pick, top) * 8 / 1e6)
    return pick, verdict


def drive_verdict(avg, pause):
    """Plain-language reading of a drive test (average bytes/s, longest pause in s)."""
    mbs = avg / 1048576.0
    if mbs < 15 or pause > 10:
        return ('This drive is too slow for fast downloads: downloads can only go as fast as it saves '
                '(about %.0f MB/s). A USB 3 SSD or hard drive saves 100+ MB/s.' % mbs)
    if mbs < 35:
        return ('This drive keeps up with about %.0f MB/s (%d Mbit/s). Faster downloads would wait for it.'
                % (mbs, mbs * 8.39))
    return 'This drive is fast enough for your line.'


def action_drivetest():
    """Write a test file to the download folder's drive and report its real speed."""
    folder = ensure_root()
    if not folder:
        return
    resume_after = False
    try:
        jobs = client.status(wait=2)['jobs']
        if any(j['status'] == 'downloading' for j in jobs):
            if not DIALOG.yesno('Test drive speed', 'Pause downloads during the test so they don\'t '
                                'skew the result? They resume afterwards.'):
                return
            client.cmd('pause_all')
            resume_after = True
    except client.ServiceDown:
        pass
    test = storage.WriteTest(folder)
    test.start()
    dlg = xbmcgui.DialogProgress()
    dlg.create('Test drive speed', 'Writing a test file to %s...' % folder)
    try:
        while not test.finished:
            elapsed, total, avg, now, pause = test.stats()
            stopping = dlg.iscanceled() or test.stop_flag
            if stopping:
                test.stop_flag = True
            line2 = ('Waiting for the drive to finish its last write...' if stopping else
                     'Now %.1f MB/s  ·  average %.1f MB/s' % (now / 1048576.0, avg / 1048576.0))
            dlg.update(int(min(100, max(elapsed / test.seconds, total / float(test.limit)) * 100)),
                       '%s written in %ds[CR]%s[CR]Longest pause so far: %ds' % (
                           common.human_size(total), elapsed, line2, pause))
            xbmc.sleep(500)
    finally:
        dlg.close()
        if resume_after:
            try:
                client.cmd('resume_all')
            except client.ServiceDown:
                pass
    if test.error is not None:
        return DIALOG.ok('Test drive speed', storage.check_writable(folder) or 'The test failed: %s' % test.error)
    elapsed, total, avg, _now, pause = test.stats()
    DIALOG.ok('Test drive speed', 'Saved %s in %ds: average %.1f MB/s, longest pause %ds.[CR]%s' % (
        common.human_size(total), elapsed, avg / 1048576.0, pause, drive_verdict(avg, pause)))


def action_cloud_delete():
    """Delete a file or folder from the premiumize cloud (asks first; local copies are kept)."""
    kind, item_id, name = ARGS.get('kind'), ARGS['id'], ARGS.get('name', '')
    path = (ARGS.get('path') or '').strip('/')
    jobs = client.jobs_by_id(wait=2)
    if kind == 'folder':
        prefix = path + '/' if path else None
        active = [j for j in jobs.values() if j['status'] != 'done' and prefix
                  and (j.get('cloud_path') or '').startswith(prefix)]
        what = 'the folder "%s" and everything in it' % short(name)
    else:
        active = [j for j in jobs.values() if j['id'] == item_id and j['status'] != 'done']
        what = '"%s"' % short(name)
    lines = ['Permanently delete %s from your premiumize cloud?' % what,
             'Copies already downloaded to this device are kept.']
    if active:
        lines.append('[COLOR orange]%d unfinished download%s from it will be cancelled.[/COLOR]' % (
            len(active), '' if len(active) == 1 else 's'))
    if not DIALOG.yesno('Delete from premiumize cloud', '[CR]'.join(lines), nolabel='Keep', yeslabel='Delete'):
        return
    for job in active:
        try:
            client.cmd('cancel', id=job['id'])
        except client.ServiceDown:
            pass
    try:
        if kind == 'folder':
            api().delete_folder(item_id)
        else:
            api().delete_item(item_id)
    except ApiError as exc:
        return DIALOG.ok('Delete from premiumize cloud', str(exc))
    common.notify('Deleted from your cloud: %s' % short(name))
    xbmc.executebuiltin('Container.Refresh')


def action_speedtest():
    """Measure this file's download speed with different connection counts (nothing is saved)."""
    file_id = ARGS['id']
    try:
        link = api().link(file_id)
    except ApiError as exc:
        return DIALOG.ok('Speed test', str(exc))
    counts = [1, 4, 8, 12, 16]
    seconds = 10
    # A running download shares the line with the test and skews every number.
    resume_after = False
    try:
        if any(j['status'] == 'downloading' for j in client.status(wait=2)['jobs']):
            if not DIALOG.yesno('Speed test', 'Pause downloads during the test so they don\'t '
                                'skew the result? They resume afterwards.'):
                return
            client.cmd('pause_all')
            resume_after = True
            xbmc.sleep(2000)            # let their connections close first
    except client.ServiceDown:
        pass
    dlg = xbmcgui.DialogProgress()
    dlg.create('Speed test', 'Testing %s...' % ARGS.get('name', ''))
    results = []
    try:
        for i, n in enumerate(counts):
            if dlg.iscanceled():
                break
            d = engine.Downloader(lambda refresh: link, engine.NullSink(), connections=n,
                                  max_buffer=max(32, common.setting_int('buffer_mb', 192)) * engine.MB)
            t = threading.Thread(target=d.run)
            t.daemon = True
            t.start()
            t0 = time.time()
            while t.is_alive() and time.time() - t0 < seconds and not dlg.iscanceled():
                xbmc.sleep(500)
                st = d.stats()
                done = (i + (time.time() - t0) / seconds) / len(counts)
                so_far = '   '.join('%d: %s' % (c, common.human_speed(sp)) for c, sp in results)
                dlg.update(int(done * 100), 'Now testing %d connection%s: %s[CR]%s' % (
                    n, 's' if n > 1 else '', common.human_speed(st['speed']), so_far))
            elapsed = time.time() - t0
            got = d.received
            d.stop()
            t.join(10)
            # Ignore the first second (connection setup) by using the moving speed when available.
            results.append((n, d.stats()['speed'] or got / max(elapsed, 0.1)))
    finally:
        dlg.close()
        if resume_after:
            try:
                client.cmd('resume_all')
            except client.ServiceDown:
                pass
    if not results:
        return
    current = common.setting_int('connections', 8)
    summary = 'MB/s by connections:  ' + '   '.join('%d: %.1f' % (n, sp / 1048576.0) for n, sp in results)
    pick, verdict = speedtest_advice(results, current)
    if pick == current:
        DIALOG.ok('Speed test', '%s[CR]%s[CR]Keeping %d connections.' % (summary, verdict, current))
    elif DIALOG.yesno('Speed test', '%s[CR]%s[CR]Switch from %d to %d connections?' % (summary, verdict, current, pick)):
        common.set_setting('connections', str(pick))


# ======================================================================= router

def main():
    action = ARGS.get('action')
    try:
        if action is None:
            root()
        elif action == 'cloud':
            cloud()
        elif action == 'search':
            search()
        elif action == 'search_new':
            action_search_new()
        elif action == 'search_forget':
            action_search_forget()
        elif action == 'downloads':
            downloads()
        elif action == 'play':
            action_play()
        elif action == 'choose':
            action_choose()
        elif action == 'playjob':
            action_playjob()
        elif action == 'download':
            action_download()
        elif action == 'download_folder':
            action_download_folder()
        elif action == 'jobmenu':
            action_jobmenu()
        elif action == 'job':
            action_job()
        elif action == 'progress':
            progress_window(ARGS.get('id'))
        elif action == 'settings':
            common.addon().openSettings()
        elif action == 'choose_folder':
            choose_root()
        elif action == 'account':
            action_account()
        elif action == 'pair':
            action_pair()
        elif action == 'unpair':
            action_unpair()
        elif action == 'cloud_delete':
            action_cloud_delete()
        elif action == 'speedtest':
            action_speedtest()
        elif action == 'drivetest':
            action_drivetest()
    except client.ServiceDown as exc:
        DIALOG.ok('Premiumize Downloader', str(exc))
        if action in ('play', 'playjob'):
            cancel_resolve()
    except ApiError as exc:
        DIALOG.ok('Premiumize Downloader', str(exc))
        if action in ('play', 'playjob'):
            cancel_resolve()


if __name__ == '__main__':
    main()
