# -*- coding: utf-8 -*-
"""Release-name parsing.  python -I test_naming.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'plugin.video.premiumize.downloader', 'resources', 'lib', 'pmdl'))
import naming  # noqa: E402

M, E = 'movie', 'episode'
CASES = [
    # (name, folder, expected)
    ('Ran.1985.JAPANESE.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-FGT.mkv', None, (M, 'Ran', 1985)),
    ('Resident.Evil.2026.2160p.iT.WEB-DL.DV.HDR10+.MULTi[Ben The Men].mp4', None, (M, 'Resident Evil', 2026)),
    ('Blade.Runner.2049.2017.2160p.UHD.BluRay.x265.mkv', None, (M, 'Blade Runner 2049', 2017)),
    ('2001.A.Space.Odyssey.1968.1080p.BluRay.x264.mkv', None, (M, '2001 A Space Odyssey', 1968)),
    ('1917.2019.2160p.UHD.BluRay.REMUX.mkv', None, (M, '1917', 2019)),
    ('The Godfather (1972) 1080p BluRay.mkv', None, (M, 'The Godfather', 1972)),
    ('Dune.Part.Two.2024.IMAX.2160p.WEB-DL.mkv', None, (M, 'Dune Part Two', 2024)),
    ('Oppenheimer_2023_1080p_WEBRip.mp4', None, (M, 'Oppenheimer', 2023)),
    ('The.Bear.S03E02.1080p.WEB.h264-ETHEL.mkv', None, (E, 'The Bear', 3, 2)),
    ('Shogun.2024.S01E10.2160p.DSNP.WEB-DL.mkv', None, (E, 'Shogun', 1, 10)),
    ('severance s02e01 720p.mkv', None, (E, 'severance', 2, 1)),
    ('Show.Name.1x05.HDTV.avi', None, (E, 'Show Name', 1, 5)),
    ('Show.S01E01E02.1080p.mkv', None, (E, 'Show', 1, 1)),
    ('abc123.mkv', 'Heat.1995.REMASTERED.2160p.BluRay', (M, 'Heat', 1995)),
    ('movie.mkv', None, (None,)),
    ('Video.1920x1080.mkv', None, (None,)),
    ('Home.Video.2160p.mkv', None, (None,)),
]

fails = 0
for name, folder, want in CASES:
    got = naming.parse(name, folder)
    if want[0] is None:
        ok = got['type'] is None
        shown = '(not recognised)'
    elif want[0] == M:
        ok = (got['type'], got['title'], got['year']) == want
        shown = 'movie "%s" (%s)' % (got['title'], got['year'])
    else:
        ok = (got['type'], got['show'], got['season'], got['episode']) == want
        shown = 'episode "%s" S%02dE%02d' % (got['show'], got['season'] or 0, got['episode'] or 0)
    fails += not ok
    print('  %s %-62s -> %s%s' % ('PASS' if ok else 'FAIL', name[:62], shown,
                                   '' if ok else '   (want %s)' % (want,)))
print('\n%d FAILED' % fails if fails else '\nALL PASSED')
sys.exit(1 if fails else 0)
