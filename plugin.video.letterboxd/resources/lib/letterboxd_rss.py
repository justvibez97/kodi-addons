"""
Letterboxd HTML scraper — no API key required.

Scrapes public Letterboxd profile pages directly.
Works for public profiles; poster/plot enrichment is handled by TMDB.
"""
import html
import re
import urllib.parse

import requests
import xbmc

_BASE = "https://letterboxd.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def _attr(tag, name):
    """Extract a single attribute value from an HTML tag string."""
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return html.unescape(m.group(1)) if m else ""


def _has_next(page_html, current_page):
    # Rely only on Letterboxd's actual pagination element class rather than
    # checking for /page/N/ anywhere in the HTML, which produces false positives
    # (the URL pattern appears in film links, other list references, etc.).
    return 'class="next "' in page_html or 'class="next"' in page_html


class LetterboxdRSSClient:
    def __init__(self, username):
        self.username = username
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        # Prime the session with a cookie-gathering visit
        try:
            self._session.get(_BASE + "/", timeout=10)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API — same interface as LetterboxdAPIClient
    # ------------------------------------------------------------------

    def get_official_lists_rss(self):
        """
        Fetch ALL official Letterboxd lists by walking every page of the RSS
        feed at https://letterboxd.com/official/rss/.

        Pagination strategy (tried in order):
          1. Look for <atom:link rel="next" href="..."/> in the parsed XML.
          2. Fall back to appending ?page=N (N = 2, 3, …) until a page
             returns no new list items or raises an HTTP error.

        Returns a list of dicts: {name, path, creator}.
        Items that are stories (no /list/ in URL) are skipped.
        Duplicate paths are silently dropped.
        """
        import xml.etree.ElementTree as ET

        ATOM_NS = "http://www.w3.org/2005/Atom"

        def _parse_page(content):
            """Return (items_found, next_url_or_None) for one RSS page."""
            page_lists = []
            next_url = None
            try:
                root = ET.fromstring(content)

                # Look for <atom:link rel="next"> anywhere in the document
                for el in root.iter(f"{{{ATOM_NS}}}link"):
                    if el.get("rel") == "next":
                        next_url = el.get("href", "").strip() or None
                        break

                for item in root.iter("item"):
                    title_el = item.find("title")
                    link_el  = item.find("link")
                    if title_el is None or link_el is None:
                        continue
                    link  = (link_el.text or "").strip()
                    title = (title_el.text or "").strip()
                    if "/list/" not in link:
                        continue
                    pm = re.search(
                        r"letterboxd\.com(/[^/?#]+/list/[^/?#]+/)", link
                    )
                    if not pm:
                        continue
                    path = pm.group(1)
                    name = html.unescape(title)
                    if name:
                        page_lists.append(
                            {"name": name, "path": path, "creator": "official"}
                        )
            except Exception as exc:
                xbmc.log(
                    f"[Letterboxd] official RSS parse error: {exc}", xbmc.LOGINFO
                )
            return page_lists, next_url

        base_rss_url = f"{_BASE}/official/rss/"
        lists = []
        seen_paths = set()
        page_num = 1
        next_url = base_rss_url          # first URL to fetch
        prev_url = None                  # used as Referer on subsequent pages
        use_atom_next = None             # None = undecided, True/False once known

        while next_url:
            try:
                extra_headers = (
                    {"Referer": prev_url} if prev_url else {}
                )
                resp = self._session.get(next_url, timeout=15, headers=extra_headers)
                resp.raise_for_status()
                xbmc.log(
                    f"[Letterboxd] official RSS p{page_num} "
                    f"fetched {len(resp.content)} bytes from {next_url}",
                    xbmc.LOGINFO,
                )
            except Exception as exc:
                xbmc.log(
                    f"[Letterboxd] official RSS p{page_num} fetch failed "
                    f"({next_url}): {exc}",
                    xbmc.LOGINFO,
                )
                break

            page_lists, atom_next = _parse_page(resp.content)

            # Decide once which pagination style the feed supports
            if use_atom_next is None:
                use_atom_next = atom_next is not None

            # Deduplicate and accumulate
            new_count = 0
            for entry in page_lists:
                if entry["path"] not in seen_paths:
                    seen_paths.add(entry["path"])
                    lists.append(entry)
                    new_count += 1

            xbmc.log(
                f"[Letterboxd] official RSS p{page_num}: "
                f"{len(page_lists)} items, {new_count} new",
                xbmc.LOGINFO,
            )

            # Stop if this page was empty (no list items at all)
            if not page_lists:
                break

            prev_url = next_url          # becomes Referer on the next fetch

            # Advance to the next page
            if use_atom_next:
                next_url = atom_next  # None if no more pages
            else:
                # Fallback: Letterboxd uses path-style pagination /page/N/
                page_num += 1
                next_url = f"{base_rss_url}page/{page_num}/"
                # If the page returned 0 new items despite having items, we
                # may have looped; stop to be safe.
                if new_count == 0:
                    break

            page_num += 1 if use_atom_next else 0  # atom path already bumped above

        xbmc.log(
            f"[Letterboxd] official RSS total: {len(lists)} lists across pages",
            xbmc.LOGINFO,
        )
        return lists

    def get_watchlist(self, page=1, page_size=20):
        url = f"{_BASE}/{self.username}/watchlist/page/{page}/"
        page_html = self._get(url)
        films = self._parse_poster_grid(page_html)
        return films, _has_next(page_html, page)

    def get_diary(self, page=1, page_size=20):
        url = f"{_BASE}/{self.username}/films/diary/page/{page}/"
        page_html = self._get(url)
        films = self._parse_diary(page_html)
        return films, _has_next(page_html, page)

    def get_lists(self):
        url = f"{_BASE}/{self.username}/lists/"
        page_html = self._get(url)
        lists = []
        for m in re.finditer(
            r'<h2[^>]*class="[^"]*title[^"]*"[^>]*>.*?'
            r'<a\s+href="/([^/]+)/list/([^/"]+)/?"[^>]*>([^<]+)</a>',
            page_html, re.DOTALL
        ):
            slug = m.group(2)
            name = html.unescape(m.group(3).strip())
            lists.append({
                "id": f"{self.username}/{slug}",
                "name": name,
                "film_count": 0,
                "description": "",
            })
        return lists

    def get_list_entries(self, list_id, page=1, page_size=20):
        # list_id is "username/slug"
        url = f"{_BASE}/{list_id}/page/{page}/"
        page_html = self._get(url)
        films = self._parse_poster_grid(page_html)
        return films, _has_next(page_html, page)

    def search(self, query, page=1, page_size=20):
        encoded = urllib.parse.quote(query, safe="")
        url = f"{_BASE}/search/films/{encoded}/page/{page}/"
        page_html = self._get(url)
        films = self._parse_poster_grid(page_html)
        return films, _has_next(page_html, page)

    def search_lists(self, query, page=1):
        """Search Letterboxd lists by keyword. Returns (lists, has_next)."""
        encoded = urllib.parse.quote(query, safe="")
        url = f"{_BASE}/search/lists/{encoded}/page/{page}/"
        page_html = self._get(url)
        lists = self._parse_list_cards(page_html)
        xbmc.log(
            f"[Letterboxd] search_lists '{query}' p{page} "
            f"found {len(lists)} lists has_next={_has_next(page_html, page)}",
            xbmc.LOGINFO,
        )
        return lists, _has_next(page_html, page)

    def get_genre_films(self, genre_slug, page=1):
        """
        Fetch popular films for a Letterboxd genre.

        genre_slug must be a valid Letterboxd genre slug, e.g. 'horror',
        'science-fiction', 'animation'.  Returns (films, has_next).
        """
        url = f"{_BASE}/films/genre/{genre_slug}/page/{page}/"
        page_html = self._get(url)
        films = self._parse_poster_grid(page_html)
        return films, _has_next(page_html, page)

    def get_public_lists_at(self, base_url, page=1):
        """
        Scrape a Letterboxd list-discovery page with pagination support.

        Page 1 is always fetched via the bare base_url (avoids 403 that
        Letterboxd returns for /page/1/ on some paths).  Page 2+ appends
        /page/N/ to the base URL and sends a Referer header pointing to the
        previous page to bypass Letterboxd's hotlink-style 403.

        Returns (lists, has_next).
        """
        base = base_url.rstrip("/")
        if page > 1:
            fetch_url = f"{base}/page/{page}/"
            prev_url  = f"{base}/page/{page - 1}/" if page > 2 else base_url
            extra_headers = {"Referer": prev_url}
        else:
            fetch_url     = base_url
            extra_headers = {}

        page_html = self._get(fetch_url, extra_headers=extra_headers)
        lists = self._parse_list_cards(page_html)
        has_next = _has_next(page_html, page)
        xbmc.log(
            f"[Letterboxd] list-discovery '{fetch_url}' p{page} "
            f"found {len(lists)} lists has_next={has_next}",
            xbmc.LOGINFO,
        )
        return lists, has_next

    def get_public_list_films(self, path, lb_page=1, offset=0, limit=20):
        """
        Scrape films from a public list with virtual pagination.

        Letterboxd sometimes puts 100+ films on a single page.  We fetch one
        Letterboxd page (lb_page) but return only `limit` films starting at
        `offset` so each Kodi page stays small and enrichment stays fast.

        Returns (films_slice, has_next, next_lb_page, next_offset).
        """
        url = f"{_BASE}{path}page/{lb_page}/"
        page_html = self._get(url)
        all_films = self._parse_poster_grid(page_html)

        end = offset + limit
        sliced = all_films[offset:end]

        if end < len(all_films):
            # More films remaining on this Letterboxd page
            return sliced, True, lb_page, end
        else:
            # Exhausted this Letterboxd page — check for a real next page
            return sliced, _has_next(page_html, lb_page), lb_page + 1, 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_list_cards(self, page_html):
        """
        Extract list entries from any Letterboxd list-discovery page.

        Each list has TWO anchors pointing to the same href:
          1. class="poster-list-link" — contains poster thumbnails, no text
          2. A plain <a>Title</a>     — contains the actual list title

        Strategy: find each href match, skip to the closing '>' of the
        opening tag to read inner content (not raw attributes), skip anchors
        with empty text, and keep the first anchor with a real name per path.
        """
        path_to_entry = {}

        for m in re.finditer(r'href="(/([^/"]+)/list/([^/"]+)/)"', page_html):
            path = m.group(1)
            creator = m.group(2)

            if path in path_to_entry:
                continue  # already found a good name for this path

            # Advance past the remaining attributes to the closing '>'
            tag_end = page_html.find(">", m.end())
            if tag_end < 0:
                continue

            end_a = page_html.find("</a>", tag_end)
            if end_a < 0:
                continue

            inner = page_html[tag_end + 1:end_a]
            name = html.unescape(re.sub(r"<[^>]+>", " ", inner))
            name = re.sub(r"\s+", " ", name).strip()

            if len(name) < 3:
                # No meaningful text (poster-thumbnail anchor) — let the next
                # occurrence of this path supply the title.
                continue

            path_to_entry[path] = {"name": name, "creator": creator, "path": path}

        return list(path_to_entry.values())

    def _get(self, url, extra_headers=None):
        hdrs = extra_headers or {}
        resp = self._session.get(url, timeout=15, headers=hdrs)
        resp.raise_for_status()
        page_html = resp.text
        xbmc.log(f"[Letterboxd] fetched {url} ({len(page_html)} chars)", xbmc.LOGINFO)
        return page_html

    def _parse_poster_grid(self, page_html):
        """
        Extract films from Letterboxd's poster-grid pages.

        Two HTML structures are handled:

        Pattern A — user content pages (watchlist, diary, list entries):
          <div class="film-poster" data-target-link="/film/<slug>/"
               data-target-link-text="Title">
            <img alt="Title">
          </div>

        Pattern B — genre / browse pages:
          <div class="film-poster" ...>
            <a href="/film/<slug>/" class="frame">
              <img alt="Title">
              <span class="frame-title">Title</span>
            </a>
          </div>

        Pattern B is only attempted when Pattern A yields nothing, so the
        two patterns never double-count films on the same page.
        """
        films = []
        seen = set()

        # ── Pattern A ────────────────────────────────────────────────────────
        candidates = [
            (m.group(1), m.start())
            for m in re.finditer(r'data-target-link="/film/([^/"]+)/"', page_html)
        ]

        # ── Pattern B (fallback) ─────────────────────────────────────────────
        if not candidates:
            for m in re.finditer(r'href="/film/([^/"]+)/"', page_html):
                # Only count this link if a film-poster element appears in the
                # ~400 chars before it (i.e. the link is *inside* a poster div).
                pre = page_html[max(0, m.start() - 400):m.start()]
                if "film-poster" in pre or "poster-container" in pre:
                    candidates.append((m.group(1), m.start()))

        # ── Name + year extraction (shared) ──────────────────────────────────
        for slug, pos in candidates:
            if slug in seen:
                continue
            seen.add(slug)

            ctx_start = max(0, pos - 200)
            ctx_end   = min(len(page_html), pos + 800)
            ctx = page_html[ctx_start:ctx_end]

            name_m = re.search(r'data-target-link-text="([^"]+)"', ctx)
            if not name_m:
                name_m = re.search(r'<img[^>]+alt="([^"]+)"', ctx)
            if not name_m:
                name_m = re.search(r'class="[^"]*visually-hidden[^"]*">([^<]+)<', ctx)
            if not name_m:
                name_m = re.search(r'class="frame-title">([^<]+)<', ctx)

            if not name_m:
                xbmc.log(
                    f"[Letterboxd] no name for slug={slug}, ctx={ctx[:200]}",
                    xbmc.LOGINFO,
                )
                continue

            name = html.unescape(name_m.group(1).strip())
            if not name or name.lower() in ("", "poster"):
                continue

            year_m = re.search(r'data-film-release-year="([^"]+)"', ctx)
            year = year_m.group(1) if year_m else ""

            films.append(self._film(slug, name, year))

        xbmc.log(f"[Letterboxd] _parse_poster_grid found {len(films)} films", xbmc.LOGINFO)
        return films

    def _parse_diary(self, page_html):
        """
        Extract films from the diary page.
        Each entry is a <tr class="diary-entry-row"> containing a
        .film-poster div plus a rating and watched date.
        """
        films = []
        for row in re.findall(
            r'<tr[^>]+class="[^"]*diary-entry-row[^"]*"[^>]*>(.*?)</tr>',
            page_html, re.DOTALL
        ):
            # Film info from poster div
            tag_m = re.search(r'<div[^>]+class="[^"]*film-poster[^"]*"[^>]+>', row)
            if not tag_m:
                continue
            tag = tag_m.group(0)
            # data-film-slug was removed; extract slug from data-target-link="/film/<slug>/"
            slug = _attr(tag, "data-film-slug")
            if not slug:
                tl = _attr(tag, "data-target-link")
                lm = re.search(r"/film/([^/\"]+)/", tl)
                slug = lm.group(1) if lm else ""
            name = _attr(tag, "data-target-link-text")
            if not name:
                img_m = re.search(r'<img[^>]+alt="([^"]+)"', row)
                if img_m:
                    name = html.unescape(img_m.group(1))
            year = _attr(tag, "data-film-release-year")
            if not slug:
                continue

            # Rating: Letterboxd stores it as a CSS class like "rated-8" (= 4 stars)
            rating = None
            r_m = re.search(r'rated-(\d+)', row)
            if r_m:
                rating = int(r_m.group(1)) / 2.0  # convert to 0-5 scale

            # Watched date from <td class="td-day"><a href="...YYYY/MM/DD...">
            watched = ""
            d_m = re.search(r'/(\d{4}/\d{2}/\d{2})/', row)
            if d_m:
                watched = d_m.group(1).replace("/", "-")

            film = self._film(slug, name, year)
            film["rating"] = rating
            film["watched_date"] = watched
            films.append(film)
        return films

    @staticmethod
    def _film(slug, name, year):
        return {
            "id": slug,
            "name": name,
            "year": year,
            "poster": "",
            "imdb_id": "",
            "tmdb_id": "",
            "description": "",
            "rating": None,
            "watched_date": "",
        }
