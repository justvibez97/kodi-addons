# -*- coding: utf-8 -*-
"""Turn release names into a title Kodi (and the Trakt add-on) can recognise.

"Ran.1985.JAPANESE.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-FGT.mkv"
    -> movie "Ran" (1985)
"The.Bear.S03E02.1080p.WEB.h264-ETHEL.mkv"
    -> episode of "The Bear", season 3, episode 2

No Kodi imports. A name that can't be read returns type None, and the
caller keeps the plain file name.
"""
import datetime
import os
import re

VIDEO_EXT = ('.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.ts', '.m2ts', '.mts', '.mpg', '.mpeg',
             '.webm', '.flv', '.vob', '.divx', '.ogv', '.3gp', '.iso')

# Words that only appear after the title in release names: once one shows up,
# the title is over.
_TAGS = {
    '2160p', '1080p', '1080i', '720p', '576p', '480p', '4k', 'uhd', 'hdr', 'hdr10', 'hdr10+', 'dv', 'dovi',
    'sdr', 'bluray', 'blu-ray', 'bdrip', 'brrip', 'bdremux', 'remux', 'web', 'web-dl', 'webdl', 'webrip',
    'hdtv', 'dvdrip', 'dvd', 'hdrip', 'x264', 'x265', 'h264', 'h265', 'h.264', 'h.265', 'hevc', 'avc',
    'xvid', 'av1', 'vp9', '10bit', '8bit', 'dts', 'dts-hd', 'dts-x', 'truehd', 'atmos', 'ddp', 'dd', 'ac3',
    'eac3', 'aac', 'flac', 'opus', 'proper', 'repack', 'extended', 'unrated', 'uncut', 'remastered',
    'imax', 'criterion', 'directors', 'limited', 'internal', 'multi', 'dual', 'subbed', 'dubbed',
    'amzn', 'nf', 'dsnp', 'hmax', 'atvp', 'it', 'hulu', 'pcok', 'max', 'complete', 'season',
}
_YEAR = re.compile(r'^(19\d\d|20\d\d)$')
_EPISODE = re.compile(r'(?:^|[ ._\-\[(])[Ss](\d{1,2})[ ._\-]?[Ee](\d{1,3})(?:[Ee-]\d{1,3})*(?=$|[ ._\-\])])')
_CROSS = re.compile(r'(?:^|[ ._\-\[(])(\d{1,2})x(\d{2,3})(?=$|[ ._\-\])])')


def _strip_ext(name):
    root, ext = os.path.splitext(name)
    return root if ext.lower() in VIDEO_EXT else name


def _tokens(text):
    # Bracketed release-group notes ("[Ben The Men]") never belong to the title.
    text = re.sub(r'\[[^\]]*\]', ' ', text)
    text = text.replace('(', ' ').replace(')', ' ')
    return [t for t in re.split(r'[ ._]+', text) if t]


def _clean(tokens):
    title = ' '.join(tokens).strip(' -')
    return re.sub(r'\s+', ' ', title)


def _is_tag(token):
    return token.lower().rstrip('-') in _TAGS


def parse(name, folder=None):
    """Return dict(type=..., title=..., year=..., show=..., season=..., episode=...).

    ``folder`` (the cloud folder name) is used when the file name alone is
    not readable, e.g. "abc123.mkv" inside "Movie.Title.2019.1080p".
    """
    info = _parse_one(_strip_ext(name or ''))
    if info['type'] is None and folder:
        info = _parse_one(folder)
    return info


def _parse_one(text):
    out = {'type': None, 'title': None, 'year': None, 'show': None, 'season': None, 'episode': None}
    m = _EPISODE.search(text) or _CROSS.search(text)
    if m:
        show_tokens = _tokens(text[:m.start()])
        year = None
        if show_tokens and _YEAR.match(show_tokens[-1]) and len(show_tokens) > 1:
            year = int(show_tokens.pop())     # "Show.2019.S01E02": the show's year
        show = _clean(show_tokens)
        if show:
            out.update(type='episode', show=show, title=show, season=int(m.group(1)),
                       episode=int(m.group(2)), year=year)
        return out

    tokens = _tokens(text)
    max_year = datetime.date.today().year + 1
    end = len(tokens)
    for i, tok in enumerate(tokens):
        if i > 0 and _is_tag(tok):
            end = i
            break
    # The release year is the last plausible year before the first tag; earlier
    # year-like words belong to the title ("2001.A.Space.Odyssey.1968", "1917.2019").
    year_at = None
    for i in range(min(end, len(tokens)) - 1, 0, -1):
        if _YEAR.match(tokens[i]) and int(tokens[i]) <= max_year:
            year_at = i
            break
    if year_at is None:
        return out
    title = _clean(tokens[:year_at])
    if not title:
        return out
    out.update(type='movie', title=title, year=int(tokens[year_at]))
    return out
