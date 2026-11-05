# -*- coding: utf-8 -*-
"""Write a playable MJPEG AVI test video (frame counter + clock on a noisy background).

python -I gen_avi.py OUT.avi SECONDS [FPS] [WIDTH] [HEIGHT]
"""
import io, os, random, struct, sys
from PIL import Image, ImageDraw, ImageFont

out, seconds = sys.argv[1], float(sys.argv[2])
fps = int(sys.argv[3]) if len(sys.argv) > 3 else 24
w = int(sys.argv[4]) if len(sys.argv) > 4 else 1280
h = int(sys.argv[5]) if len(sys.argv) > 5 else 720
frames = int(seconds * fps)
random.seed(7)
noise = Image.frombytes('L', (w // 4, h // 4), os.urandom((w // 4) * (h // 4))).resize((w, h)).convert('RGB')
try:
    font = ImageFont.truetype('arialbd.ttf', 120)
    small = ImageFont.truetype('arial.ttf', 48)
except OSError:
    font = small = ImageFont.load_default()

def chunk(fourcc, data):
    pad = b'\0' if len(data) % 2 else b''
    return fourcc + struct.pack('<I', len(data)) + data + pad

with open(out, 'wb') as f:
    f.write(b'\0' * 2048)          # header written at the end
    movi_start = f.tell()
    f.write(b'LIST\0\0\0\0movi')
    index, maxsize = [], 0
    for i in range(frames):
        img = noise.copy()
        d = ImageDraw.Draw(img)
        hue = (i * 7) % 255
        d.rectangle([w // 2 - 420, h // 2 - 150, w // 2 + 420, h // 2 + 150], fill=(hue, 60, 255 - hue))
        t = i / fps
        d.text((w // 2, h // 2 - 40), 'FRAME %05d' % i, font=font, fill='white', anchor='mm')
        d.text((w // 2, h // 2 + 90), '%02d:%02d.%02d  /  %d%%' % (t // 60, t % 60, (t * 100) % 100, i * 100 // frames),
               font=small, fill='white', anchor='mm')
        d.rectangle([0, h - 24, int(w * i / frames), h], fill=(46, 170, 255))
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=88)
        data = buf.getvalue()
        index.append((f.tell() - (movi_start + 8), len(data)))
        f.write(chunk(b'00dc', data))
        maxsize = max(maxsize, len(data))
        if i % (fps * 10) == 0:
            print('frame', i, 'of', frames, len(data) // 1024, 'KB', flush=True)
    movi_end = f.tell()
    f.write(b'idx1' + struct.pack('<I', 16 * len(index)))
    for off, size in index:
        f.write(b'00dc' + struct.pack('<III', 0x10, off, size))
    end = f.tell()
    f.seek(movi_start + 4)
    f.write(struct.pack('<I', movi_end - movi_start - 8))
    avih = struct.pack('<IIIIIIIIII4I', 1000000 // fps, maxsize * fps, 0, 0x10, frames, 0, 1, maxsize, w, h, 0, 0, 0, 0)
    strh = struct.pack('<4s4sIHHIIIIIIIIhhhh', b'vids', b'MJPG', 0, 0, 0, 0, 1, fps, 0, frames, maxsize, 10000, 0, 0, 0, w, h)
    strf = struct.pack('<IiiHH4sIiiII', 40, w, h, 1, 24, b'MJPG', w * h * 3, 0, 0, 0, 0)
    strl = b'LIST' + struct.pack('<I', 4 + len(chunk(b'strh', strh)) + len(chunk(b'strf', strf))) + b'strl' + chunk(b'strh', strh) + chunk(b'strf', strf)
    hdrl_body = b'hdrl' + chunk(b'avih', avih) + strl
    hdrl = b'LIST' + struct.pack('<I', len(hdrl_body)) + hdrl_body
    junk_len = 2048 - 12 - len(hdrl) - 8
    header = b'RIFF' + struct.pack('<I', end - 8) + b'AVI ' + hdrl + b'JUNK' + struct.pack('<I', junk_len) + b'\0' * junk_len
    assert len(header) == 2048
    f.seek(0)
    f.write(header)
print('wrote', out, os.path.getsize(out) // (1024 * 1024), 'MB,', frames, 'frames')
