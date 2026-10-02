"""
recommender.py

Queue-aware song recommendations (spec §5):

  seeds (current / queue / history)  ->  candidates per seed (YouTube Mix, Last.fm)
  -> weighted score  ->  tag bonus for the top few  ->  filters + diversity  ->  Tracks

The scoring / filtering half is pure (no I/O) so it can be unit tested with fixtures;
`Recommender` does the network + cache work around it.
"""

import asyncio
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from utils.lastfm import LastFM
from utils.metadata import normalize_key, parse_artist_title
from utils.store import Store
from utils.track_resolver import Track, fetch_youtube_mix, search_youtube

W_YTMIX = float(os.getenv("REC_W_YTMIX", "1.0"))
W_LASTFM = float(os.getenv("REC_W_LASTFM", "1.0"))
TAG_ALPHA = float(os.getenv("REC_TAG_ALPHA", "0.5"))
RECENT_HOURS = float(os.getenv("REC_RECENT_HOURS", "3"))
DEFAULT_EXPLORE = float(os.getenv("REC_EXPLORE", "0.2"))

YTMIX_TTL = 7 * 86400
LASTFM_SIM_TTL = 30 * 86400
TAGS_TTL = 90 * 86400
MAX_FETCH_SEEDS = 4          # seeds we fetch candidates for: bounds a cold batch to ~8 calls
TAG_TOP_N = 12               # only the top pre-ranked candidates get tag lookups
LASTFM_MIN_CONFIDENCE = 0.5
EXPLORE_RANKS = (5, 15)      # explore picks come from rank 6..15
ARTIST_GAP = int(os.getenv("REC_ARTIST_GAP", "2"))   # a pick's artist can't match any of the N songs before it

