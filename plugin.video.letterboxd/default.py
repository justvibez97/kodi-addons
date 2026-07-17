"""
plugin.video.letterboxd — main entry point.

URL routing: plugin://plugin.video.letterboxd/?mode=<mode>&<extra params>
"""
import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib.letterboxd_api import AuthError, LetterboxdAPIClient
from resources.lib.letterboxd_rss import LetterboxdRSSClient
from resources.lib.tmdb import TMDBClient
from resources.lib.utils import (
    ADDON,
    add_directory_item,
    add_film_item,
    end_of_directory,
    get_setting,
    log,
    notify,
    notify_error,
)

HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ARGS = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).query)

_ADDON_DATA = xbmcvfs.translatePath("special://userdata/addon_data/plugin.video.letterboxd/")
_TMDB_CACHE = _ADDON_DATA + "tmdb_cache.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def param(key, default=None):
    vals = ARGS.get(key)
    return vals[0] if vals else default


def url_for(mode, **kw):
    kw["mode"] = mode
    return f"{BASE_URL}?{urllib.parse.urlencode(kw)}"


def page_size():
    try:
        return int(get_setting("page_size") or "20")
    except ValueError:
        return 20


def get_tmdb():
    api_key = get_setting("tmdb_api_key").strip()
    omdb_api_key = get_setting("omdb_api_key").strip()
    return TMDBClient(api_key, _TMDB_CACHE, omdb_api_key=omdb_api_key)


def get_client():
    """Return (client, mode_str) or (None, None) if no username configured."""
    username = get_setting("username").strip()
    if not username:
        return None, None

    use_api = get_setting("use_api").lower() == "true"
    api_key = get_setting("api_key").strip()
    api_secret = get_setting("api_secret").strip()
    password = get_setting("password").strip()

    if use_api and api_key and api_secret:
        client = LetterboxdAPIClient(api_key, api_secret)
        if username and password:
            try:
                client.authenticate(username, password)
            except AuthError as exc:
                notify_error("Letterboxd auth failed", str(exc))
                log(f"Auth error: {exc}", xbmc.LOGERROR)
        return client, "api"

    return LetterboxdRSSClient(username), "rss"


def _require_client():
    client, mode = get_client()
    if client is None:
        xbmcgui.Dialog().ok(
            "Letterboxd",
            "Please enter your Letterboxd username in the add-on Settings.",
        )
        ADDON.openSettings()
    return client, mode


def _append_next(mode, page, has_next, **extra):
    if has_next:
        add_directory_item(
            HANDLE,
            f"Next page  ({page + 1})",
            url_for(mode, page=page + 1, **extra),
            icon="DefaultFolder.png",
            is_folder=True,
        )


def _build_watched_filter():
    """
    Return a callable  is_watched(film) -> bool  based on the hide_watched
    setting, or None if filtering is disabled.

    "0" = Off, "1" = Trakt, "2" = Letterboxd.
    The watched-ID sets are fetched once and cached inside the watched module.
    """
    from resources.lib import watched as _w

    hide = get_setting("hide_watched").strip()
    if hide == "1":  # Trakt
        imdb_set, tmdb_set = _w.get_trakt_watched_ids()
        if imdb_set or tmdb_set:
            return lambda f: _w.is_film_watched_trakt(f, imdb_set, tmdb_set)
    elif hide == "2":  # Letterboxd
        username = get_setting("username").strip()
        slug_set = _w.get_letterboxd_watched_slugs(username)
        if slug_set:
            return lambda f: _w.is_film_watched_letterboxd(f, slug_set)
    return None


def _umbrella_meta(film):
    """
    Build the 'meta' dict Umbrella expects on its play URL.

    Umbrella's own playItem() calls meta.get('plot') etc. unconditionally in
    several code paths. Without a meta param (and if its own internal
    meta lookup fails), meta stays None and every source resolve attempt
    crashes with AttributeError before ever reaching actual playback — this
    is also why the source picker showed no poster/plot info.
    """
    meta = {
        "mediatype": "movie",
        "title":     film.get("name", ""),
        "year":      film.get("year", ""),
        "imdb":      film.get("imdb_id", ""),
        "tmdb":      film.get("tmdb_id", ""),
        "plot":      film.get("description", ""),
        "poster":    film.get("poster", ""),
    }
    if film.get("imdb_rating") is not None:
        meta["rating"] = film["imdb_rating"]
    if film.get("genre"):
        meta["genre"] = film["genre"]
    if film.get("director"):
        meta["director"] = film["director"]
    if film.get("duration"):
        meta["duration"] = film["duration"]
    if film.get("mpaa"):
        meta["mpaa"] = film["mpaa"]
    return {k: v for k, v in meta.items() if v not in (None, "")}


