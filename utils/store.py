"""
store.py

SQLite persistence for the recommender: known tracks, play history, similarity
cache and per-guild settings. One shared connection; blocking calls are run in the
default executor via the async wrappers so they never stall the event loop.
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
  key TEXT PRIMARY KEY,
  artist TEXT, title TEXT, video_id TEXT, duration INT,
  tags TEXT,
  info_fetched_at INT
);
CREATE INDEX IF NOT EXISTS tracks_video ON tracks(video_id);
CREATE TABLE IF NOT EXISTS plays (
  id INTEGER PRIMARY KEY,
  guild_id INT, session_id TEXT, track_key TEXT,
  requested_by INT,
  is_recommendation INT,
  started_at INT, listened_sec INT, duration INT,
  outcome TEXT,
  listeners INT,
  listener_ids TEXT
);
CREATE INDEX IF NOT EXISTS plays_guild_time ON plays(guild_id, started_at);
CREATE INDEX IF NOT EXISTS plays_session ON plays(session_id);
CREATE TABLE IF NOT EXISTS sim_cache (
  source TEXT, seed TEXT,
  payload TEXT,
  fetched_at INT,
  PRIMARY KEY (source, seed)
);
CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id INT PRIMARY KEY, autoplay INT DEFAULT 0, explore REAL DEFAULT 0.2
);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- tracks -------------------------------------------------------

    def upsert_track(self, key: str, artist: str, title: str, video_id: str = "",
                     duration: int = 0, tags: Optional[dict] = None,
                     info_fetched_at: Optional[int] = None):
        """Insert or refresh a track. Existing tags/info timestamps are kept unless new ones are given."""
        self._exec(
            """INSERT INTO tracks (key, artist, title, video_id, duration, tags, info_fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 artist = excluded.artist,
                 title = excluded.title,
                 video_id = COALESCE(NULLIF(excluded.video_id, ''), tracks.video_id),
                 duration = COALESCE(NULLIF(excluded.duration, 0), tracks.duration),
                 tags = COALESCE(excluded.tags, tracks.tags),
                 info_fetched_at = COALESCE(excluded.info_fetched_at, tracks.info_fetched_at)""",
            (key, artist, title, video_id or "", duration or 0,
             json.dumps(tags) if tags is not None else None, info_fetched_at),
        )

    def get_track(self, key: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM tracks WHERE key = ?", (key,))
        return _track_row(rows[0]) if rows else None

    def get_track_by_video(self, video_id: str) -> Optional[dict]:
        """Video-id alias lookup: finds the canonical track for a YouTube upload."""
        if not video_id:
            return None
        rows = self._query("SELECT * FROM tracks WHERE video_id = ? LIMIT 1", (video_id,))
        return _track_row(rows[0]) if rows else None

    # ---- plays --------------------------------------------------------

    def insert_play(self, guild_id: int, session_id: str, track_key: str,
                    requested_by: Optional[int], is_recommendation: bool,
                    duration: int, listener_ids: list[int],
                    started_at: Optional[int] = None) -> int:
        cur = self._exec(
            """INSERT INTO plays (guild_id, session_id, track_key, requested_by, is_recommendation,
                                  started_at, duration, listeners, listener_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, session_id, track_key, requested_by, int(is_recommendation),
             int(started_at if started_at is not None else time.time()), duration or 0,
             len(listener_ids), json.dumps(listener_ids)),
        )
        return cur.lastrowid

    def finish_play(self, play_id: int, listened_sec: int, outcome: str):
        self._exec("UPDATE plays SET listened_sec = ?, outcome = ? WHERE id = ?",
                   (int(listened_sec), outcome, play_id))

    def get_plays(self, guild_id: int, since: int = 0) -> list[dict]:
        rows = self._query(
            "SELECT * FROM plays WHERE guild_id = ? AND started_at >= ? ORDER BY started_at, id",
            (guild_id, since))
        return [dict(r) for r in rows]

    # ---- similarity cache ---------------------------------------------

    def cache_get(self, source: str, seed: str, max_age_sec: int) -> Optional[list]:
        rows = self._query("SELECT payload, fetched_at FROM sim_cache WHERE source = ? AND seed = ?",
                           (source, seed))
        if not rows or time.time() - rows[0]["fetched_at"] > max_age_sec:
            return None
        return json.loads(rows[0]["payload"])

    def cache_put(self, source: str, seed: str, payload: list):
        self._exec("INSERT OR REPLACE INTO sim_cache (source, seed, payload, fetched_at) VALUES (?, ?, ?, ?)",
                   (source, seed, json.dumps(payload), int(time.time())))

    # ---- guild settings -----------------------------------------------

    def get_guild_settings(self, guild_id: int) -> dict:
        rows = self._query("SELECT autoplay, explore FROM guild_settings WHERE guild_id = ?", (guild_id,))
        if not rows:
            return {"autoplay": False, "explore": 0.2}
        return {"autoplay": bool(rows[0]["autoplay"]), "explore": rows[0]["explore"]}

    def set_guild_setting(self, guild_id: int, **values: Any):
        current = self.get_guild_settings(guild_id)
        current.update(values)
        self._exec("INSERT OR REPLACE INTO guild_settings (guild_id, autoplay, explore) VALUES (?, ?, ?)",
                   (guild_id, int(bool(current["autoplay"])), float(current["explore"])))

    # ---- async helper -------------------------------------------------

    async def run(self, method: str, *args, **kwargs):
        """Run any Store method off the event loop: `await store.run("insert_play", ...)`."""
        loop = asyncio.get_running_loop()
        fn = getattr(self, method)
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _track_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"]) if d["tags"] else {}
    return d


SKIP_EARLY_SEC = 30
SKIP_SIGNAL_SEC = 60   # skips after this long carry no taste signal


def classify_outcome(listened_sec: float, skip_requested: bool, stop_requested: bool,
                     errored: bool) -> str:
    """Map how a track ended to a plays.outcome value."""
    if stop_requested:
        return "stopped"
    if skip_requested:
        if listened_sec < SKIP_EARLY_SEC:
            return "skipped_early"
        if listened_sec < SKIP_SIGNAL_SEC:
            return "skipped_late"
        return "skipped_neutral"
    if errored:
        return "error"
    return "completed"
