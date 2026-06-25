"""
plugin.video.letterboxd — background service.

Two responsibilities:
  1. Playback monitor  — logs films to Letterboxd the moment Kodi finishes
                         playing them (≥ 80 % watched).
  2. Trakt → Letterboxd sync — on startup and every 30 minutes, fetches
                         recent Trakt watch history and logs any films not
                         yet in the Letterboxd diary.  Covers watches made
                         on any device (phone, TV, other Kodi boxes, etc.).

Logging paths (tried in order):
  a. Official Letterboxd API  (Settings → Advanced: Enable API + key/secret)
  b. Web form                 (Settings → Account: username + password only)
"""

import datetime
import json
import os
import re
import time
import unicodedata
import urllib.parse

import requests
import xbmc
import xbmcgui
import xbmcvfs

_TAG  = "[Letterboxd service]"
_BASE = "https://letterboxd.com"

# Path to the file that tracks the last successful Trakt sync timestamp
_SYNC_FILE = xbmcvfs.translatePath(
    "special://userdata/addon_data/plugin.video.letterboxd/trakt_lb_sync.json"
)


# ---------------------------------------------------------------------------
# Helpers shared by both paths
# ---------------------------------------------------------------------------

def _make_lb_session():
    from resources.lib.letterboxd_rss import _HEADERS
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


# ---------------------------------------------------------------------------
# Path A — official Letterboxd API
# ---------------------------------------------------------------------------

def _get_api_client():
    from resources.lib.letterboxd_api import LetterboxdAPIClient, AuthError
    from resources.lib.utils import get_setting

    if get_setting("use_api").lower() != "true":
        return None
    api_key    = get_setting("api_key").strip()
    api_secret = get_setting("api_secret").strip()
    username   = get_setting("username").strip()
    password   = get_setting("password").strip()
    if not api_key or not api_secret or not username or not password:
        return None
    try:
        client = LetterboxdAPIClient(api_key, api_secret)
        client.authenticate(username, password)
        return client
    except Exception as exc:
        xbmc.log(f"{_TAG} API auth error: {exc}", xbmc.LOGERROR)
        return None


def _log_via_api(client, imdb_id, title, year, watched_date):
    """Log one film via the official API. Returns True on success."""
    query = imdb_id if imdb_id else f"{title} {year}".strip()
    try:
        results, _ = client.search(query, page_size=5)
    except Exception as exc:
        xbmc.log(f"{_TAG} API search error: {exc}", xbmc.LOGWARNING)
        return False

    lb_id = None
    if results:
        if imdb_id:
            lb_id = results[0].get("id")
        else:
            for r in results:
                if str(r.get("year", "")) == str(year):
                    lb_id = r.get("id")
                    break
            lb_id = lb_id or results[0].get("id")

    if not lb_id:
        return False

    try:
        client.log_entry(lb_id, watched_date=watched_date)
        return True
    except Exception as exc:
        xbmc.log(f"{_TAG} API log_entry error: {exc}", xbmc.LOGERROR)
        return False


# ---------------------------------------------------------------------------
# Path B — Letterboxd web form  (no API key required)
# ---------------------------------------------------------------------------

