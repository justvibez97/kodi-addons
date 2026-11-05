# -*- coding: utf-8 -*-
"""Download queue that lives in the service: persistence, scheduling, runners."""
import os
import threading
import time
import traceback
import urllib.request

from . import common, engine, storage
from .api import ApiError, Premiumize

ORDER = {'downloading': 0, 'queued': 1, 'paused': 2, 'failed': 3, 'done': 4}
SPACE_MARGIN = 200 * 1024 * 1024
MAX_AUTO_RETRIES = 6


class Runner(threading.Thread):
    """Runs one job's download in its own thread."""

    def __init__(self, mgr, job_id):
        threading.Thread.__init__(self, name='pmdl-job-%s' % job_id)
        self.daemon = True
        self.mgr = mgr
        self.job_id = job_id
        self.dl = None
        self.reason = None
        self._stop_evt = threading.Event()

    def stop(self, reason):
        self.reason = reason
        self._stop_evt.set()
        dl = self.dl
        if dl is not None:
            dl.stop()

    @property
    def stopping(self):
        return self._stop_evt.is_set()

    def run(self):
        self.mgr._run_job(self)


class StreamSource(object):
    """What the local playback server needs to know about one job."""

    def __init__(self, mgr, job_id):
        self.mgr = mgr
        self.id = job_id
        with mgr.lock:
            job = mgr.jobs[job_id]
            self.size = job['size']
            self.name = job['name']
            self.dest = job['dest']
            self.part = job['part']

    def _job(self):
        return self.mgr.jobs.get(self.id) or {}

    def done(self):
        return self._job().get('status') == 'done' and os.path.exists(self.dest)

    def path(self):
        return self.dest if self.done() else self.part

    def written(self):
        if self.done():
            return self.size
        runner = self.mgr.runners.get(self.id)
        dl = runner.dl if runner else None
        if dl is not None and dl.state in ('running', 'done'):
            return dl.written
        try:
            return min(os.path.getsize(self.part), self.size)
        except OSError:
            return 0

    def downloading(self):
        runner = self.mgr.runners.get(self.id)
        return bool(runner and runner.is_alive() and not runner.stopping)

    def speed(self):
        runner = self.mgr.runners.get(self.id)
        dl = runner.dl if runner else None
        return dl.stats()['speed'] if dl is not None else 0

    def link(self, refresh=False):
        return self.mgr.link(self.id, refresh)


