"""
TMDB API client for enriching Letterboxd RSS data.

Free API key: themoviedb.org/settings/api (instant, no review).
OMDb API key: omdbapi.com/apikey.aspx (1 000 req/day free).
Results are cached to avoid repeated lookups across sessions.
"""
import json
import os
import threading
import time

import requests
import xbmc

_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/w500"
_OMDB_BASE = "https://www.omdbapi.com/"
_LB_BASE = "https://letterboxd.com/film/"
_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days
_LB_RATING_TTL = 60 * 60 * 24 * 7  # 7 days (community ratings change)


class TMDBClient:
    def __init__(self, api_key, cache_path, omdb_api_key=""):
        self.api_key = api_key
        self.omdb_api_key = omdb_api_key
        self.cache_path = cache_path
        self._cache = self._load_cache()
        self._cache_lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def enrich(self, film):
        """
        Add poster, plot, imdb_id, tmdb_id to a film dict in-place.
        Returns the same dict.
        """
        if not self.api_key:
            return film

        name = film.get("name", "")
        year = str(film.get("year") or "")
        if not name:
            return film

        cached = self._get_cached(name, year)
        if cached:
            film.update(cached)
            return film

        data = self._search(name, year)
        if not data:
            return film

        film.update(data)
        self._set_cached(name, year, data)
        return film

    def enrich_ratings(self, film):
        """
        Add OMDb ratings (IMDb score, Rotten Tomatoes %, Metacritic %)
        to a film dict in-place.  Requires an OMDb API key and an imdb_id.
        """
        imdb_id = film.get("imdb_id", "")
        if not imdb_id or not self.omdb_api_key:
            return film

        cache_key = f"omdb|{imdb_id}"
        entry = self._cache.get(cache_key)
        if entry and time.time() - entry.get("ts", 0) < _CACHE_TTL:
            film.update(entry.get("data", {}))
            return film

        try:
            resp = self._session.get(
                _OMDB_BASE,
                params={"i": imdb_id, "apikey": self.omdb_api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return film

        if data.get("Response") != "True":
            return film

        result = {}

        # IMDb score (0–10)
        imdb_r = data.get("imdbRating", "N/A")
        if imdb_r not in ("N/A", "", None):
            try:
                result["imdb_rating"] = float(imdb_r)
                votes = data.get("imdbVotes", "0").replace(",", "")
                result["imdb_votes"] = int(votes) if votes.isdigit() else 0
            except ValueError:
                pass

        # RT / Metacritic (stored as 0–100 integers to match Kodi skin convention)
        for r in data.get("Ratings", []):
            src = r.get("Source", "")
            val = r.get("Value", "")
            try:
                if src == "Rotten Tomatoes" and val.endswith("%"):
                    result["rt_rating"] = int(val[:-1])
                elif src == "Metacritic" and "/" in val:
                    result["mc_rating"] = int(val.split("/")[0])
            except (ValueError, IndexError):
                pass

        # Rich InfoPanel metadata — genre, director, runtime, mpaa
        genre = data.get("Genre", "")
        if genre and genre != "N/A":
            result["genre"] = genre
        director = data.get("Director", "")
        if director and director != "N/A":
            result["director"] = director
        runtime_str = data.get("Runtime", "")  # e.g. "93 min"
        if runtime_str and runtime_str not in ("N/A", ""):
            try:
                result["duration"] = int(runtime_str.split()[0]) * 60  # seconds
            except (ValueError, IndexError):
                pass
        mpaa = data.get("Rated", "")
        if mpaa and mpaa not in ("N/A", ""):
            result["mpaa"] = mpaa

        if result:
            film.update(result)
            self._cache[cache_key] = {"ts": time.time(), "data": result}
            self._save_cache()

        # TMDB vote_average — backfill for films cached before this field was added
        # (this block is part of enrich_ratings; enrich_letterboxd_rating is below)
        if film.get("tmdb_id") and film.get("tmdb_rating") is None and self.api_key:
            va_key = f"tmdb_va|{film['tmdb_id']}"
            va_entry = self._cache.get(va_key)
            if va_entry and time.time() - va_entry.get("ts", 0) < _CACHE_TTL:
                film["tmdb_rating"] = va_entry["data"].get("tmdb_rating")
            else:
                try:
                    r = self._session.get(
                        f"{_BASE}/movie/{film['tmdb_id']}",
                        params={"api_key": self.api_key},
                        timeout=10,
                    )
                    va = float(r.json().get("vote_average") or 0) or None
                    if va:
                        film["tmdb_rating"] = va
                        self._cache[va_key] = {
                            "ts": time.time(),
                            "data": {"tmdb_rating": va},
                        }
                        self._save_cache()
                except Exception:
                    pass

        return film

    def enrich_letterboxd_rating(self, film):
        """
        Fetch the Letterboxd community average rating.
        Tries five extraction strategies in order since Letterboxd injects
        the JSON-LD content via JavaScript (the script tag is empty in static HTML).
        Requires film["id"] == Letterboxd slug (e.g. "may", "i-origins").
        Cached for 7 days.
        """
        slug = film.get("id", "")
        if not slug:
            return film

        cache_key = f"lb_rating|{slug}"
        entry = self._cache.get(cache_key)
        if entry and time.time() - entry.get("ts", 0) < _LB_RATING_TTL:
            film.update(entry.get("data", {}))
            return film

        import re as _re

        _HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        rv = None
        rc = 0
        strategy_used = "none"

        try:
            # ── Primary: film page HTML ─────────────────────────────────────
            resp = requests.get(
                f"{_LB_BASE}{slug}/", headers=_HEADERS, timeout=15,
            )
            resp.raise_for_status()
            html = resp.text

            # Strategy 1 – JSON-LD script tag (populated in some responses)
            for block in _re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>'
                r'([\s\S]*?)</script>',
                html,
            ):
                content = block.strip()
                if not content:
                    continue
                try:
                    ld = json.loads(content)
                    agg = ld.get("aggregateRating", {})
                    _rv = agg.get("ratingValue")
                    if _rv is not None:
                        rv = float(_rv)
                        rc = int(agg.get("ratingCount", 0))
                        strategy_used = "ld+json"
                        break
                except Exception:
                    pass

            # Strategy 2 – "aggregateRating" JSON fragment anywhere in the page
            if rv is None:
                m = _re.search(r'"aggregateRating"\s*:\s*(\{[^}]+\})', html)
                if m:
                    try:
                        agg = json.loads(m.group(1))
                        _rv = agg.get("ratingValue")
                        if _rv is not None:
                            rv = float(_rv)
                            rc = int(agg.get("ratingCount", 0))
                            strategy_used = "agg_fragment"
                    except Exception:
                        pass

            # Strategy 3 – data-average-rating attribute
            if rv is None:
                m = _re.search(r'data-average-rating=["\']?([\d.]+)', html)
                if m:
                    try:
                        rv = float(m.group(1))
                        strategy_used = "data-average-rating"
                    except ValueError:
                        pass

            # Strategy 4 – "X.XX out of 5" text pattern (title attrs, tooltips)
            if rv is None:
                m = _re.search(r'([0-4]\.\d+|5\.0+)\s+out\s+of\s+5', html)
                if m:
                    try:
                        rv = float(m.group(1))
                        snip = html[max(0, m.start() - 20):m.end() + 100]
                        rc_m = _re.search(r'([\d,]+)\s+(?:rating|fan)', snip)
                        if rc_m:
                            rc = int(rc_m.group(1).replace(",", ""))
                        strategy_used = "out-of-5"
                    except ValueError:
                        pass

            # Strategy 5 – "averageRating": X.XX anywhere in page JS/JSON
            if rv is None:
                m = _re.search(r'"averageRating"\s*:\s*([\d.]+)', html)
                if m:
                    try:
                        rv = float(m.group(1))
                        strategy_used = "averageRating-key"
                    except ValueError:
                        pass

            xbmc.log(
                f"[Letterboxd] '{slug}' len={len(html)} "
                f"strategy={strategy_used} rv={rv} rc={rc}",
                xbmc.LOGINFO,
            )

            # ── Fallback: CSI rating-histogram fragment ─────────────────────
            if rv is None:
                try:
                    hist = requests.get(
                        f"https://letterboxd.com/csi/film/{slug}/rating-histogram/",
                        headers={**_HEADERS, "X-Requested-With": "XMLHttpRequest",
                                 "Referer": f"{_LB_BASE}{slug}/"},
                        timeout=10,
                    )
                    if hist.status_code == 200:
                        hhtml = hist.text
                        m = _re.search(
                            r'([0-4]\.\d+|5\.0+)\s+out\s+of\s+5', hhtml
                        )
                        if m:
                            rv = float(m.group(1))
                            rc_m = _re.search(
                                r'([\d,]+)\s+(?:rating|fan)', hhtml
                            )
                            if rc_m:
                                rc = int(rc_m.group(1).replace(",", ""))
                            strategy_used = "csi-histogram"
                        xbmc.log(
                            f"[Letterboxd] CSI histogram '{slug}' "
                            f"status={hist.status_code} rv={rv}",
                            xbmc.LOGINFO,
                        )
                except Exception as he:
                    xbmc.log(
                        f"[Letterboxd] CSI histogram failed '{slug}': {he}",
                        xbmc.LOGINFO,
                    )

        except Exception as exc:
            xbmc.log(
                f"[Letterboxd] lb_rating fetch failed for '{slug}': {exc}",
                xbmc.LOGINFO,
            )
            return film

        if rv is None:
            xbmc.log(
                f"[Letterboxd] no rating found for '{slug}' "
                f"(all strategies exhausted)",
                xbmc.LOGINFO,
            )
            return film

        result = {
            "lb_community_rating": float(rv),   # 0.5–5.0
            "lb_rating_count": int(rc),
        }
        film.update(result)
        self._cache[cache_key] = {"ts": time.time(), "data": result}
        self._save_cache()

        xbmc.log(
            f"[Letterboxd] community rating for '{slug}': "
            f"{rv} ({rc} ratings) via {strategy_used}",
            xbmc.LOGINFO,
        )
        return film

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _search(self, title, year):
        params = {"api_key": self.api_key, "query": title, "language": "en-US"}
        if year:
            params["year"] = year

        try:
            resp = self._session.get(
                f"{_BASE}/search/movie", params=params, timeout=10
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception:
            return None

        if not results:
            return None

        # ── Pick the best match ───────────────────────────────────────────
        # Priority 1: exact title + exact year
        # Priority 2: exact title (any year, pick most popular)
        # Priority 3: first result (TMDB's own ranking)
        #
        # "Exact title" means the TMDB title or original_title equals the
        # query (case-insensitive).  This prevents e.g. "May" (2002) from
        # being replaced by "May December" (2023).
        title_lower = title.lower().strip()

        def _title_matches(r):
            return (
                r.get("title", "").lower().strip() == title_lower
                or r.get("original_title", "").lower().strip() == title_lower
            )

        exact = [r for r in results if _title_matches(r)]

        if exact and year:
            # Among exact-title hits, prefer the one whose release year matches
            year_str = str(year)
            year_hit = next(
                (r for r in exact if str(r.get("release_date", ""))[:4] == year_str),
                None,
            )
            top = year_hit or max(exact, key=lambda r: r.get("popularity", 0))
        elif exact:
            top = max(exact, key=lambda r: r.get("popularity", 0))
        else:
            top = results[0]

        xbmc.log(
            f"[Letterboxd/TMDB] search='{title}' year='{year}' "
            f"exact_hits={len(exact)} chosen='{top.get('title')}' "
            f"({str(top.get('release_date', ''))[:4]})",
            xbmc.LOGINFO,
        )

        tmdb_id = str(top.get("id", ""))

        # Fetch full details to get imdb_id
        imdb_id = ""
        try:
            detail = self._session.get(
                f"{_BASE}/movie/{tmdb_id}",
                params={"api_key": self.api_key},
                timeout=10,
            ).json()
            imdb_id = detail.get("imdb_id", "")
        except Exception:
            pass

        poster_path = top.get("poster_path", "")
        poster = f"{_IMG_BASE}{poster_path}" if poster_path else ""

        # vote_average comes from the detail call we already made
        tmdb_rating = None
        try:
            va = float(detail.get("vote_average") or 0)
            if va:
                tmdb_rating = va
        except (TypeError, ValueError):
            pass

        release_date = top.get("release_date", "")   # e.g. "1985-06-01"
        tmdb_year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""

        return {
            "tmdb_id":     tmdb_id,
            "imdb_id":     imdb_id,
            "poster":      poster,
            "description": top.get("overview", ""),
            "tmdb_rating": tmdb_rating,
            "year":        tmdb_year,
        }

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(name, year):
        return f"{name.lower().strip()}|{year}"

    def _get_cached(self, name, year):
        entry = self._cache.get(self._cache_key(name, year))
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > _CACHE_TTL:
            return None
        return entry.get("data")

    def _set_cached(self, name, year, data):
        with self._cache_lock:
            self._cache[self._cache_key(name, year)] = {"ts": time.time(), "data": data}
        self._save_cache()

    def _load_cache(self):
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_cache(self):
        with self._cache_lock:
            try:
                os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f)
            except Exception:
                pass
