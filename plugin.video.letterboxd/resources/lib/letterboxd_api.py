"""
Letterboxd API v0 client.

Each request is signed with HMAC-SHA256 using the API key/secret.
User actions require OAuth2 (password grant) first.

Apply for credentials at: https://letterboxd.com/api-beta/
"""
import hashlib
import hmac
import json
import time
import urllib.parse
import uuid

import requests

BASE_URL = "https://api.letterboxd.com/api/v0"


class AuthError(Exception):
    pass


class LetterboxdAPIClient:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = None
        self._member_id = None
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Kodi-Letterboxd/1.0"

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self, username, password):
        """Obtain an OAuth2 access token via password grant."""
        form = {
            "grant_type": "password",
            "username": username,
            "password": password,
        }
        data = self._request("POST", "/auth/token", body=form, form_encoded=True)
        self.access_token = data["access_token"]
        return self.access_token

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_watchlist(self, page=1, page_size=20):
        member_id = self._get_member_id()
        params = {"perPage": page_size}
        cursor = self._offset_cursor(page, page_size)
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", f"/member/{member_id}/watchlist", params=params)
        films = [self._parse_film(f) for f in data.get("items", [])]
        return films, data.get("next") is not None

    def get_diary(self, page=1, page_size=20):
        member_id = self._get_member_id()
        params = {"perPage": page_size, "where": "Diary"}
        cursor = self._offset_cursor(page, page_size)
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", f"/member/{member_id}/log-entries", params=params)
        films = []
        for entry in data.get("items", []):
            film = self._parse_film(entry.get("film", {}))
            film["rating"] = entry.get("rating")
            details = entry.get("diaryDetails", {})
            film["watched_date"] = details.get("diaryDate", "")
            films.append(film)
        return films, data.get("next") is not None

    def get_lists(self):
        member_id = self._get_member_id()
        data = self._request("GET", f"/member/{member_id}/lists", params={"perPage": 100})
        return [
            {
                "id": lst["id"],
                "name": lst["name"],
                "film_count": lst.get("filmCount", 0),
                "description": lst.get("description", ""),
            }
            for lst in data.get("items", [])
        ]

    def get_list_entries(self, list_id, page=1, page_size=20):
        params = {"perPage": page_size}
        cursor = self._offset_cursor(page, page_size)
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", f"/list/{list_id}/entries", params=params)
        films = [self._parse_film(e.get("film", {})) for e in data.get("items", [])]
        return films, data.get("next") is not None

    def search(self, query, page=1, page_size=20):
        params = {
            "input": query,
            "perPage": page_size,
            "searchMethod": "FullText",
            "include": "FilmSearchItem",
        }
        cursor = self._offset_cursor(page, page_size)
        if cursor:
            params["cursor"] = cursor
        data = self._request("GET", "/search", params=params)
        films = [
            self._parse_film(item.get("film", {}))
            for item in data.get("items", [])
            if item.get("type") == "FilmSearchItem"
        ]
        return films, data.get("next") is not None

    def log_entry(self, film_id, rating=None, review=None, watched_date=None):
        """Add or update a diary entry."""
        body = {"film": {"id": film_id}}
        if rating is not None:
            body["rating"] = rating
        if review:
            body["review"] = {"text": review}
        if watched_date:
            body["diaryDetails"] = {"diaryDate": watched_date}
        return self._request("POST", "/log-entry", body=body)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_member_id(self):
        if not self.access_token:
            raise AuthError("Not authenticated — call authenticate() first")
        if not self._member_id:
            me = self._request("GET", "/me")
            self._member_id = me["id"]
        return self._member_id

    @staticmethod
    def _offset_cursor(page, page_size):
        if page <= 1:
            return None
        return f"start={(page - 1) * page_size}"

    def _sign(self, method, url, body_str=""):
        """Return signed URL with apikey/nonce/timestamp/signature appended."""
        nonce = str(uuid.uuid4())
        timestamp = str(int(time.time()))

        sep = "&" if "?" in url else "?"
        url_with_auth = (
            f"{url}{sep}apikey={urllib.parse.quote(self.api_key)}"
            f"&nonce={nonce}&timestamp={timestamp}"
        )

        body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        salted = f"{method.upper()}\x00{url_with_auth}\x00{body_hash}"

        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            salted.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return f"{url_with_auth}&signature={sig}"

    def _request(self, method, endpoint, params=None, body=None, form_encoded=False):
        url = BASE_URL + endpoint
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean)}"

        body_str = ""
        headers = {}

        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        if body is not None:
            if form_encoded:
                body_str = urllib.parse.urlencode(body)
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body_str = json.dumps(body)
                headers["Content-Type"] = "application/json"

        signed_url = self._sign(method, url, body_str)

        resp = self._session.request(
            method,
            signed_url,
            data=body_str.encode("utf-8") if body_str else None,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_film(film):
        poster = ""
        sizes = film.get("poster", {}).get("sizes", [])
        if sizes:
            largest = max(sizes, key=lambda s: s.get("width", 0))
            poster = largest.get("url", "")

        ext = film.get("externalIds", {})
        return {
            "id": film.get("id", ""),
            "name": film.get("name", "Unknown"),
            "year": film.get("releaseYear"),
            "poster": poster,
            "imdb_id": ext.get("imdb", ""),
            "tmdb_id": ext.get("tmdb", ""),
            "description": film.get("description", ""),
            "rating": film.get("rating"),
            "watched_date": "",
        }
