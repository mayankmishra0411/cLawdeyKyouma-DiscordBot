"""
track_resolver.py

Handles finding and resolving playable audio from multiple free sources.

Key reality check baked into this design:
- YouTube & SoundCloud: yt-dlp can extract a direct, playable audio stream URL for free.
- Spotify & Apple Music: their public APIs only ever return METADATA (title, artist,
  album, track order, cover art) - neither service lets any third-party app stream
  raw audio, free or paid. That's a platform restriction, not a code limitation.
  So for Spotify/Apple Music links, we fetch the metadata, then search YouTube/
  SoundCloud for the best-matching audio and stream that instead. The "source" tag
  on each track tells you where the audio is actually coming from.
"""

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Track:
    title: str
    artist: str = ""
    duration: int = 0            # seconds
    source: str = ""             # "YouTube", "SoundCloud", "Spotify -> YouTube", etc.
    webpage_url: str = ""        # human-viewable page (for /nowplaying links etc.)
    stream_query: str = ""       # what we actually feed yt-dlp to get audio
    thumbnail: Optional[str] = None

    @property
    def display_name(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


# ---------------------------------------------------------------------------
# yt-dlp configuration
# ---------------------------------------------------------------------------

YDL_SEARCH_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "extract_flat": "in_playlist",
    "skip_download": True,
    # Bypass YouTube cloud IP bot detection:
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios"]
        }
    },
    # Reads the uploaded cookie file
    "cookiefile": "cookies.txt",
}

