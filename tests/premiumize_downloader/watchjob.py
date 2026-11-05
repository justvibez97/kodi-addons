"""Poll the add-on's service and print one line every few seconds.  python -I watchjob.py SECONDS [JOB_ID]"""
import json, os, sys, time, urllib.request
prof = os.path.expandvars(r'%APPDATA%\Kodi\userdata\addon_data\plugin.video.premiumize.downloader')
token = json.load(open(os.path.join(prof, 'service.json')))['token']
secs = float(sys.argv[1]); want = sys.argv[2] if len(sys.argv) > 2 else None
MB = 1048576.0
t0 = time.time(); last = 0
while time.time() - t0 < secs:
    jobs = json.load(urllib.request.urlopen('http://127.0.0.1:52770/api/status?token=' + token, timeout=10))['jobs']
    job = next((j for j in jobs if (want and j['id'] == want) or (not want and j['status'] in ('downloading', 'queued'))), None)
    if job is None:
        job = next((j for j in jobs if want and j['id'] == want), None)
    if job is None:
        print('%5.0fs  no active job' % (time.time() - t0)); break
    if time.time() - last >= 5 or job['status'] not in ('downloading', 'queued'):
        last = time.time()
        have, written = job.get('have', 0), job.get('written', job.get('have', 0))
        print('%5.0fs  %-11s %5.1f%%  written %6.0f MB  in RAM %4.0f MB  net %5.1f MB/s  disk %5.1f MB/s  conns %s  %s' % (
            time.time() - t0, job['status'], have * 100.0 / job['size'], written / MB, (have - written) / MB,
            (job.get('speed') or 0) / MB, (job.get('write_speed') or 0) / MB, job.get('conns'), job.get('note') or job.get('error') or ''),
            flush=True)
    if job['status'] in ('done', 'failed', 'paused'):
        break
    time.sleep(1)
