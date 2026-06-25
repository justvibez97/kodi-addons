"""
Watch-history helpers for the "Hide watched films" feature.

Backends
--------
trakt       — GET /sync/watched/movies using the Trakt access token stored
              by script.trakt (auto-refreshes if expired).
letterboxd  — Scrapes letterboxd.com/{username}/films/ pages for logged slugs.

All results are cached at module level (one Kodi plugin invocation = one
Python interpreter lifetime), so the HTTP round-trip happens at most once
per directory listing.
"""

import json
import re
import time

import requests
import xbmc
import xbmcaddon
import xbmcgui

# Reuse the same headers that letterboxd_rss.py uses — these include the
# Sec-Fetch-* and other browser headers that Letterboxd requires to avoid
# being detected as a bot and having the connection aborted.
from resources.lib.letterboxd_rss import _HEADERS as _LB_HEADERS

# ---------------------------------------------------------------------------
# Trakt client credentials  (same obfuscation scheme as script.trakt uses)
# ---------------------------------------------------------------------------

_CLIENT_ID_DATA = [
    123, 39,116,119, 33,117, 32, 32,118, 35,119, 32,117,122,116, 35,
    119, 32,119, 38, 36,113, 39,117,113,117,115, 33,113, 32, 33, 38,
     36, 35,114,114, 39,112,116, 35,118,123,114,117,122, 39, 35,112,
    114,112,122,122, 38,116,115,116, 36, 39,122,113, 38,122,118, 38,
]
_CLIENT_SECRET_DATA = [
     35, 32, 33,116, 36, 33,123,119,119,118,114,118,123,123, 38,114,
    113,114,122, 39,119,117, 32, 33,112,117, 39, 33, 35, 36,123, 35,
     38, 39, 33,117, 36,123, 38,118,114, 35,122,123,112,116, 36,113,
    117,123,118,115, 36, 36,119,117,123,123, 32, 35,119, 35, 32,113,
]


def _deobfuscate(data):
    return "".join(chr(b ^ 0x42) for b in data)


# ---------------------------------------------------------------------------
# Session-scoped caches (reset on every plugin invocation)
# _trakt_cache  : (frozenset[imdb], frozenset[tmdb]) or False (= tried/failed)
# _lb_slug_cache: frozenset[slug]                    or False
# ---------------------------------------------------------------------------

_trakt_cache    = None
_lb_slug_cache  = None


# ---------------------------------------------------------------------------
# Trakt — token management
# ---------------------------------------------------------------------------

def _load_trakt_auth():
    """
    Read the Trakt authorization dict from script.trakt settings.
    Returns the dict, or None if not configured.  Token values are never logged.
    """
    try:
        addon = xbmcaddon.Addon("script.trakt")
        raw = addon.getSetting("authorization")
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        xbmc.log(f"[Letterboxd/watched] Cannot read Trakt auth: {exc}", xbmc.LOGWARNING)
        return None


def _save_trakt_auth(auth_dict):
    """Persist a refreshed Trakt token back into script.trakt settings."""
    try:
        xbmcaddon.Addon("script.trakt").setSetting("authorization", json.dumps(auth_dict))
    except Exception as exc:
        xbmc.log(f"[Letterboxd/watched] Cannot save Trakt auth: {exc}", xbmc.LOGWARNING)


def _get_trakt_access_token():
    """
    Return a valid Trakt access-token string, or None.
    Silently refreshes via the refresh_token when the access_token has expired.
    """
    auth = _load_trakt_auth()
    if not auth:
        return None

    access_token  = auth.get("access_token", "")
    refresh_token = auth.get("refresh_token", "")
    created_at    = auth.get("created_at", 0)
    expires_in    = auth.get("expires_in", 7776000)  # 90 days default

    if not access_token:
        return None

    # 5-minute safety buffer
    is_expired = int(time.time()) >= (created_at + expires_in - 300)
    if not is_expired:
        return access_token

    if not refresh_token:
        xbmc.log(
            "[Letterboxd/watched] Trakt token expired and no refresh_token is available.",
            xbmc.LOGWARNING,
        )
        return None

    xbmc.log("[Letterboxd/watched] Trakt token expired — refreshing…", xbmc.LOGINFO)
    try:
        resp = requests.post(
            "https://api.trakt.tv/oauth/token",
            json={
                "refresh_token": refresh_token,
                "client_id":     _deobfuscate(_CLIENT_ID_DATA),
                "client_secret": _deobfuscate(_CLIENT_SECRET_DATA),
                "redirect_uri":  "urn:ietf:wg:oauth:2.0:oob",
                "grant_type":    "refresh_token",
            },
            timeout=15,
        )
        if resp.status_code in (400, 401, 403):
            # Refresh token itself is expired (Trakt refresh tokens last 1 year).
            # The user must re-authorize script.trakt to get fresh tokens.
            xbmc.log(
                "[Letterboxd/watched] Trakt refresh token is expired. "
                "Please open script.trakt → Settings and re-authorize.",
                xbmc.LOGWARNING,
            )
            xbmcgui.Dialog().notification(
                "Letterboxd — Trakt filter",
                "Trakt session expired. Re-authorize via Add-ons → script.trakt → Settings.",
                xbmcgui.NOTIFICATION_WARNING,
                8000,
            )
            return None
        resp.raise_for_status()
        auth.update(resp.json())
        _save_trakt_auth(auth)
        xbmc.log("[Letterboxd/watched] Trakt token refreshed successfully.", xbmc.LOGINFO)
        return auth.get("access_token", "")
    except Exception as exc:
        xbmc.log(f"[Letterboxd/watched] Trakt token refresh failed: {exc}", xbmc.LOGWARNING)
        return None


