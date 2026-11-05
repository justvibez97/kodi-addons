# Real-internet throughput check: engine -> NullSink (nothing saved). 
# python -I speedtest.py URL [seconds] [conns...]
import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'plugin.video.premiumize.downloader', 'resources', 'lib', 'pmdl'))
import engine
url = sys.argv[1]; secs = float(sys.argv[2]); conns = [int(c) for c in sys.argv[3:]] or [1, 4, 8, 16]
for n in conns:
    d = engine.Downloader(lambda r: url, engine.NullSink(), connections=n, max_buffer=192 * engine.MB)
    t = threading.Thread(target=d.run); t0 = time.time(); t.start()
    peak = 0
    while t.is_alive() and time.time() - t0 < secs:
        time.sleep(0.5); st = d.stats(); peak = max(peak, st['speed'])
    got = d.received; dt = time.time() - t0; d.stop(); t.join(15)
    print('%2d conn: avg %6.1f MB/s (%5.0f Mbit/s), peak %6.1f MB/s, %5.0f MB in %.1fs, retired=%d %s' % (
        n, got / dt / 1e6 * 1.048576, got / dt * 8 / 1e6, peak / 1e6 * 1.048576, got / 1048576, dt, d.retired, d.error or ''))