NON_MUSIC_RE = re.compile(
    r"full album|\b1 hour\b|\b10 hours\b|podcast|mashup compilation|jukebox|nonstop|non stop|"
    r"reaction|interview",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Seed:
    track: Track
    weight: float              # negative = "less like this"

    @property
    def label(self) -> str:
        t = self.track
        return t.canonical_title or t.title


@dataclass
class Candidate:
    key: str
    artist: str = ""
    title: str = ""
    raw_title: str = ""
    channel: str = ""
    video_id: str = ""
    duration: int = 0
    score: float = 0.0
    contributions: dict = field(default_factory=dict)   # seed label -> signed contribution
    sources: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def build_seeds(current: Optional[Track], queue: Sequence[Track],
                history: Sequence[Track], outcomes: Sequence[str]) -> list[Seed]:
    """Spec §5.1 seed weights. `history`/`outcomes` are oldest-first (deque order)."""
    seeds: list[Seed] = []
    if current:
        seeds.append(Seed(current, 1.0))
    for t, w in zip(list(queue)[:3], (0.8, 0.6, 0.4)):
        seeds.append(Seed(t, w))
    recent = list(zip(history, outcomes))[::-1][:5]
    for k, (t, outcome) in enumerate(recent, start=1):
        decay = 0.7 ** k
        if outcome == "completed":
            seeds.append(Seed(t, decay))
        # Skips only mean something for tracks the bot picked; skipping a human-queued
        # song is usually group chatter or someone else's pick, so it's ignored.
        elif outcome == "skipped_early" and t.is_recommendation:
            seeds.append(Seed(t, -0.5 * decay))
        elif outcome == "skipped_late" and t.is_recommendation:
            seeds.append(Seed(t, 0.2 * decay))
        # skipped_neutral / stopped / error: no signal, not a seed
    return seeds


def entry_from_mix(e: dict, rank: int, count: int) -> dict:
    artist, title, _ = parse_artist_title(e["raw_title"], e["channel"])
    return {
        "key": normalize_key(artist, title), "artist": artist, "title": title,
        "raw_title": e["raw_title"], "channel": e["channel"], "video_id": e["video_id"],
        "duration": e.get("duration") or 0, "score": round(1 - rank / count, 4),
    }


def entry_from_lastfm(artist: str, title: str, match: float) -> dict:
    return {"key": normalize_key(artist, title), "artist": artist, "title": title,
            "raw_title": "", "channel": "", "video_id": "", "duration": 0, "score": match}


def score_candidates(results: list[tuple[Seed, str, list[dict]]],
                     source_weights: dict[str, float]) -> dict[str, Candidate]:
    """results: (seed, source, entries). Σ_seeds w_seed · Σ_sources W_source · sim."""
    cands: dict[str, Candidate] = {}
    by_video: dict[str, str] = {}
    for seed, source, entries in results:
        w_src = source_weights.get(source, 0.0)
        for e in entries:
            key = by_video.get(e.get("video_id") or "", e["key"])
            c = cands.get(key)
            if c is None:
                c = cands[key] = Candidate(key=key, artist=e["artist"], title=e["title"])
            # Keep the richest metadata seen (a playable video id beats none).
            if e.get("video_id") and not c.video_id:
                c.video_id, c.raw_title, c.channel = e["video_id"], e["raw_title"], e["channel"]
                c.duration = e.get("duration") or c.duration
            if e["artist"] and not c.artist:
                c.artist, c.title = e["artist"], e["title"]
            if c.video_id:
                by_video[c.video_id] = key
            contrib = seed.weight * w_src * e["score"]
            c.score += contrib
            c.contributions[seed.label] = c.contributions.get(seed.label, 0.0) + contrib
            c.sources.add(source)
    return cands


def tag_profile(weighted_tags: list[tuple[float, dict]]) -> dict[str, float]:
    """Weighted sum of positive seeds' tag vectors, L2-normalized."""
    prof: dict[str, float] = {}
    for w, tags in weighted_tags:
        if w <= 0:
            continue
        for t, v in tags.items():
            prof[t] = prof.get(t, 0.0) + w * v
    norm = math.sqrt(sum(v * v for v in prof.values()))
    return {t: v / norm for t, v in prof.items()} if norm else {}


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _title_part(key: str) -> str:
    return key.split("|", 1)[1] if "|" in key else key


def is_non_music(c: Candidate) -> bool:
    if c.duration and (c.duration > 600 or c.duration < 60):
        return True
    return bool(NON_MUSIC_RE.search(c.raw_title or "") or NON_MUSIC_RE.search(c.title or ""))


_ARTIST_SPLIT_RE = re.compile(r",|&|\bx\b|\band\b|\bwith\b|\bfeat\.?|\bft\.?", re.IGNORECASE)


def primary_artist(artist: str) -> str:
    """'Kendrick Lamar, SZA' -> 'kendrick lamar' (used for the per-artist diversity cap)."""
    if not artist:
        return ""
    return normalize_key(_ARTIST_SPLIT_RE.split(artist)[0], "").rstrip("|")


def _artist_id(c: Candidate) -> str:
    return primary_artist(c.artist)


def select_picks(ranked: list[Candidate], count: int, *, exclude_keys: set[str],
                 exclude_videos: set[str], recent_artists: Sequence[str] = (), explore: float = 0.0,
                 rng: Optional[random.Random] = None) -> list[Candidate]:
    """Filters + diversity (spec §5.4). `ranked` must be sorted best-first.

    exclude_keys: canonical keys of current/queue/history + recently played in the guild.
    recent_artists: artists of the songs the picks will follow, in play order. A pick's
    artist may not match any of the ARTIST_GAP songs right before it (picks included).
    """
    rng = rng or random.Random()
    excluded_titles = {_title_part(k) for k in exclude_keys}

    def allowed(c: Candidate) -> bool:
        if c.score <= 0 or c.key in exclude_keys or (c.video_id and c.video_id in exclude_videos):
            return False
        # A low-confidence key ("|tum hi ho") may be the same song as "arijit singh|tum hi ho".
        title = _title_part(c.key)
        if title and title in excluded_titles and (not c.artist or any(
                k.startswith("|") and _title_part(k) == title for k in exclude_keys)):
            return False
        return not is_non_music(c)

    pool = [c for c in ranked if allowed(c)]
    before = [primary_artist(a) for a in recent_artists]

    def take(candidates, picks):
        used = {_artist_id(p) for p in picks if _artist_id(p)}
        blocked = set((before + [_artist_id(p) for p in picks])[-ARTIST_GAP:])
        for c in candidates:
            a = _artist_id(c)
            if c in picks or (a and (a in used or a in blocked)):
                continue
            return c
        return None

    picks: list[Candidate] = []
    for _ in range(count):
        c = take(pool, picks)
        if c is None:
            break
        picks.append(c)

    if picks and explore > 0 and rng.random() < explore:
        lo, hi = EXPLORE_RANKS
        others = [c for c in pool[lo:hi] if c not in picks]
        rng.shuffle(others)
        replacement = take(others, picks[:-1])
        if replacement is not None:
            picks[-1] = replacement
    return picks


def reason_for(c: Candidate) -> str:
    top = sorted(((v, k) for k, v in c.contributions.items() if v > 0), reverse=True)[:2]
    names = [k for _, k in top]
    if not names:
        return ""
    return "because you played " + " & ".join(names)


# ---------------------------------------------------------------------------
# Recommender (I/O + caching)
# ---------------------------------------------------------------------------

class Recommender:
    def __init__(self, store: Store, lastfm: Optional[LastFM] = None):
        self.store = store
        self.lastfm = lastfm or LastFM()

    async def close(self):
        await self.lastfm.close()

    # ---- candidate sources --------------------------------------------

    async def _ytmix(self, seed: Seed) -> list[dict]:
        vid = seed.track.video_id
        if not vid:
            return []
        cached = await self.store.run("cache_get", "ytmix", vid, YTMIX_TTL)
        if cached is not None:
            # Re-parse so parser improvements apply to already-cached Mixes.
            for e in cached:
                artist, title, _ = parse_artist_title(e["raw_title"], e["channel"])
                e.update(key=normalize_key(artist, title), artist=artist, title=title)
            return cached
        try:
            raw = await asyncio.wait_for(fetch_youtube_mix(vid), timeout=20)
        except Exception as e:
            print(f"YouTube Mix failed for {vid}: {e}")
            return []
        entries = [entry_from_mix(e, i, len(raw)) for i, e in enumerate(raw)]
        await self.store.run("cache_put", "ytmix", vid, entries)
        return entries

    async def _lastfm(self, seed: Seed) -> list[dict]:
        t = seed.track
        if not self.lastfm.enabled or not t.canonical_artist or t.meta_confidence < LASTFM_MIN_CONFIDENCE:
            return []
        cached = await self.store.run("cache_get", "lastfm_track", t.canonical_key, LASTFM_SIM_TTL)
        if cached is not None:
            return cached
        sims = await self.lastfm.similar_tracks(t.canonical_artist, t.canonical_title, limit=30)
        entries = [entry_from_lastfm(s["artist"], s["title"], s["match"]) for s in sims]
        if not entries:
            entries = await self._lastfm_artist_fallback(t.canonical_artist)
        await self.store.run("cache_put", "lastfm_track", t.canonical_key, entries)
        return entries

    async def _lastfm_artist_fallback(self, artist: str) -> list[dict]:
        seed_key = normalize_key(artist, "")
        cached = await self.store.run("cache_get", "lastfm_artist", seed_key, LASTFM_SIM_TTL)
        if cached is not None:
            return cached
        similar = (await self.lastfm.similar_artists(artist, limit=10))[:5]
        tops = await asyncio.gather(*(self.lastfm.artist_top_tracks(a["artist"], limit=3) for a in similar))
        entries = []
        for a, tracks in zip(similar, tops):
            for rank, t in enumerate(tracks):
                entries.append(entry_from_lastfm(t["artist"], t["title"], 0.6 * a["match"] * (1 - rank / 3)))
        await self.store.run("cache_put", "lastfm_artist", seed_key, entries)
        return entries

    async def _tags(self, key: str, artist: str, title: str) -> dict:
        if not artist:
            return {}
        row = await self.store.run("get_track", key)
        if row and row["info_fetched_at"] and time.time() - row["info_fetched_at"] < TAGS_TTL:
            return row["tags"]
        if not self.lastfm.enabled:
            return row["tags"] if row else {}
        tags = await self.lastfm.track_tags(artist, title)
        if not tags:
            tags = await self._artist_tags(artist)
        await self.store.run("upsert_track", key, artist, title, row["video_id"] if row else "",
                             row["duration"] if row else 0, tags, int(time.time()))
        return tags

    async def _artist_tags(self, artist: str) -> dict:
        seed = normalize_key(artist, "")
        cached = await self.store.run("cache_get", "lastfm_artist_tags", seed, TAGS_TTL)
        if cached is not None:
            return cached[0] if cached else {}
        tags = await self.lastfm.artist_tags(artist)
        await self.store.run("cache_put", "lastfm_artist_tags", seed, [tags])
        return tags

    # ---- main entry point --------------------------------------------

    async def recommend(self, guild_id: int, current: Optional[Track], queue: Sequence[Track],
                        history: Sequence[Track], outcomes: Sequence[str], count: int = 2,
                        explore: Optional[float] = None) -> list[Track]:
        seeds = build_seeds(current, queue, history, outcomes)
        if not seeds:
            return []
        for s in seeds:
            if not s.track.canonical_key:
                s.track.apply_metadata()

        # Bound network cost: strongest positive seeds + the most recent early skip.
        positives = sorted((s for s in seeds if s.weight > 0), key=lambda s: -s.weight)
        negatives = [s for s in seeds if s.weight < 0]
        fetch = positives[:MAX_FETCH_SEEDS - (1 if negatives else 0)] + negatives[:1]

        jobs = []
        for s in fetch:
            jobs.append((s, "ytmix", self._ytmix(s)))
            jobs.append((s, "lastfm", self._lastfm(s)))
        lists = await asyncio.gather(*(j[2] for j in jobs), return_exceptions=True)
        results = [(s, src, r) for (s, src, _), r in zip(jobs, lists) if isinstance(r, list)]

        cands = score_candidates(results, {"ytmix": W_YTMIX, "lastfm": W_LASTFM})
        ranked = sorted(cands.values(), key=lambda c: -c.score)

        await self._apply_tag_bonus(ranked[:TAG_TOP_N], positives)
        ranked.sort(key=lambda c: -c.score)

        exclude_keys, exclude_videos = set(), set()
        for t in [current, *queue, *history]:
            if t:
                exclude_keys.add(t.canonical_key)
                if t.video_id:
                    exclude_videos.add(t.video_id)
        since = int(time.time() - RECENT_HOURS * 3600)
        for p in await self.store.run("get_plays", guild_id, since):
            exclude_keys.add(p["track_key"])
        exclude_keys.discard("")
        exclude_keys.discard("|")

        if explore is None:
            # Per-guild explore rates arrive with /autoplay explore:<0-1> (Phase 4).
            explore = DEFAULT_EXPLORE
        # Play order the picks will follow: ... history, current, queue
        order = [*history, current, *queue] if current else [*history, *queue]
        picks = select_picks(ranked, count, exclude_keys=exclude_keys, exclude_videos=exclude_videos,
                             recent_artists=[t.canonical_artist for t in order[-ARTIST_GAP:]],
                             explore=explore)

        tracks = await asyncio.gather(*(self._to_track(c) for c in picks))
        return [t for t in tracks if t]

    async def _apply_tag_bonus(self, top: list[Candidate], positives: list[Seed]):
        if TAG_ALPHA <= 0 or not self.lastfm.enabled or not top:
            return
        seed_tags = await asyncio.gather(*(
            self._tags(s.track.canonical_key, s.track.canonical_artist, s.track.canonical_title)
            for s in positives[:MAX_FETCH_SEEDS]))
        profile = tag_profile([(s.weight, t) for s, t in zip(positives, seed_tags)])
        if not profile:
            return
        cand_tags = await asyncio.gather(*(self._tags(c.key, c.artist, c.title) for c in top))
        for c, tags in zip(top, cand_tags):
            c.score += TAG_ALPHA * cosine(tags, profile)

    async def _to_track(self, c: Candidate) -> Optional[Track]:
        if c.video_id:
            url = f"https://www.youtube.com/watch?v={c.video_id}"
            t = Track(title=c.raw_title or c.title, duration=c.duration, source="Autoplay -> YouTube",
                      webpage_url=url, stream_query=url, video_id=c.video_id, channel=c.channel)
            t.apply_metadata()
            if t.canonical_artist:  # show "Artist - Title" instead of the raw upload title
                t.artist, t.title = t.canonical_artist, t.canonical_title
        else:
            try:
                results = await search_youtube(f"{c.artist} {c.title} audio", limit=1)
            except Exception as e:
                print(f"Autoplay search failed for {c.artist} - {c.title}: {e}")
                return None
            if not results:
                return None
            t = results[0]
            t.title, t.artist, t.source = c.title, c.artist, "Autoplay -> YouTube"
            t.apply_metadata()
        t.is_recommendation = True
        t.rec_reason = reason_for(c)
        return t
