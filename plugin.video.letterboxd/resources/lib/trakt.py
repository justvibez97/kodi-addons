"""
Trakt watchlist.

Uses the same Trakt sign-in as the watched filter: the token that the Trakt
add-on (script.trakt) stores after you authorise it on this device.
"""

import requests

from resources.lib.watched import _CLIENT_ID_DATA, _deobfuscate, _get_trakt_access_token

_API = "https://api.trakt.tv"


class TraktNotConnected(Exception):
    """script.trakt isn't authorised on this device (or its sign-in expired)."""


def get_watchlist(page=1, page_size=20):
    """
    One page of the user's Trakt movie watchlist, in the order set on Trakt.

    Returns (films, has_next). Each film carries Trakt's exact TMDb and IMDb
    IDs, so it is looked up by ID rather than by title.
    """
    token = _get_trakt_access_token()
    if not token:
        raise TraktNotConnected()

    resp = requests.get(
        f"{_API}/sync/watchlist/movies",
        headers={
            "Authorization":     f"Bearer {token}",
            "trakt-api-version": "2",
            "trakt-api-key":     _deobfuscate(_CLIENT_ID_DATA),
            "Content-Type":      "application/json",
        },
        params={"page": page, "limit": page_size},
        timeout=30,
    )
    if resp.status_code == 401:
        raise TraktNotConnected()
    resp.raise_for_status()

    films = []
    for entry in resp.json():
        movie = entry.get("movie") or {}
        ids = movie.get("ids") or {}
        if not movie.get("title"):
            continue
        films.append({
            "id":           "",   # Letterboxd slug, filled in from the TMDb ID
            "name":         movie["title"],
            "year":         str(movie.get("year") or ""),
            "poster":       "",
            "imdb_id":      ids.get("imdb") or "",
            "tmdb_id":      str(ids.get("tmdb") or ""),
            "description":  "",
            "rating":       None,
            "watched_date": "",
        })

    try:
        page_count = int(resp.headers.get("X-Pagination-Page-Count") or page)
    except ValueError:
        page_count = page
    return films, page < page_count