YDL_STREAM_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "source_address": "0.0.0.0",
    # Bypass YouTube cloud IP bot detection:
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios"]
        }
    },
    # Reads the uploaded cookie file
    "cookiefile": "cookies.txt",
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def _extract(query: str, opts: dict) -> dict:
    """Blocking yt-dlp call - run this inside run_in_executor."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


async def _run_extract(query: str, opts: dict) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _extract, query, opts)


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

async def search_youtube(query: str, limit: int = 3) -> list[Track]:
    info = await _run_extract(f"ytsearch{limit}:{query}", YDL_SEARCH_OPTS)
    entries = info.get("entries", []) or []
    tracks = []
    for e in entries:
        if not e:
            continue
        tracks.append(Track(    
            title=e.get("title", "Unknown"),
            duration=e.get("duration") or 0,
            source="YouTube",
            webpage_url=e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
            stream_query=e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
            thumbnail=e.get("thumbnail"),
        ))
    return tracks


async def resolve_youtube_playlist(url: str) -> list[Track]:
    opts = dict(YDL_SEARCH_OPTS)
    opts["noplaylist"] = False
    info = await _run_extract(url, opts)
    entries = info.get("entries", []) or [info]
    tracks = []
    for e in entries:
        if not e:
            continue
        vid_url = e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
        tracks.append(Track(
            title=e.get("title", "Unknown"),
            duration=e.get("duration") or 0,
            source="YouTube",
            webpage_url=vid_url,
            stream_query=vid_url,
            thumbnail=e.get("thumbnail"),
        ))
    return tracks


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

async def search_soundcloud(query: str, limit: int = 3) -> list[Track]:
    info = await _run_extract(f"scsearch{limit}:{query}", YDL_SEARCH_OPTS)
    entries = info.get("entries", []) or []
    tracks = []
    for e in entries:
        if not e:
            continue
        tracks.append(Track(
            title=e.get("title", "Unknown"),
            duration=e.get("duration") or 0,
            source="SoundCloud",
            webpage_url=e.get("webpage_url") or e.get("url", ""),
            stream_query=e.get("webpage_url") or e.get("url", ""),
            thumbnail=e.get("thumbnail"),
        ))
    return tracks


async def resolve_soundcloud_playlist(url: str) -> list[Track]:
    opts = dict(YDL_SEARCH_OPTS)
    opts["noplaylist"] = False
    info = await _run_extract(url, opts)
    entries = info.get("entries", []) or [info]
    tracks = []
    for e in entries:
        if not e:
            continue
        tracks.append(Track(
            title=e.get("title", "Unknown"),
            duration=e.get("duration") or 0,
            source="SoundCloud",
            webpage_url=e.get("webpage_url") or e.get("url", ""),
            stream_query=e.get("webpage_url") or e.get("url", ""),
            thumbnail=e.get("thumbnail"),
        ))
    return tracks


# ---------------------------------------------------------------------------
# Spotify (metadata only -> resolved to YouTube audio)
# ---------------------------------------------------------------------------

class SpotifyResolver:
    def __init__(self, client_id: str, client_secret: str, user_client: Optional[spotipy.Spotify] = None):
        self._public = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=client_id, client_secret=client_secret
            )
        )
        # Optional authenticated client for the bot owner's private playlists
        self._user_client = user_client

    def set_user_client(self, client: spotipy.Spotify):
        self._user_client = client

    @staticmethod
    def _client_for(url: str, private: bool, resolver: "SpotifyResolver"):
        return resolver._user_client if (private and resolver._user_client) else resolver._public

    async def _match_on_youtube(self, title: str, artist: str) -> Optional[Track]:
        query = f"{artist} {title} audio"
        results = await search_youtube(query, limit=1)
        if not results:
            return None
        yt = results[0]
        yt.title = title
        yt.artist = artist
        yt.source = "Spotify -> YouTube"
        return yt

    async def resolve_track(self, url: str) -> Optional[Track]:
        client = self._user_client or self._public
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.track, url)
        title = data["name"]
        artist = ", ".join(a["name"] for a in data["artists"])
        return await self._match_on_youtube(title, artist)

    async def resolve_album(self, url: str) -> list[Track]:
        client = self._user_client or self._public
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.album, url)
        items = data["tracks"]["items"]
        tasks = [
            self._match_on_youtube(t["name"], ", ".join(a["name"] for a in t["artists"]))
            for t in items
        ]
        results = await asyncio.gather(*tasks)
        return [t for t in results if t]

    async def resolve_playlist(self, url: str, private: bool = False) -> list[Track]:
        client = self._user_client if (private and self._user_client) else self._public
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.playlist_items, url)
        items = data["items"]
        # paginate
        while data.get("next"):
            data = await loop.run_in_executor(None, client.next, data)
            items.extend(data["items"])

        tasks = []
        for item in items:
            track = item.get("track")
            if not track:
                continue
            tasks.append(self._match_on_youtube(
                track["name"], ", ".join(a["name"] for a in track["artists"])
            ))
        results = await asyncio.gather(*tasks)
        return [t for t in results if t]


# ---------------------------------------------------------------------------
# URL detection helpers
# ---------------------------------------------------------------------------

SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_ALBUM_RE = re.compile(r"open\.spotify\.com/album/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)")
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)")
SOUNDCLOUD_RE = re.compile(r"soundcloud\.com")


def classify_url(url: str) -> str:
    if SPOTIFY_PLAYLIST_RE.search(url):
        return "spotify_playlist"
    if SPOTIFY_ALBUM_RE.search(url):
        return "spotify_album"
    if SPOTIFY_TRACK_RE.search(url):
        return "spotify_track"
    if YOUTUBE_RE.search(url):
        return "youtube"
    if SOUNDCLOUD_RE.search(url):
        return "soundcloud"
    return "search"


# ---------------------------------------------------------------------------
# Combined multi-platform search (for /search and plain-text /play queries)
# ---------------------------------------------------------------------------

# async def multi_search(query: str, per_source: int = 3) -> list[Track]:
#     yt_task = search_youtube(query, limit=per_source)
#     sc_task = search_soundcloud(query, limit=per_source)
#     yt_results, sc_results = await asyncio.gather(yt_task, sc_task, return_exceptions=True)

#     tracks: list[Track] = []
#     if isinstance(yt_results, list):
#         tracks.extend(yt_results)
#     if isinstance(sc_results, list):
#         tracks.extend(sc_results)
#     return tracks
async def multi_search(query: str, yt_limit: int = 3, sc_limit: int = 5) -> list[Track]:
    """Searches YouTube first. If no results or an error occurs, falls back to SoundCloud."""
    
    # Primary attempt
    try:
        yt_results = await search_youtube(query, limit=yt_limit)
        if yt_results:
            return yt_results
    except Exception as e:
        print(f"YouTube search failed, falling back: {e}")

    # Fallback attempt
    try:
        sc_results = await search_soundcloud(query, limit=sc_limit)
        return sc_results
    except Exception as e:
        print(f"SoundCloud fallback failed: {e}")
        
    return []


async def get_stream_url(stream_query: str) -> str:
    """Resolve the final direct audio stream URL right before playback
    (stream URLs expire, so we re-resolve at play time, not at search time)."""
    info = await _run_extract(stream_query, YDL_STREAM_OPTS)
    if "url" in info:
        return info["url"]
    # playlist-shaped single result
    return info["entries"][0]["url"]