def _enrich_and_add(films, fetch_lb_rating=True):
    """
    Enrich films with TMDB metadata then add to the directory listing.

    After enrichment the item URL is set directly to the player plugin
    (e.g. Umbrella) so that clicking a film launches the scraper/player
    immediately — no intermediate play handler, no stuck setResolvedUrl.
    Falls back to our own play handler if we have no metadata IDs yet.

    fetch_lb_rating=False skips live Letterboxd community-rating fetches
    (still reads from cache).  Use for large public lists to avoid blocking
    on one HTTP request per film before the list is displayed.

    Watched status: if a "Watched status from" source is configured, watched
    films have film["playcount"] = 1 set before the ListItem is built.  This
    lets Kodi show the native watched overlay and the skin's sidebar
    "Unwatched" toggle work exactly like Umbrella's own lists.
    """
    import time as _time
    from resources.lib.tmdb import _LB_RATING_TTL

    is_watched = _build_watched_filter()
    tmdb       = get_tmdb()
    player     = get_setting("player").strip() or "plugin.video.umbrella"

    def _enrich_one(film):
        """Enrich a single film — safe to run concurrently across films."""
        tmdb.enrich(film)
        if is_watched and is_watched(film):
            film["playcount"] = 1
        tmdb.enrich_ratings(film)
        if fetch_lb_rating:
            tmdb.enrich_letterboxd_rating(film)
        else:
            slug = film.get("id", "")
            if slug:
                entry = tmdb._cache.get(f"lb_rating|{slug}")
                if entry and _time.time() - entry.get("ts", 0) < _LB_RATING_TTL:
                    film.update(entry.get("data", {}))
        return film

    # Enrich all films concurrently; executor.map preserves input order
    with ThreadPoolExecutor(max_workers=5) as executor:
        enriched = list(executor.map(_enrich_one, films))

    # Direct IsPlayable URL straight to Umbrella — required so the resolving
    # handle Kodi opens for this click belongs to Umbrella's own invocation.
    # See the note in utils.add_film_item() for why.
    for film in enriched:
        film_name = film.get("name", "")
        film_year = str(film.get("year") or "")
        imdb_id   = film.get("imdb_id", "")
        tmdb_id   = film.get("tmdb_id", "")

        if film_name:
            play_params = {k: v for k, v in {
                "action": "play",
                "title":  film_name,
                "year":   film_year,
                "imdb":   imdb_id,
                "tmdb":   tmdb_id,
                "meta":   json.dumps(_umbrella_meta(film)),
            }.items() if v}
            direct_url = f"plugin://{player}/?{urllib.parse.urlencode(play_params)}"
            add_film_item(HANDLE, film, BASE_URL, direct_url=direct_url)
        else:
            # No title yet — fall back to our own play handler, which looks
            # up TMDB/IMDB IDs before handing off to Umbrella.
            add_film_item(HANDLE, film, BASE_URL)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def show_main_menu():
    username = get_setting("username").strip()
    if not username:
        xbmcgui.Dialog().ok(
            "Letterboxd",
            "Welcome! Please configure your Letterboxd username in Settings.",
        )
        ADDON.openSettings()
        return

    items = [
        ("Watchlist",     url_for("watchlist"),     "DefaultVideoPlaylists.png"),
        ("Browse Lists",  url_for("public_lists"),  "DefaultVideoPlaylists.png"),
        ("Diary",         url_for("diary"),          "DefaultRecentlyAddedMovies.png"),
        ("My Lists",      url_for("lists"),          "DefaultVideoPlaylists.png"),
        ("Search",        url_for("search"),         "DefaultAddonsSearch.png"),
        ("Settings",      url_for("settings"),       "DefaultAddonImages.png"),
    ]

    for label, u, icon in items:
        add_directory_item(HANDLE, label, u, icon=icon, is_folder=True)

    xbmcplugin.setPluginCategory(HANDLE, f"Letterboxd — {username}")
    end_of_directory(HANDLE)


