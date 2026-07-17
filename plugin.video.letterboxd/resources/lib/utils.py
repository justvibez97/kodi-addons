import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")


def get_setting(key, default=""):
    val = ADDON.getSetting(key)
    return val if val is not None else default


def log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(f"[{ADDON_ID}] {msg}", level)


def notify(title, msg, duration=5000):
    xbmcgui.Dialog().notification(title, msg, xbmcgui.NOTIFICATION_INFO, duration)


def notify_error(title, msg):
    xbmcgui.Dialog().notification(title, msg, xbmcgui.NOTIFICATION_ERROR, 6000)


def add_directory_item(handle, label, url, icon="", is_folder=True, info=None):
    li = xbmcgui.ListItem(label)
    li.setArt({"icon": icon, "thumb": icon})
    if info:
        li.setInfo("video", info)
    xbmcplugin.addDirectoryItem(handle, url, li, isFolder=is_folder)


def add_film_item(handle, film, base_url, mode="play", direct_url=None):
    """
    Add a film item to the Kodi directory listing.

    If *direct_url* is supplied the item URL is set to that value directly
    (e.g. a pre-built plugin://plugin.video.umbrella/? URL).
    """
    name = film.get("name") or "Unknown"
    year = str(film.get("year") or "")
    label = f"{name} ({year})" if year else name

    li = xbmcgui.ListItem(label)

    poster = film.get("poster", "")
    if poster:
        li.setArt({"poster": poster, "thumb": poster, "icon": poster})

    imdb_id = film.get("imdb_id", "")
    tmdb_id = film.get("tmdb_id", "")

    info = {"title": f"{name} ({year})" if year else name, "mediatype": "movie", "playcount": film.get("playcount", 0)}
    if year.isdigit():
        info["year"] = int(year)
    plot = film.get("description", "")
    lb_avg   = film.get("lb_community_rating")   # 0.5–5.0
    lb_count = film.get("lb_rating_count", 0)
    if lb_avg is not None:
        count_str = f"{lb_count:,}" if lb_count else ""
        lb_line = f"Letterboxd  ★ {lb_avg:.2f} / 5"
        if count_str:
            lb_line += f"  ({count_str} ratings)"
        plot = f"{lb_line}\n\n{plot}" if plot else lb_line
    if plot:
        info["plot"] = plot
    # imdbnumber lets Kodi/skins identify the exact film without a title search
    if imdb_id:
        info["imdbnumber"] = imdb_id

    # OMDb-sourced metadata — fills out the full InfoPanel template
    if film.get("imdb_rating") is not None:
        info["rating"] = float(film["imdb_rating"])
    if film.get("genre"):
        info["genre"] = film["genre"]
    if film.get("director"):
        info["director"] = film["director"]
    if film.get("duration"):
        info["duration"] = int(film["duration"])   # seconds
    if film.get("mpaa"):
        info["mpaa"] = film["mpaa"]

    rating = film.get("rating")
    if rating is not None:
        # Letterboxd is 0.5–5.0 stars; Kodi userrating is 1–10
        info["userrating"] = round(float(rating) * 2)

    watched = film.get("watched_date", "")
    if watched:
        info["date"] = watched

    li.setInfo("video", info)

    # Pass IMDB/TMDB IDs explicitly so the skin uses them directly
    unique_ids = {k: v for k, v in {"imdb": imdb_id, "tmdb": tmdb_id}.items() if v}
    if unique_ids:
        li.setUniqueIDs(unique_ids)

    # Ratings panel — same named ratings Umbrella sets, so the skin renders
    # the RT / Metacritic / IMDb badge row identically to its own lists
    imdb_score  = film.get("imdb_rating")
    imdb_votes  = film.get("imdb_votes", 0)
    rt_score    = film.get("rt_rating")
    mc_score    = film.get("mc_rating")
    tmdb_score      = film.get("tmdb_rating")
    lb_community    = film.get("lb_community_rating")   # public average (0.5–5.0)
    lb_rating_count = film.get("lb_rating_count", 0)

    if imdb_score is not None:
        li.setRating("imdb", float(imdb_score), int(imdb_votes), True)
    if rt_score is not None:
        li.setRating("tomatoes", float(rt_score))        # 0–100
    if mc_score is not None:
        li.setRating("metacritic", float(mc_score))      # 0–100
    if tmdb_score is not None:
        li.setRating("tmdb", float(tmdb_score))          # 0–10
    if lb_community is not None:
        # Letterboxd 0.5–5.0 → ×2 for 0–10 scale alongside IMDb/TMDB
        li.setRating("letterboxd", float(lb_community) * 2, int(lb_rating_count))

    # Not IsPlayable. Umbrella manages playback itself via its own custom
    # Player class (xbmc.Player().play_source(...)) inside its source-picker
    # dialog — it never calls xbmcplugin.setResolvedUrl(). If we mark this
    # item IsPlayable, Kodi opens its own resolving/playlist slot expecting
    # that callback; when it never comes, Kodi's playlist player concludes
    # the item is unplayable and tears down whatever Umbrella just started
    # playing ("skipping unplayable item"). RunPlugin avoids creating that
    # competing resolver context entirely.
    li.setProperty("IsPlayable", "false")

    if direct_url:
        url = direct_url
    else:
        params = {"mode": mode}
        if film.get("id"):
            params["film_id"] = film["id"]
        if film.get("imdb_id"):
            params["imdb_id"] = film["imdb_id"]
        if film.get("tmdb_id"):
            params["tmdb_id"] = film["tmdb_id"]
        if film.get("name"):
            params["film_name"] = film["name"]
        if year:
            params["film_year"] = year
        url = f"{base_url}?{urllib.parse.urlencode(params)}"

    xbmcplugin.addDirectoryItem(handle, url, li, isFolder=False)


def end_of_directory(handle, succeeded=True):
    xbmcplugin.endOfDirectory(handle, succeeded)
