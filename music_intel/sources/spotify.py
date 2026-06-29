"""
music_intel.sources.spotify — Spotify Web API client (client-credentials flow).

Provides artist popularity (momentum signal) and 2026 release activity.
Never raises out of public methods — returns None/{}/empty on any error or
missing creds (graceful degrade, consistent with other music_intel sources).

Env vars: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"
_ALBUMS_URL = "https://api.spotify.com/v1/artists/{id}/albums"

# Refresh the cached token this many seconds before expiry to avoid races.
_REFRESH_BUFFER = 60


class SpotifyClient:
    """Spotify Web API client using client-credentials flow.

    Args:
        http: An httpx.AsyncClient (or compatible mock).
        client_id: Spotify app client ID. Falls back to SPOTIFY_CLIENT_ID env var.
        client_secret: Spotify app secret. Falls back to SPOTIFY_CLIENT_SECRET env var.
        now_fn: Clock callable (default time.monotonic). Inject for tests.
    """

    def __init__(
        self,
        http,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        now_fn=time.monotonic,
    ) -> None:
        self._http = http
        self._cid = client_id if client_id is not None else os.environ.get("SPOTIFY_CLIENT_ID", "")
        self._sec = client_secret if client_secret is not None else os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        self._now = now_fn
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0.0

    @property
    def enabled(self) -> bool:
        """True iff both client_id and client_secret are non-empty."""
        return bool(self._cid and self._sec)

    async def _token(self) -> Optional[str]:
        """Return a cached bearer token, refreshing when within REFRESH_BUFFER of expiry.

        Returns None if disabled or if the token request fails.
        """
        if not self.enabled:
            return None
        if self._cached_token and self._now() < self._token_expiry - _REFRESH_BUFFER:
            return self._cached_token
        try:
            resp = await self._http.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._cid, self._sec),
            )
            body = resp.json()
            self._cached_token = body["access_token"]
            self._token_expiry = self._now() + body["expires_in"]
            return self._cached_token
        except Exception as exc:  # noqa: BLE001
            logger.warning("[spotify] Token fetch failed: %s", exc)
            return None

    async def search_artist(self, name: str) -> Optional[dict]:
        """Search for an artist by name.

        Returns a dict with keys ``id``, ``name``, ``popularity``, ``followers``
        (follower count as int), or None if not found or on any error.
        """
        if not self.enabled:
            return None
        token = await self._token()
        if token is None:
            return None
        try:
            resp = await self._http.get(
                _SEARCH_URL,
                params={"q": name, "type": "artist", "limit": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
            items = resp.json()["artists"]["items"]
            if not items:
                return None
            item = items[0]
            return {
                "id": item["id"],
                "name": item["name"],
                "popularity": item["popularity"],
                "followers": item["followers"]["total"],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[spotify] search_artist(%r) failed: %s", name, exc)
            return None

    async def release_momentum(self, artist_id: str, year: str = "2026") -> dict:
        """Count releases and albums for an artist in a given year.

        Returns a dict with:
            ``releases``: total items whose release_date starts with *year*.
            ``albums``: subset where album_type == "album".
            ``latest``: the lexicographically greatest release_date among matches,
                        or None if no matches. ISO dates sort correctly as strings;
                        partial dates (e.g. "2026") are treated as-is for max.

        Returns {} if not enabled or on any error.
        """
        if not self.enabled:
            return {}
        token = await self._token()
        if token is None:
            return {}
        url = _ALBUMS_URL.format(id=artist_id)
        try:
            resp = await self._http.get(
                url,
                params={"include_groups": "album,single", "market": "US", "limit": 50},
                headers={"Authorization": f"Bearer {token}"},
            )
            items = resp.json()["items"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[spotify] release_momentum(%r) failed: %s", artist_id, exc)
            return {}

        year_items = [i for i in items if i.get("release_date", "").startswith(year)]
        releases = len(year_items)
        albums = sum(1 for i in year_items if i.get("album_type") == "album")
        dates = [i["release_date"] for i in year_items if i.get("release_date")]
        latest = max(dates) if dates else None

        return {"releases": releases, "albums": albums, "latest": latest}
