"""List a plugin directory through Kodi's JSON-RPC.  python -I lsdir.py PLUGIN_URL"""
import json, sys
sys.path.insert(0, __import__('os').path.dirname(__file__))
from kodirpc import call
r = call('Files.GetDirectory', {'directory': sys.argv[1], 'properties': ['file', 'size']}, timeout=60)
if 'error' in r:
    print('ERROR', r['error']); sys.exit(1)
files = r['result'].get('files') or []
print(len(files), 'items')
for f in files[:60]:
    print(' %-9s | %-80s | %s' % (f['filetype'], f['label'][:80], f['file'].split('?', 1)[-1][:150]))