class _WebSession:
    """
    A single authenticated Letterboxd web session.
    Login once, then call log_film() many times without re-authenticating.
    """

    def __init__(self, username, password):
        self._s        = _make_lb_session()
        self._csrf     = ""
        self._username = username
        self._password = password
        self.ok        = False   # True after successful login

    def _extract_csrf(self):
        """
        Try every known location for Letterboxd's CSRF token and return the
        first non-empty value found, or "".

        Priority order:
          1. /ajax/csrf-token  JSON endpoint  (most reliable when it exists)
          2. /sign-in/         hidden <input name="__csrf" value="...">
          3. /sign-in/         <meta name="csrf-token" content="...">
          4. Cookie named "csrf" (set by any of the above page loads)
        """
        # 1 — dedicated AJAX endpoint
        try:
            r = self._s.get(_BASE + "/ajax/csrf-token", timeout=10)
            if r.status_code == 200:
                token = (r.json() or {}).get("token", "")
                if token:
                    xbmc.log(f"{_TAG} Web: CSRF from /ajax/csrf-token", xbmc.LOGDEBUG)
                    return token
        except Exception:
            pass

        # 2 & 3 & 4 — sign-in page
        try:
            r = self._s.get(_BASE + "/sign-in/", timeout=15)
            xbmc.log(
                f"{_TAG} Web: sign-in page HTTP {r.status_code}  "
                f"cookies={list(self._s.cookies.keys())}  "
                f"html_snippet={r.text[200:400]!r}",
                xbmc.LOGINFO,
            )
            html = r.text

            # hidden input  <input ... name="__csrf" value="...">
            m = re.search(r'name="__csrf"[^>]*value="([^"]+)"', html)
            if not m:
                m = re.search(r'value="([^"]+)"[^>]*name="__csrf"', html)
            if m:
                xbmc.log(f"{_TAG} Web: CSRF from sign-in hidden input", xbmc.LOGDEBUG)
                return m.group(1)

            # meta tag  <meta name="csrf-token" content="...">
            m = re.search(r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"', html)
            if not m:
                m = re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="csrf-token"', html)
            if m:
                xbmc.log(f"{_TAG} Web: CSRF from sign-in meta tag", xbmc.LOGDEBUG)
                return m.group(1)

            # cookie set by page load (Letterboxd uses "com.xk72.webparts.csrf")
            for cookie_name in ("com.xk72.webparts.csrf", "csrf", "csrftoken"):
                csrf = self._s.cookies.get(cookie_name, "")
                if csrf:
                    xbmc.log(f"{_TAG} Web: CSRF from cookie {cookie_name!r}", xbmc.LOGDEBUG)
                    return csrf
        except Exception as exc:
            xbmc.log(f"{_TAG} Web: sign-in page error: {exc}", xbmc.LOGWARNING)

        return ""

    def login(self):
        self._csrf = self._extract_csrf()
        if not self._csrf:
            xbmc.log(f"{_TAG} Web: could not obtain CSRF token — tried all methods.", xbmc.LOGERROR)
            return False

        # Authenticate
        try:
            r = self._s.post(
                _BASE + "/user/login.do",
                data={
                    "__csrf":   self._csrf,
                    "username": self._username,
                    "password": self._password,
                },
                timeout=15,
            )
        except Exception as exc:
            xbmc.log(f"{_TAG} Web: login request failed: {exc}", xbmc.LOGWARNING)
            return False

        if '"result":"error"' in r.text or '"errors"' in r.text:
            xbmc.log(
                f"{_TAG} Web: login rejected — HTTP {r.status_code}  "
                f"body={r.text[:200]}",
                xbmc.LOGERROR,
            )
            return False

        # Keep CSRF up to date (server may rotate it after login)
        for _cn in ("com.xk72.webparts.csrf", "csrf", "csrftoken"):
            _cv = self._s.cookies.get(_cn, "")
            if _cv:
                self._csrf = _cv
                break
        self.ok = True
        xbmc.log(f"{_TAG} Web: logged in as {self._username!r}.", xbmc.LOGINFO)
        return True

    # ------------------------------------------------------------------
    # Slug helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_slug(title):
        """
        Convert a film title to a Letterboxd URL slug.

        Letterboxd slugs are lowercase, ASCII, with non-alphanumeric chars
        stripped and spaces replaced by hyphens.
        Examples:
          "Rain Man"        → "rain-man"
          "Amélie"          → "amelie"
          "Se7en"           → "se7en"
          "Ferris Bueller's Day Off" → "ferris-buellers-day-off"
          "2001: A Space Odyssey"    → "2001-a-space-odyssey"
        """
        # Normalise accented characters (Amélie → Amelie)
        s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
        s = s.lower()
        # Keep alphanumeric and spaces; drop everything else (colons, apostrophes …)
        s = re.sub(r"[^\w\s]", "", s)
        # Collapse whitespace/underscores into hyphens
        s = re.sub(r"[\s_]+", "-", s.strip())
        s = re.sub(r"-+", "-", s)
        return s

    def _film_id_from_slug(self, slug):
        """Fetch /film/{slug}/ and return its data-film-id, or None."""
        url = f"{_BASE}/film/{slug}/"
        try:
            r = self._s.get(url, timeout=15)
        except Exception as exc:
            xbmc.log(f"{_TAG} Web: GET {url} exception: {exc}", xbmc.LOGWARNING)
            return None

        if r.status_code != 200:
            xbmc.log(f"{_TAG} Web: GET /film/{slug}/ → HTTP {r.status_code}", xbmc.LOGDEBUG)
            return None

        text = r.text

        # 1. data-production-uid="film:12345" — current Letterboxd HTML structure
        m = re.search(r'data-production-uid="film:(\d+)"', text)
        if m:
            return m.group(1)

        # 2. Legacy: data-film-id attribute
        m = re.search(r'data-film-id="([^"]+)"', text)
        if m:
            return m.group(1)

        # 3. AJAX action URL  /film:12345/
        m = re.search(r'/film:(\d+)/', text)
        if m:
            return m.group(1)

        # 4. JSON / JS variable  "filmId":"12345"
        m = re.search(r'"filmId"\s*:\s*"?(\d+)"?', text)
        if m:
            return m.group(1)

        xbmc.log(f"{_TAG} Web: no film ID found in /film/{slug}/", xbmc.LOGWARNING)
        return None

    # ------------------------------------------------------------------

    def find_film_id(self, imdb_id, title, year):
        """
        Return Letterboxd's internal data-film-id for the given film, or None.

        Strategy (no search page needed — Letterboxd search is JS-rendered):
          1. Try /film/{slug}/            e.g. /film/rain-man/
          2. Try /film/{slug}-{year}/     disambiguation e.g. /film/the-gift-2015/
          3. Try /film/{slug}-1/          Letterboxd occasionally appends -1, -2 …
        """
        if not title:
            return None

        slug = self._make_slug(title)
        year_str = str(year) if year else ""

        candidates = [slug]
        if year_str:
            candidates.append(f"{slug}-{year_str}")
        candidates.append(f"{slug}-1")

        for candidate in candidates:
            film_id = self._film_id_from_slug(candidate)
            if film_id:
                xbmc.log(
                    f"{_TAG} Web: {title!r} ({year}) → /film/{candidate}/  id={film_id}",
                    xbmc.LOGINFO,
                )
                return film_id

        xbmc.log(
            f"{_TAG} Web: could not find {title!r} ({year})  "
            f"tried slugs: {candidates}",
            xbmc.LOGWARNING,
        )
        return None

    def log_film(self, film_id, watched_date):
        """Submit a diary entry. Returns True on success."""
        for _cn in ("com.xk72.webparts.csrf", "csrf", "csrftoken"):
            _cv = self._s.cookies.get(_cn, "")
            if _cv:
                self._csrf = _cv
                break
        try:
            r = self._s.post(
                _BASE + "/s/save-diary-entry",
                data={
                    "__csrf":           self._csrf,
                    "filmId":           film_id,
                    "specifiedDate":    "true",
                    "viewingDateStr":   watched_date,
                    "rewatch":          "false",
                    "rating":           "",
                    "review":           "",
                    "containsSpoilers": "false",
                    "liked":            "false",
                    "tag":              "",
                    "tagOption":        "",
                },
                headers={
                    "Accept":           "*/*",
                    "Origin":           _BASE,
                    "Referer":          f"{_BASE}/",
                    "Sec-Fetch-Dest":   "empty",
                    "Sec-Fetch-Mode":   "cors",
                    "Sec-Fetch-Site":   "same-origin",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=15,
            )
            xbmc.log(
                f"{_TAG} Web: diary POST filmId={film_id} date={watched_date} "
                f"→ HTTP {r.status_code}  body={r.text[:300]!r}",
                xbmc.LOGINFO,
            )
            return r.status_code == 200
        except Exception as exc:
            xbmc.log(f"{_TAG} Web: diary save error: {exc}", xbmc.LOGERROR)
            return False


# ---------------------------------------------------------------------------
# Log a single film (tries API → web)
# ---------------------------------------------------------------------------

def _log_one(imdb_id, title, year, watched_date, web_session=None):
    """
    Log one film to Letterboxd.  Returns True on success.

    web_session — a pre-authenticated _WebSession to reuse (avoids re-login).
    """
    # Try official API first
    client = _get_api_client()
    if client and _log_via_api(client, imdb_id, title, year, watched_date):
        return True

    # Web-form fallback
    if web_session and web_session.ok:
        film_id = web_session.find_film_id(imdb_id, title, year)
        if film_id:
            return web_session.log_film(film_id, watched_date)

    return False


# ---------------------------------------------------------------------------
# Trakt → Letterboxd sync
# ---------------------------------------------------------------------------

def _load_sync_state():
    """Return {"last_sync": "ISO timestamp or None"}."""
    try:
        if os.path.exists(_SYNC_FILE):
            with open(_SYNC_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"last_sync": None}


def _save_sync_state(state):
    try:
        os.makedirs(os.path.dirname(_SYNC_FILE), exist_ok=True)
        with open(_SYNC_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:
        xbmc.log(f"{_TAG} Could not save sync state: {exc}", xbmc.LOGWARNING)


def _fetch_trakt_history(start_at=None):
    """
    Yield movie dicts from Trakt watch history (newest first).
    Handles pagination automatically.
    Each dict: {imdb_id, title, year, watched_date}
    """
    from resources.lib.watched import _get_trakt_access_token, _deobfuscate, _CLIENT_ID_DATA

    token = _get_trakt_access_token()
    if not token:
        return

    headers = {
        "Authorization":     f"Bearer {token}",
        "trakt-api-version": "2",
        "trakt-api-key":     _deobfuscate(_CLIENT_ID_DATA),
        "Content-Type":      "application/json",
    }

    page = 1
    while True:
        params = {"limit": 100, "page": page}
        if start_at:
            params["start_at"] = start_at

        try:
            r = requests.get(
                "https://api.trakt.tv/sync/history/movies",
                headers=headers,
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            entries = r.json()
        except Exception as exc:
            xbmc.log(f"{_TAG} Trakt history fetch error: {exc}", xbmc.LOGWARNING)
            return

        if not entries:
            return

        for entry in entries:
            movie = entry.get("movie", {})
            ids   = movie.get("ids", {})
            # "watched_at" is like "2026-05-28T14:30:00.000Z" → keep YYYY-MM-DD
            watched_at   = (entry.get("watched_at") or "")[:10]
            watched_date = watched_at or datetime.date.today().isoformat()
            yield {
                "imdb_id":      ids.get("imdb", ""),
                "title":        movie.get("title", ""),
                "year":         movie.get("year", 0),
                "watched_date": watched_date,
            }

        # Check for more pages
        total_pages = int(r.headers.get("X-Pagination-Page-Count", 1))
        if page >= total_pages:
            return
        page += 1


def _sync_trakt_to_letterboxd(monitor, player):
    """
    Fetch Trakt history since the last sync and log any new watches to
    the Letterboxd diary.  Runs on startup (delayed) and every 30 minutes.

    Playback-aware: skips entirely if media is playing, and aborts mid-sync
    the moment playback starts so the stream is never interrupted.
    """
    from resources.lib.utils import get_setting

    if player.isPlaying():
        xbmc.log(f"{_TAG} Sync skipped — playback active.", xbmc.LOGINFO)
        return

    xbmc.log(f"{_TAG} Trakt→Letterboxd sync starting…", xbmc.LOGINFO)

    state    = _load_sync_state()
    start_at = state.get("last_sync")

    films = list(_fetch_trakt_history(start_at=start_at))
    if not films:
        xbmc.log(f"{_TAG} No new Trakt watches to sync.", xbmc.LOGINFO)
        state["last_sync"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _save_sync_state(state)
        return

    xbmc.log(f"{_TAG} Syncing {len(films)} Trakt watch(es) to Letterboxd…", xbmc.LOGINFO)

    username = get_setting("username").strip()
    password = get_setting("password").strip()
    web_session = None
    if username and password:
        web_session = _WebSession(username, password)
        web_session.login()

    synced       = 0
    consecutive_failures = 0

    for film in films:
        # Abort immediately if playback starts — stream takes priority
        if player.isPlaying() or monitor.abortRequested():
            xbmc.log(f"{_TAG} Sync aborted — playback started.", xbmc.LOGINFO)
            return   # don't save timestamp; retry next interval

        logged = _log_one(
            film["imdb_id"], film["title"], film["year"], film["watched_date"],
            web_session=web_session,
        )
        if logged:
            synced += 1
            consecutive_failures = 0
            xbmc.log(
                f"{_TAG} Synced: {film['title']!r} ({film['year']}) "
                f"[watched {film['watched_date']}]",
                xbmc.LOGINFO,
            )
        else:
            consecutive_failures += 1
            # If the first 3 films all fail the web session is broken
            # (e.g. Cloudflare blocking) — stop hammering and wait for next interval
            if consecutive_failures >= 3 and synced == 0:
                xbmc.log(
                    f"{_TAG} Sync aborted after {consecutive_failures} consecutive "
                    f"failures — will retry next interval.",
                    xbmc.LOGWARNING,
                )
                return   # don't save timestamp so we retry the same films

    if synced:
        xbmcgui.Dialog().notification(
            "Letterboxd",
            f"Synced {synced} film{'s' if synced != 1 else ''} from Trakt",
            xbmcgui.NOTIFICATION_INFO,
            5000,
        )

    xbmc.log(f"{_TAG} Sync complete: {synced}/{len(films)} logged.", xbmc.LOGINFO)
    state["last_sync"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _save_sync_state(state)


# ---------------------------------------------------------------------------
# Playback monitor — catches films played in Kodi right now
# ---------------------------------------------------------------------------

class _Player(xbmc.Player):

    def __init__(self):
        super().__init__()
        self._clear()

    def _clear(self):
        self._imdb  = ""
        self._title = ""
        self._year  = 0
        self._t0    = 0.0
        self._dur   = 0.0

    def onAVStarted(self):
        self._clear()
        try:
            tag = self.getVideoInfoTag()
            if tag.getMediaType() != "movie":
                return
            self._imdb  = tag.getIMDBNumber() or ""
            self._title = tag.getTitle()      or ""
            self._year  = tag.getYear()       or 0
            self._t0    = time.monotonic()
            try:
                self._dur = self.getTotalTime()
            except Exception:
                self._dur = 0.0
            xbmc.log(
                f"{_TAG} Now playing: {self._title!r} ({self._year})"
                f"  imdb={self._imdb!r}  dur={self._dur:.0f}s",
                xbmc.LOGINFO,
            )
        except Exception as exc:
            xbmc.log(f"{_TAG} onAVStarted error: {exc}", xbmc.LOGWARNING)

    def onPlayBackEnded(self):
        self._handle_stop(natural_end=True)

    def onPlayBackStopped(self):
        self._handle_stop(natural_end=False)

    def onPlayBackError(self):
        self._clear()

    def _handle_stop(self, natural_end):
        title, year, imdb = self._title, self._year, self._imdb
        t0,   dur         = self._t0,    self._dur
        self._clear()

        if not title:
            return

        if not natural_end and dur > 0:
            elapsed = time.monotonic() - t0
            if elapsed / dur < 0.80:
                xbmc.log(
                    f"{_TAG} Skipping {title!r} — only {elapsed/dur*100:.0f}% watched.",
                    xbmc.LOGINFO,
                )
                return

        today = datetime.date.today().isoformat()
        from resources.lib.utils import get_setting
        username = get_setting("username").strip()
        password = get_setting("password").strip()

        web = None
        if username and password:
            web = _WebSession(username, password)
            web.login()

        logged = _log_one(imdb, title, year, today, web_session=web)
        if logged:
            xbmcgui.Dialog().notification(
                "Letterboxd",
                f"Logged to diary: {title} ({year})",
                xbmcgui.NOTIFICATION_INFO,
                5000,
            )


# ---------------------------------------------------------------------------
# Service entry point
# ---------------------------------------------------------------------------

_SYNC_INTERVAL  = 30 * 60   # seconds between periodic syncs
_STARTUP_DELAY  =  5 * 60   # wait 5 min after boot before first sync

def run():
    monitor = xbmc.Monitor()
    player  = _Player()
    xbmc.log(f"{_TAG} Started.", xbmc.LOGINFO)

    # Delay the first sync so it never races with startup playback
    monitor.waitForAbort(_STARTUP_DELAY)
    if monitor.abortRequested():
        xbmc.log(f"{_TAG} Stopped.", xbmc.LOGINFO)
        return

    _sync_trakt_to_letterboxd(monitor, player)
    last_sync_ts = time.monotonic()

    while not monitor.abortRequested():
        monitor.waitForAbort(60)
        if monitor.abortRequested():
            break
        if time.monotonic() - last_sync_ts >= _SYNC_INTERVAL:
            _sync_trakt_to_letterboxd(monitor, player)
            last_sync_ts = time.monotonic()

    xbmc.log(f"{_TAG} Stopped.", xbmc.LOGINFO)


if __name__ == "__main__":
    run()