def show_watchlist():
    client, _ = _require_client()
    if not client:
        return

    current_page = int(param("page", 1))
    try:
        films, has_next = client.get_watchlist(page=current_page, page_size=page_size())
    except Exception as exc:
        notify_error("Watchlist error", str(exc))
        log(f"Watchlist: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    _enrich_and_add(films)
    _append_next("watchlist", current_page, has_next)
    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.setPluginCategory(HANDLE, "Watchlist")
    end_of_directory(HANDLE)


def show_diary():
    client, _ = _require_client()
    if not client:
        return

    current_page = int(param("page", 1))
    try:
        films, has_next = client.get_diary(page=current_page, page_size=page_size())
    except Exception as exc:
        notify_error("Diary error", str(exc))
        log(f"Diary: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    _enrich_and_add(films)
    _append_next("diary", current_page, has_next)
    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.setPluginCategory(HANDLE, "Diary")
    end_of_directory(HANDLE)


def show_lists():
    client, _ = _require_client()
    if not client:
        return

    try:
        lists = client.get_lists()
    except Exception as exc:
        notify_error("Lists error", str(exc))
        log(f"Lists: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    if not lists:
        notify("My Lists", "No lists found on your profile.")

    for lst in lists:
        count = lst.get("film_count") or 0
        label = lst["name"]
        if count:
            label = f"{label}  ({count} films)"
        add_directory_item(
            HANDLE,
            label,
            url_for("list_entries", list_id=lst["id"], list_name=lst["name"]),
            icon="DefaultVideoPlaylists.png",
            is_folder=True,
            info={"plot": lst.get("description", "")},
        )

    xbmcplugin.setPluginCategory(HANDLE, "My Lists")
    end_of_directory(HANDLE)


def show_list_entries():
    client, _ = _require_client()
    if not client:
        return

    list_id = param("list_id", "")
    list_name = param("list_name", "List")
    current_page = int(param("page", 1))

    if not list_id:
        notify_error("Error", "Missing list ID")
        end_of_directory(HANDLE, False)
        return

    try:
        films, has_next = client.get_list_entries(
            list_id, page=current_page, page_size=page_size()
        )
    except Exception as exc:
        notify_error("List error", str(exc))
        log(f"List entries: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    _enrich_and_add(films)
    _append_next("list_entries", current_page, has_next, list_id=list_id, list_name=list_name)
    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.setPluginCategory(HANDLE, list_name)
    end_of_directory(HANDLE)


def show_public_lists():
    """Browse Lists sub-menu — no account needed."""
    # Both sections use HTML scraping with Referer-based pagination.
    add_directory_item(
        HANDLE, "Featured & Popular",
        url_for("public_lists_page", lb_url="https://letterboxd.com/lists/", paged=1),
        icon="DefaultVideoPlaylists.png",
        is_folder=True,
    )
    add_directory_item(
        HANDLE, "Official Lists (Top 50)",
        url_for("all_official_lists"),
        icon="DefaultVideoPlaylists.png",
        is_folder=True,
    )
    add_directory_item(
        HANDLE, "By Genre",
        url_for("genre_lists"),
        icon="DefaultVideoPlaylists.png",
        is_folder=True,
    )
    add_directory_item(
        HANDLE, "By Decade",
        url_for("decade_lists"),
        icon="DefaultVideoPlaylists.png",
        is_folder=True,
    )

    xbmcplugin.setPluginCategory(HANDLE, "Browse Lists")
    end_of_directory(HANDLE)


def show_all_official_lists():
    """
    Show all official Letterboxd lists fetched from the RSS feed at
    https://letterboxd.com/official/rss/. No pagination needed — the RSS
    returns all lists in a single feed.
    """
    client = LetterboxdRSSClient("")
    try:
        lists = client.get_official_lists_rss()
    except Exception as exc:
        notify_error("Official Lists error", str(exc))
        log(f"Official RSS: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    if not lists:
        notify("Official Lists", "No lists found.")

    for lst in lists:
        add_directory_item(
            HANDLE,
            f"★ {lst['name']}",
            url_for("public_list_films", list_path=lst["path"], list_name=lst["name"]),
            icon="DefaultVideoPlaylists.png",
            is_folder=True,
        )

    xbmcplugin.setPluginCategory(HANDLE, "Official Lists")
    end_of_directory(HANDLE)


def show_genre_lists():
    """
    Genre sub-menu — all confirmed-working Letterboxd official list slugs,
    verified by HTTP 200 response at build time.
    """
    categories = [
        # Core genres
        ("Action",              "/official/list/top-250-action-films/"),
        ("Animation",           "/official/list/top-250-animated-films/"),
        ("Anime",               "/official/list/top-100-anime-films/"),
        ("Biographical",        "/official/list/top-250-biographical-films/"),
        ("Comedy Specials",     "/official/list/top-100-comedy-specials/"),
        ("Concert Films",       "/official/list/top-100-concert-films/"),
        ("Crime",               "/official/list/top-250-crime-films/"),
        ("Documentary",         "/official/list/top-250-documentary-films/"),
        ("Fantasy",             "/official/list/top-100-live-action-fantasy-films/"),
        ("Horror",              "/official/list/top-250-horror-films/"),
        ("Musical",             "/official/list/top-250-musical-films/"),
        ("Mystery",             "/official/list/top-250-mystery-films/"),
        ("Science Fiction",     "/official/list/top-250-science-fiction-films/"),
        ("Sports",              "/official/list/top-100-sports-films/"),
        ("Theatre",             "/official/list/top-100-proshot-theatre-films/"),
        ("War",                 "/official/list/top-250-war-films/"),
        ("Western",             "/official/list/top-100-western-films/"),
        # Special curated lists
        ("Top 500 Films",       "/official/list/letterboxds-top-500-films/"),
        ("Most Fans",           "/official/list/top-250-films-with-the-most-fans/"),
        ("Underseen Films",     "/official/list/top-100-underseen-films/"),
        ("Underseen Horror",    "/official/list/top-50-underseen-horror-films/"),
    ]
    for label, list_path in categories:
        add_directory_item(
            HANDLE, label,
            url_for("public_list_films", list_path=list_path, list_name=label),
            icon="DefaultVideoPlaylists.png",
            is_folder=True,
        )
    xbmcplugin.setPluginCategory(HANDLE, "By Genre")
    end_of_directory(HANDLE)


def show_decade_lists():
    """Decade sub-menu — confirmed Letterboxd official decade lists."""
    decades = [
        ("1910s",  "/official/list/top-50-films-of-the-1910s/"),
        ("1920s",  "/official/list/top-100-films-of-the-1920s/"),
        ("1930s",  "/official/list/top-250-films-of-the-1930s/"),
        ("1940s",  "/official/list/top-250-films-of-the-1940s/"),
        ("1950s",  "/official/list/top-250-films-of-the-1950s/"),
        ("1960s",  "/official/list/top-250-films-of-the-1960s/"),
        ("1970s",  "/official/list/top-250-films-of-the-1970s/"),
        ("1980s",  "/official/list/top-250-films-of-the-1980s/"),
        ("1990s",  "/official/list/top-250-films-of-the-1990s/"),
        ("2000s",  "/official/list/top-250-films-of-the-2000s/"),
        ("2010s",  "/official/list/top-250-films-of-the-2010s/"),
        ("2020s",  "/official/list/top-250-films-of-the-2020s/"),
    ]
    for label, list_path in decades:
        add_directory_item(
            HANDLE, label,
            url_for("public_list_films", list_path=list_path, list_name=label),
            icon="DefaultVideoPlaylists.png",
            is_folder=True,
        )
    xbmcplugin.setPluginCategory(HANDLE, "By Decade")
    end_of_directory(HANDLE)


def show_public_lists_page():
    """
    Show Letterboxd list-folders from a discovery URL.

    paged=1  → real multi-page discovery section; shows a "More lists" button
               when Letterboxd signals a next page exists.
    paged=0  → single-page curated section (e.g. /lists/, /official/lists/);
               Letterboxd blocks /page/N/ with 403, so no pagination button.
    """
    lb_url       = param("lb_url", "https://letterboxd.com/lists/")
    paged        = param("paged", "0") == "1"
    current_page = int(param("page", 1))

    section_name = {
        "https://letterboxd.com/lists/":          "Featured & Popular",
        "https://letterboxd.com/official/lists/": "Official Lists",
    }.get(lb_url, "Lists")

    client = LetterboxdRSSClient("")
    try:
        lists, has_next = client.get_public_lists_at(lb_url, page=current_page)
    except Exception as exc:
        log(f"Public lists page: {exc}", xbmc.LOGERROR)
        # 403 on page 2+ is a known Letterboxd restriction — treat as end of list
        # rather than surfacing an error dialog to the user.
        xbmcplugin.setPluginCategory(HANDLE, section_name)
        end_of_directory(HANDLE)
        return

    if not lists:
        notify(section_name, "No lists found.")

    for lst in lists:
        creator = lst.get("creator", "")
        label = lst["name"]
        if creator and creator.lower() == "official":
            label = f"★ {label}"
        elif creator:
            label = f"{label}  [by {creator}]"
        add_directory_item(
            HANDLE,
            label,
            url_for("public_list_films", list_path=lst["path"], list_name=lst["name"]),
            icon="DefaultVideoPlaylists.png",
            is_folder=True,
        )

    if paged and has_next:
        add_directory_item(
            HANDLE,
            f"More lists  (page {current_page + 1})",
            url_for("public_lists_page", lb_url=lb_url, paged=1, page=current_page + 1),
            icon="DefaultFolder.png",
            is_folder=True,
        )

    xbmcplugin.setPluginCategory(HANDLE, section_name)
    end_of_directory(HANDLE)


def show_public_list_films():
    """
    Show films inside a specific public Letterboxd list.

    Uses virtual pagination: each Kodi page shows at most page_size() films so
    TMDB/OMDb enrichment never blocks on more than ~20 API calls at once.
    lb_page / lb_offset track position within the underlying Letterboxd page.
    """
    list_path = param("list_path", "")
    list_name = param("list_name", "List")
    lb_page   = int(param("lb_page",   1))
    lb_offset = int(param("lb_offset", 0))
    limit     = page_size()

    if not list_path:
        notify_error("Error", "Missing list path")
        end_of_directory(HANDLE, False)
        return

    client = LetterboxdRSSClient("")
    try:
        films, has_next, next_lb_page, next_offset = client.get_public_list_films(
            list_path, lb_page=lb_page, offset=lb_offset, limit=limit
        )
    except Exception as exc:
        notify_error("List error", str(exc))
        log(f"Public list films: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    # fetch_lb_rating=False: read cache only, never block on per-film HTTP requests.
    _enrich_and_add(films, fetch_lb_rating=False)

    if has_next:
        add_directory_item(
            HANDLE,
            "Next page →",
            url_for("public_list_films",
                    list_path=list_path, list_name=list_name,
                    lb_page=next_lb_page, lb_offset=next_offset),
            icon="DefaultFolder.png",
            is_folder=True,
        )

    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.setPluginCategory(HANDLE, list_name)
    end_of_directory(HANDLE)


def show_search_lists():
    """Search Letterboxd lists by keyword."""
    query = param("query", "")
    if not query:
        query = xbmcgui.Dialog().input(
            "Search Lists", type=xbmcgui.INPUT_ALPHANUM
        )
    if not query:
        end_of_directory(HANDLE, False)
        return

    current_page = int(param("page", 1))
    client = LetterboxdRSSClient("")
    try:
        lists, has_next = client.search_lists(query, page=current_page)
    except Exception as exc:
        notify_error("Search Lists error", str(exc))
        log(f"Search lists: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    if not lists:
        notify("Search Lists", f'No lists found for "{query}"')

    for lst in lists:
        creator = lst.get("creator", "")
        label = lst["name"]
        if creator and creator.lower() == "official":
            label = f"★ {label}"
        elif creator:
            label = f"{label}  [by {creator}]"
        add_directory_item(
            HANDLE,
            label,
            url_for("public_list_films", list_path=lst["path"], list_name=lst["name"]),
            icon="DefaultVideoPlaylists.png",
            is_folder=True,
        )

    if has_next:
        add_directory_item(
            HANDLE,
            f"More results  (page {current_page + 1})",
            url_for("search_lists", query=query, page=current_page + 1),
            icon="DefaultFolder.png",
            is_folder=True,
        )

    xbmcplugin.setPluginCategory(HANDLE, f"Lists: {query}")
    end_of_directory(HANDLE)


def show_search():
    query = param("query", "")
    if not query:
        query = xbmcgui.Dialog().input(
            "Search Letterboxd", type=xbmcgui.INPUT_ALPHANUM
        )
    if not query:
        end_of_directory(HANDLE, False)
        return

    client, _ = _require_client()
    if not client:
        return

    current_page = int(param("page", 1))
    try:
        films, has_next = client.search(
            query, page=current_page, page_size=page_size()
        )
    except Exception as exc:
        notify_error("Search error", str(exc))
        log(f"Search: {exc}", xbmc.LOGERROR)
        end_of_directory(HANDLE, False)
        return

    if not films:
        notify("Search", f'No results for "{query}"')

    _enrich_and_add(films)
    _append_next("search", current_page, has_next, query=query)
    xbmcplugin.setContent(HANDLE, "movies")
    xbmcplugin.setPluginCategory(HANDLE, f"Search: {query}")
    end_of_directory(HANDLE)


def open_settings():
    ADDON.openSettings()


def play_film():
    """
    Hand off to Umbrella for scraping and playback.

    Flow:
      1. Use TMDB/IMDB IDs passed in the URL if already known.
      2. If only title+year are known, query TMDB now (one call, cached).
      3. Invoke Umbrella via PlayMedia — confirmed by reading Umbrella's own
         source (resources/lib/modules/player.py) that its play flow ends
         with control.resolve(int(sys.argv[1]), True, item), i.e.
         xbmcplugin.setResolvedUrl on ITS OWN invocation's handle. PlayMedia
         opens a genuine resolving context for the target URL so that handle
         actually exists; RunPlugin/ActivateWindow don't create one, making
         that call a silent no-op.
    """
    tmdb_id = param("tmdb_id", "")
    imdb_id = param("imdb_id", "")
    film_name = param("film_name", "")
    film_year = param("film_year", "")
    player = get_setting("player").strip() or "plugin.video.umbrella"

    # If we don't have IDs yet, look them up via TMDB
    data = {}
    if (not tmdb_id or not imdb_id) and film_name:
        tmdb = get_tmdb()
        if tmdb.api_key:
            data = tmdb._search(film_name, film_year) or {}
            if data:
                tmdb_id = tmdb_id or data.get("tmdb_id", "")
                imdb_id = imdb_id or data.get("imdb_id", "")
                tmdb._set_cached(film_name, film_year, data)

    if not tmdb_id and not imdb_id and not film_name:
        notify_error("Playback error", "No film information available.")
        return

    meta = _umbrella_meta({
        "name": film_name, "year": film_year, "imdb_id": imdb_id, "tmdb_id": tmdb_id,
        "description": data.get("description", ""), "poster": data.get("poster", ""),
    })

    # Build Umbrella's play URL — same parameters Umbrella uses internally
    play_params = urllib.parse.urlencode({
        k: v for k, v in {
            "action": "play",
            "title": film_name,
            "year": film_year,
            "imdb": imdb_id,
            "tmdb": tmdb_id,
            "meta": json.dumps(meta),
        }.items() if v
    })
    umbrella_url = f"plugin://{player}/?{play_params}"

    log(f"Invoking player: {umbrella_url}")
    xbmc.executebuiltin(f'PlayMedia("{umbrella_url}")')


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_ROUTES = {
    "main":              show_main_menu,
    "watchlist":         show_watchlist,
    "diary":             show_diary,
    "lists":             show_lists,
    "list_entries":      show_list_entries,
    "public_lists":      show_public_lists,
    "public_lists_page": show_public_lists_page,
    "public_list_films":    show_public_list_films,
    "all_official_lists":   show_all_official_lists,
    "genre_lists":          show_genre_lists,
    "decade_lists":      show_decade_lists,
    "search":            show_search,
    "settings":          open_settings,
    "play":              play_film,
}


def router():
    mode = param("mode", "main")
    handler = _ROUTES.get(mode, show_main_menu)
    handler()


if __name__ == "__main__":
    router()
