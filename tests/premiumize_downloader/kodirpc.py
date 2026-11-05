"""Send one JSON-RPC call to a local Kodi (TCP 9090). python -I kodirpc.py METHOD '{"params":..}'"""
import json, socket, sys
def call(method, params=None, timeout=15):
    s = socket.create_connection(('127.0.0.1', 9090), timeout=timeout)
    s.sendall(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}).encode())
    buf = b''
    dec = json.JSONDecoder()
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
        try:
            text = buf.decode('utf-8')
            pos = 0
            while pos < len(text):
                obj, end = dec.raw_decode(text, pos)
                pos = end
                while pos < len(text) and text[pos] in ' \r\n':
                    pos += 1
                if obj.get('id') == 1:
                    s.close()
                    return obj
        except ValueError:
            continue
    s.close()
if __name__ == '__main__':
    print(json.dumps(call(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else None), indent=1)[:4000])