# ---------------------------------------------------------------------------
# Trakt — watched IDs
# ---------------------------------------------------------------------------

def get_trakt_watched_ids():
    """
    Return (imdb_set, tmdb_set) — frozensets of watched movie IDs from Trakt.

    imdb_set : strings like "tt1234567"
    tmdb_set : strings of integer TMDB IDs e.g. "27205"

    Returns (frozenset(), frozenset()) when Trakt is not configured or
    the API call fails — filtering is simply skipped in that case.
    """
    global _trakt_cache
    if _trakt_cache is not None:
        if _trakt_cache is False:
            return frozenset(), frozenset()
        return _trakt_cache

    access_token = _get_trakt_access_token()
    if not access_token:
        xbmc.log(
            "[Letterboxd/watched] Trakt not configured — watched filter disabled.",
            xbmc.LOGINFO,
        )
        _trakt_cache = False
        return frozenset(), frozenset()

    try:
        resp = requests.get(
            "https://api.trakt.tv/sync/watched/movies",
            headers={
                "Authorization":     f"Bearer {access_token}",
                "trakt-api-version": "2",
                "trakt-api-key":     _deobfuscate(_CLIENT_ID_DATA),
                "Content-Type":      "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()

        imdb_ids = set()
        tmdb_ids = set()
        for entry in resp.json():
            ids = entry.get("movie", {}).get("ids", {})
            if ids.get("imdb"):
                imdb_ids.add(ids["imdb"])
            if ids.get("tmdb"):
                tmdb_ids.add(str(ids["tmdb"]))

        xbmc.log(
            f"[Letterboxd/watched] Trakt: {len(imdb_ids)} IMDB + {len(tmdb_ids)} TMDB watched IDs loaded.",
            xbmc.LOGINFO,
        )
        _trakt_cache = frozenset(imdb_ids), frozenset(tmdb_ids)
        return _trakt_cache

    except Exception as exc:
        xbmc.log(f"[Letterboxd/watched] Trakt /sync/watched/movies failed: {exc}", xbmc.LOGWARNING)
        _trakt_cache = False
        return frozenset(), frozenset()


# ---------------------------------------------------------------------------
# Letterboxd — watched slugs
# ---------------------------------------------------------------------------

def get_letterboxd_watched_slugs(username):
    """
    Scrape letterboxd.com/{username}/films/ (all pages) to collect every film
    slug the user has logged.

    Returns a frozenset of slug strings (e.g. "the-godfather-1972").
    Returns frozenset() when the username is empty or scraping fails.
    """
    global _lb_slug_cache
    if _lb_slug_cache is not None:
        return _lb_slug_cache if _lb_slug_cache is not False else frozenset()

    if not username:
        _lb_slug_cache = False
        return frozenset()

    session = requests.Session()
    session.headers.update(_LB_HEADERS)
    try:
        session.get("https://letterboxd.com/", timeout=15)
    except Exception:
        pass

    slugs = set()
    page  = 1
    base  = f"https://letterboxd.com/{username.lower()}/films"

    while True:
        url = f"{base}/page/{page}/" if page > 1 else f"{base}/"
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
        except Exception as exc:
            xbmc.log(
                f"[Letterboxd/watched] LB films page {page} error: {exc}",
                xbmc.LOGWARNING,
            )
            break

        # Film slugs are embedded as data-film-slug="..." attributes
        found = re.findall(r'data-film-slug="([^"]+)"', resp.text)
        if not found:
            break
        slugs.update(found)

        # Stop when Letterboxd's pagination element has no "next" link
        if 'class="next "' not in resp.text and 'class="next"' not in resp.text:
            break
        page += 1

    xbmc.log(
        f"[Letterboxd/watched] Letterboxd: {len(slugs)} logged slugs for '{username}'.",
        xbmc.LOGINFO,
    )
    _lb_slug_cache = frozenset(slugs)
    return _lb_slug_cache


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def is_film_watched_trakt(film, imdb_set, tmdb_set):
    """
    Return True if this film's IMDB or TMDB ID appears in the watched sets.
    Requires tmdb.enrich() to have already been called on the film dict.
    """
    imdb = film.get("imdb_id") or ""
    tmdb = str(film.get("tmdb_id") or "")
    if imdb and imdb in imdb_set:
        return True
    if tmdb and tmdb in tmdb_set:
        return True
    return False


def is_film_watched_letterboxd(film, slug_set):
    """
    Return True if this film's Letterboxd slug appears in the watched set.
    The slug lives in film["id"] and is available before TMDB enrichment.
    """
    slug = film.get("id") or ""
    return bool(slug and slug in slug_set)
