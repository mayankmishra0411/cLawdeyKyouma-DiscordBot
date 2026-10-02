"""
lastfm.py

Minimal async Last.fm client for the recommender. Every method returns an empty
result on any failure (missing key, timeout, HTTP error) so callers can just skip
the source for that seed.
"""

import asyncio
import os
from typing import Optional

import aiohttp

API_URL = "https://ws.audioscrobbler.com/2.0/"
MAX_CONCURRENT = 4
REQUEST_SPACING_SEC = 0.25   # with 4 in flight, keeps us around ~4 req/s
TIMEOUT_SEC = 5
MAX_TAGS = 10

# User tags that say nothing about how a song sounds.
JUNK_TAGS = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "my top songs",
    "albums i own", "love", "loved", "awesome", "beautiful", "amazing", "best",
    "favorite songs", "favourite songs", "spotify", "under 2000 listeners", "music",
    "songs", "good", "cool", "fav", "favs",
}


class LastFM:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.getenv("LASTFM_API_KEY", "")
        self._session: Optional[aiohttp.ClientSession] = None
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, **params) -> dict:
        if not self.enabled:
            return {}
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC))
        query = {"method": method, "api_key": self.api_key, "format": "json", **params}
        async with self._sem:
            try:
                async with self._session.get(API_URL, params=query) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json(content_type=None)
                    return {} if "error" in data else data
            except Exception as e:
                print(f"Last.fm {method} failed: {e}")
                return {}
            finally:
                await asyncio.sleep(REQUEST_SPACING_SEC)

    # ---- similarity -----------------------------------------------------

    async def similar_tracks(self, artist: str, title: str, limit: int = 30) -> list[dict]:
        data = await self._call("track.getSimilar", artist=artist, track=title,
                                limit=limit, autocorrect=1)
        out = []
        for t in _as_list(data.get("similartracks", {}).get("track")):
            name, a = t.get("name"), (t.get("artist") or {}).get("name")
            if name and a:
                out.append({"artist": a, "title": name, "match": float(t.get("match") or 0)})
        return out

    async def similar_artists(self, artist: str, limit: int = 10) -> list[dict]:
        data = await self._call("artist.getSimilar", artist=artist, limit=limit, autocorrect=1)
        return [{"artist": a["name"], "match": float(a.get("match") or 0)}
                for a in _as_list(data.get("similarartists", {}).get("artist")) if a.get("name")]

    async def artist_top_tracks(self, artist: str, limit: int = 3) -> list[dict]:
        data = await self._call("artist.getTopTracks", artist=artist, limit=limit, autocorrect=1)
        return [{"artist": artist, "title": t["name"]}
                for t in _as_list(data.get("toptracks", {}).get("track")) if t.get("name")]

    # ---- tags -----------------------------------------------------------

    async def track_tags(self, artist: str, title: str) -> dict[str, float]:
        data = await self._call("track.getTopTags", artist=artist, track=title, autocorrect=1)
        return clean_tags(_as_list(data.get("toptags", {}).get("tag")), artist)

    async def artist_tags(self, artist: str) -> dict[str, float]:
        data = await self._call("artist.getTopTags", artist=artist, autocorrect=1)
        return clean_tags(_as_list(data.get("toptags", {}).get("tag")), artist)


def clean_tags(raw: list[dict], artist: str = "") -> dict[str, float]:
    """Last.fm tag counts (0..100) -> top tags as {tag: 0..1}, junk removed.

    Also drops tags with no letters ("2019", "-1001740215468") and tags that just
    repeat the artist's name, which say nothing about how the song sounds.
    """
    artist = artist.strip().lower()
    tags: dict[str, float] = {}
    for t in raw:
        name = (t.get("name") or "").strip().lower()
        count = float(t.get("count") or 0)
        if (name and count > 0 and name not in JUNK_TAGS and name not in tags
                and any(c.isalpha() for c in name) and name != artist):
            tags[name] = count
    top = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)[:MAX_TAGS]
    if not top:
        return {}
    peak = top[0][1]
    return {k: round(v / peak, 3) for k, v in top}


def _as_list(x) -> list:
    # Last.fm returns a bare object instead of a 1-element list.
    if x is None:
        return []
    return x if isinstance(x, list) else [x]