class Manager(object):
    def __init__(self):
        self.lock = threading.RLock()
        self.path = os.path.join(common.profile_dir(), 'jobs.json')
        data = common.read_json(self.path, {})
        self.jobs = dict((j['id'], j) for j in data.get('jobs', []))
        self.runners = {}
        self.events = []            # (kind, job dict) for the service to announce
        self._links = {}
        self._dirty = False
        self._saved_at = 0
        resume = common.setting_bool('resume_on_start')
        for job in self.jobs.values():
            if job['status'] in ('downloading', 'queued'):
                job['status'] = 'queued' if resume else 'paused'
            if job['status'] != 'done':
                job['written'] = self._part_size(job)
        self.save(force=True)

    # ------------------------------------------------------------- persistence

    @staticmethod
    def _part_size(job):
        try:
            return min(os.path.getsize(job['part']), job['size'])
        except OSError:
            return 0

    def save(self, force=False):
        with self.lock:
            if not force and not self._dirty:
                return
            for job_id, runner in self.runners.items():
                dl = runner.dl
                if dl is not None and job_id in self.jobs and dl.state == 'running':
                    self.jobs[job_id]['written'] = dl.written
            data = {'version': 1, 'jobs': list(self.jobs.values())}
            self._dirty = False
            self._saved_at = time.time()
        try:
            common.write_json(self.path, data)
        except OSError as exc:
            common.log('could not save jobs: %s' % exc)

    def _touch(self):
        self._dirty = True

    # ------------------------------------------------------------- queries

    def snapshot(self):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: (
                ORDER.get(j['status'], 9), -(j.get('finished') or 0) if j['status'] == 'done' else j.get('prio', 0)))
            out = []
            for job in jobs:
                item = dict(job)
                item['have'] = job.get('written', 0)
                runner = self.runners.get(job['id'])
                if runner is not None:
                    dl = runner.dl
                    if dl is not None and dl.state in ('running', 'starting'):
                        st = dl.stats()
                        item.update(have=st['have'], speed=st['speed'], eta=st['eta'],
                                    conns=st['connections'], phase=dl.state)
                    else:
                        item['phase'] = 'starting'
                out.append(item)
            return out

    def stream_source(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                return None
        return StreamSource(self, job_id)

    def link(self, job_id, refresh=False):
        cached = self._links.get(job_id)
        if cached and not refresh and time.time() - cached[1] < 1800:
            return cached[0]
        link = Premiumize(common.api_token()).link(job_id)
        self._links[job_id] = (link, time.time())
        return link

    # ------------------------------------------------------------- commands

    def enqueue(self, items, root, start_id=None):
        """Add files. ``items``: dicts with id, name, size, rel (folder parts), subs."""
        added, skipped = [], []
        with self.lock:
            prio = max([j.get('prio', 0) for j in self.jobs.values()] or [0]) + 1
            for it in items:
                job_id = it['id']
                job = self.jobs.get(job_id)
                if job is not None:
                    if job['status'] == 'done' and os.path.exists(job['dest']):
                        skipped.append(job_id)
                        continue
                    if job['status'] == 'done':
                        # Finished earlier but the file is gone: fetch again into today's folder.
                        folder = os.path.join(root, common.safe_rel(it.get('rel') or []))
                        job['dest'] = os.path.join(folder, common.safe_name(it['name']))
                        job['part'] = job['dest'] + '.part'
                        job.update(written=0, sub_paths=[])
                    if job['status'] in ('paused', 'failed', 'done'):
                        job.update(status='queued', error=None, retries=0, retry_at=None)
                        added.append(job_id)
                    else:
                        skipped.append(job_id)
                    continue
                folder = os.path.join(root, common.safe_rel(it.get('rel') or []))
                dest = os.path.join(folder, common.safe_name(it['name']))
                self.jobs[job_id] = {
                    'id': job_id, 'name': it['name'], 'size': int(it['size']),
                    'dest': dest, 'part': dest + '.part', 'status': 'queued', 'written': 0,
                    'error': None, 'added': time.time(), 'finished': None, 'prio': prio,
                    'subs': it.get('subs') or [], 'sub_paths': [], 'retries': 0, 'retry_at': None,
                    'cloud_path': it.get('cloud_path', ''),
                }
                self.jobs[job_id]['written'] = self._part_size(self.jobs[job_id])
                prio += 1
                added.append(job_id)
            self._touch()
        if start_id:
            self.start_now(start_id)
        self.tick()
        return {'added': added, 'skipped': skipped}

    def start_now(self, job_id):
        """Move a job to the front and start it at once, pausing another if needed."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job['status'] == 'done' and os.path.exists(job['dest']):
                return True
            job['prio'] = min([j.get('prio', 0) for j in self.jobs.values()] or [0]) - 1
            current = self.runners.get(job_id)
            if current is not None and current.is_alive():
                if current.stopping and current.reason == 'pause':
                    # Still winding down from a pause: let it re-queue itself and
                    # tick() restarts it once the old thread is gone (one writer per file).
                    current.reason = 'preempt'
                    job['status'] = 'queued'
                    self._touch()
                return True
            job.update(status='queued', error=None, retry_at=None)
            limit = max(1, common.setting_int('max_concurrent', 1))
            busy = [jid for jid, r in self.runners.items() if r.is_alive() and not r.stopping]
            if len(busy) >= limit:
                victim = max(busy, key=lambda jid: self.jobs.get(jid, {}).get('prio', 0))
                self.runners[victim].stop('preempt')
            self._start(job)
            self._touch()
        return True

    def pause(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job['status'] == 'done':
                return
            runner = self.runners.get(job_id)
            if runner is not None:
                runner.stop('pause')
            job['status'] = 'paused'
            job['retry_at'] = None
            self._touch()

    def resume(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None and job['status'] in ('paused', 'failed'):
                job.update(status='queued', error=None, retries=0, retry_at=None)
                self._touch()
        self.tick()

    def pause_all(self):
        for job_id in list(self.jobs):
            if self.jobs[job_id]['status'] in ('queued', 'downloading'):
                self.pause(job_id)

    def resume_all(self):
        for job_id in list(self.jobs):
            if self.jobs[job_id]['status'] in ('paused', 'failed'):
                self.resume(job_id)

    def cancel(self, job_id):
        """Stop and forget a job, deleting its partial file."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            runner = self.runners.get(job_id)
            if runner is not None and runner.is_alive():
                job['status'] = 'paused'
                runner.stop('cancel')       # the runner deletes the part file and the job
                return
            self.jobs.pop(job_id, None)
            self._touch()
        self._remove_file(job['part'])

    def delete_local(self, job_id):
        with self.lock:
            job = self.jobs.pop(job_id, None)
            self._touch()
        if job is None:
            return
        for path in [job['dest'], job['part']] + list(job.get('sub_paths') or []):
            self._remove_file(path)
        self._remove_empty_dirs(os.path.dirname(job['dest']))

    def forget(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None and job['status'] == 'done':
                self.jobs.pop(job_id, None)
                self._touch()

    def clear_finished(self):
        with self.lock:
            for job_id in [j for j, job in self.jobs.items() if job['status'] == 'done']:
                self.jobs.pop(job_id, None)
            self._touch()

    @staticmethod
    def _remove_file(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _remove_empty_dirs(self, folder):
        root = os.path.normpath(common.setting('download_path') or '')
        while folder and os.path.normpath(folder) != root and len(folder) > 3:
            try:
                os.rmdir(folder)        # only succeeds when empty
            except OSError:
                return
            folder = os.path.dirname(folder)

    # ------------------------------------------------------------- scheduling

    def tick(self):
        """Reap finished runners, schedule retries, start queued jobs."""
        now = time.time()
        with self.lock:
            for job_id, runner in list(self.runners.items()):
                if not runner.is_alive():
                    del self.runners[job_id]
            for job in self.jobs.values():
                if job['status'] == 'failed' and job.get('retry_at') and now >= job['retry_at']:
                    job.update(status='queued', retry_at=None)
                    self._touch()
            limit = max(1, common.setting_int('max_concurrent', 1))
            busy = len([r for r in self.runners.values() if not r.stopping])
            waiting = sorted([j for j in self.jobs.values()
                              if j['status'] == 'queued' and j['id'] not in self.runners],
                             key=lambda j: j.get('prio', 0))
            while waiting and busy < limit:
                self._start(waiting.pop(0))
                busy += 1
        if self._dirty or (self.runners and now - self._saved_at > 5):
            self._dirty = True
            self.save()

    def _start(self, job):
        job['status'] = 'downloading'
        job['error'] = None
        runner = Runner(self, job['id'])
        self.runners[job['id']] = runner
        runner.start()

    def active(self):
        with self.lock:
            return any(not r.stopping for r in self.runners.values())

    def shutdown(self, budget=3.0):
        """Stop every download quickly: Kodi kills services that take over ~5 s."""
        deadline = time.time() + budget
        with self.lock:
            runners = list(self.runners.values())
        for runner in runners:
            runner.stop('shutdown')
        for runner in runners:
            runner.join(max(0.05, deadline - time.time()))
        self.save(force=True)

    # ------------------------------------------------------------- the actual work

    def _run_job(self, runner):
        job_id = runner.job_id
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            name, size, dest, part = job['name'], job['size'], job['dest'], job['part']
            subs = list(job.get('subs') or [])
        folder = os.path.dirname(dest)
        try:
            problem = storage.check_writable(folder)
            if problem:
                raise engine.DownloadError(problem, permanent=True)
            limit = storage.max_file_size(folder)
            if limit and size > limit:
                raise engine.DownloadError(
                    'This drive is FAT32, which can\'t store files over 4 GB (this one is %s). '
                    'Reformat it as exFAT or NTFS, or choose another folder.' % common.human_gb(size),
                    permanent=True)
            need = size - self._part_size(job)
            free = storage.free_space(folder)
            if free is not None and free < need + SPACE_MARGIN:
                raise engine.DownloadError('Not enough space: needs %s more, %s free.' % (
                    common.human_gb(need), common.human_gb(free)), permanent=True)
            if subs and common.setting_bool('subtitles'):
                self._fetch_subs(job_id, folder, subs)
            if runner.stopping:
                raise engine.Stopped()

            sink = engine.FileSink(part, resume=True)
            dl = engine.Downloader(
                lambda refresh: self.link(job_id, refresh), sink, size=size,
                connections=max(1, min(16, common.setting_int('connections', 8))),
                max_buffer=max(32, common.setting_int('buffer_mb', 192)) * engine.MB,
                log=lambda m: common.debug('%s: %s' % (name, m)))
            runner.dl = dl
            if runner.stopping:
                dl.stop()
            common.log('downloading %s (%s, %d connections)' % (name, common.human_gb(size), dl.connections))
            t0 = time.time()
            result = dl.run()
            if result == 'done':
                self._finish(job_id, part, dest)
                took = time.time() - t0
                common.log('finished %s in %ds (avg %s)' % (
                    name, took, common.human_speed((size - dl.start_offset) / max(took, 0.001))))
                return
            raise engine.Stopped()
        except engine.Stopped:
            self._stopped(runner)
        except (engine.DownloadError, ApiError) as exc:
            if runner.stopping:
                self._stopped(runner)
                return
            permanent = getattr(exc, 'permanent', False) or getattr(exc, 'code', None) in (
                'authentication_failed', 'not_found', 'no_key', 'permission_denied')
            self._failed(job_id, str(exc), permanent)
        except Exception as exc:
            common.log('job %s crashed: %s' % (name, traceback.format_exc()))
            self._failed(job_id, 'Unexpected error: %s' % exc, True)

    def _fetch_subs(self, job_id, folder, subs):
        paths = []
        for sub in subs:
            target = os.path.join(folder, common.safe_name(sub['target']))
            if not os.path.exists(target):
                try:
                    link = Premiumize(common.api_token()).link(sub['id'])
                    req = urllib.request.Request(link, headers={'User-Agent': engine.USER_AGENT})
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = resp.read(20 * 1024 * 1024)
                    with open(target, 'wb') as f:
                        f.write(data)
                except Exception as exc:
                    common.log('subtitle %s skipped: %s' % (sub.get('name'), exc))
                    continue
            paths.append(target)
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id]['sub_paths'] = paths
                self._touch()

    def _finish(self, job_id, part, dest):
        for attempt in range(20):
            try:
                os.replace(part, dest)
                break
            except OSError as exc:
                # Windows refuses while the file is open for playback; Android doesn't care.
                if attempt == 19:
                    self._failed(job_id, 'Could not rename the finished file: %s' % exc, True)
                    return
                time.sleep(1)
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job.update(status='done', written=job['size'], finished=time.time(), error=None,
                       retry_at=None, retries=0)
            self.events.append(('done', dict(job)))
            self._touch()
        self.save(force=True)

    def _stopped(self, runner):
        job_id = runner.job_id
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job['written'] = self._part_size(job)
            if runner.reason == 'cancel':
                self.jobs.pop(job_id, None)
                self._touch()
                part = job['part']
            else:
                if runner.reason == 'preempt':
                    job['status'] = 'queued'
                elif runner.reason == 'pause':
                    job['status'] = 'paused'
                # 'shutdown': leave as is; startup decides whether to resume.
                self._touch()
                part = None
        if part:
            self._remove_file(part)
        self.save(force=True)

    def _failed(self, job_id, message, permanent):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job['written'] = self._part_size(job)
            job['status'] = 'failed'
            job['error'] = message
            retries = job.get('retries', 0)
            if not permanent and common.setting_bool('auto_retry') and retries < MAX_AUTO_RETRIES:
                job['retries'] = retries + 1
                job['retry_at'] = time.time() + 20 * (2 ** retries)
            else:
                job['retry_at'] = None
                self.events.append(('failed', dict(job)))
            self._touch()
        common.log('failed %s: %s' % (job_id, message))
        self.save(force=True)
