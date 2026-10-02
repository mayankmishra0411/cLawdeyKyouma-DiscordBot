# Recommendation Engine — Build Spec (v1, local)

Spec for adding song recommendations + autoplay to the Discord music bot.
Target: **local Windows PC** (residential IP, `BOT_ENV` unset). Oracle deployment is deferred.

---

## 1. Goals & constraints

**Goals**
1. Recommend songs based on the **current track's metadata** and the **server's past behaviour** (cleaned of noise).
2. Make recommendations **queue-aware**: previous, current and upcoming tracks together define the "vibe".
3. **Autoplay**: keep music going during gaming sessions without anyone returning to Discord.

**Constraints**
- Low compute / memory: no ML models, no embeddings. Only cached HTTP calls, SQLite, weighted sums.
- Free data sources only. Spotify's recommendation endpoints are **not usable** (Related Artists /
  Recommendations / Audio Features restricted for new apps since Nov 2024; dev-mode further limited Feb 2026).
- Must degrade gracefully: if `LASTFM_API_KEY` is missing, run on YouTube Mix only.

**Non-goals (v1):** Oracle/datacenter support, cross-server learning, a web dashboard.

---

## 2. Decisions & defaults

| Decision | Default | Notes |
|---|---|---|
| Candidate sources | **Hybrid: YouTube Mix + Last.fm** | Locally YouTube is reliable, so Mix is a first-class source. Mix is strong for Indian/Bollywood music; Last.fm is strong for Western music and gives tags. |
| Autoplay behaviour | **Auto-add** when queue runs low | Toggle per server with `/autoplay`. Default **off**, persisted. |
| Storage | **SQLite** (stdlib `sqlite3`) at `data/bot.db` | Add `data/` to `.gitignore`. |
| Last.fm key | Optional env var `LASTFM_API_KEY` | Free from https://www.last.fm/api/account/create |

Source weights are config values (see §9) so they can be tuned once we see what the group plays.

---

## 3. Architecture

```
cogs/music.py            ← hooks: play start/end, skip, autoplay trigger, new commands
utils/track_resolver.py  ← Track gets new fields; YouTube functions populate them
utils/metadata.py   NEW  ← title cleaning, artist/title parsing, canonical keys
utils/store.py      NEW  ← SQLite: tracks, plays, sim cache, guild settings
utils/lastfm.py     NEW  ← async Last.fm client (aiohttp) with caching + rate limit
utils/recommender.py NEW ← context building, candidate gen, scoring, filtering
tests/              NEW  ← parser + scorer unit tests (offline fixtures)
```

Flow:
```
session context ─► candidate generation ─► scoring ─► filters + diversity ─► queue
(history/current/   (YT Mix per seed,        (weighted   (repeats, non-music,
 upcoming, skips)    Last.fm similar,         sum)        artist cap, explore)
                     server co-occurrence)
```

---

## 4. Phase 1 — Metadata + play logging (build first; history starts accruing immediately)

### 4.1 `Track` new fields (`utils/track_resolver.py`)
```python
video_id: str = ""            # YouTube ID when known
channel: str = ""             # uploader/channel name from yt-dlp
canonical_artist: str = ""
canonical_title: str = ""
canonical_key: str = ""       # "artist|title", lowercased, normalized
meta_confidence: float = 0.0  # 0..1, how sure we are of artist/title
requested_by: int | None = None   # Discord user id; None for autoplay
is_recommendation: bool = False
rec_reason: str = ""          # e.g. "because you played X & Y"
```
- Populate `video_id` and `channel` in `search_youtube()` / `resolve_youtube_playlist()` from the flat
  entry (`id`, `channel` / `uploader`). **Verify the exact keys** by printing one flat entry first.
- Spotify→YouTube tracks already have clean artist/title → confidence 1.0.
- Set `requested_by` in `/play`, `/playlist`, and the search select callback.

### 4.2 `utils/metadata.py`
`parse_artist_title(raw_title, channel) -> (artist, title, confidence)`

Rules, in order:
1. Strip noise (case-insensitive): `(Official Video)`, `(Official Music Video)`, `(Lyric Video)`,
   `[Lyrics]`, `(Audio)`, `(Full Video)`, `Full Video Song`, `HD`, `4K`, `(Remastered ...)`,
   emoji, trailing hashtags. Keep `(Remix)`, `(Acoustic)`, `(Live)` — they matter.
2. Pull out `ft.` / `feat.` / `featuring` and store the featured artists separately; drop them from the key.
3. If the title has `" - "`: left = artist, right = title (confidence 0.8).
4. Channel fallbacks: `"<X> - Topic"` → artist X (0.9); `<X>VEVO` → artist X (0.8).
5. **Indian label channels** (T-Series, Sony Music India, Zee Music Company, Saregama, Tips Official,
   Speed Records, YRF, etc.; keep a list): titles are often `Song | Movie | Actors | Composer`.
   Take segment 1 as the title, leave the artist empty, confidence 0.3. Don't guess the artist from actor
   names. For low-confidence seeds the recommender leans on YouTube Mix, which doesn't need metadata.
6. Otherwise: whole cleaned title, empty artist, confidence 0.2.

`normalize_key(artist, title)`: lowercase, strip accents/punctuation, collapse whitespace → `"artist|title"`.
If the artist is empty, use `"|title"`; if a `video_id` exists, also store it as an alias key.

**Canonicalization (optional, when Last.fm is available and confidence < 0.9):** call
`track.getInfo?autocorrect=1` once per new track. It returns the corrected artist/title **and** top tags
in one call. Cache the result in the `tracks` table and never re-query the same key within 90 days.

### 4.3 `utils/store.py` — SQLite schema
```sql
CREATE TABLE tracks (
  key TEXT PRIMARY KEY,           -- canonical_key
  artist TEXT, title TEXT, video_id TEXT, duration INT,
  tags TEXT,                      -- JSON {tag: weight 0..1}, from Last.fm
  info_fetched_at INT
);
CREATE TABLE plays (
  id INTEGER PRIMARY KEY,
  guild_id INT, session_id TEXT, track_key TEXT,
  requested_by INT,               -- NULL = autoplay
  is_recommendation INT,
  started_at INT, listened_sec INT, duration INT,
  outcome TEXT,                   -- completed | skipped_early | skipped_late | skipped_neutral | stopped | error
  listeners INT,                  -- humans in voice at start
  listener_ids TEXT               -- JSON list of user ids in voice
);
CREATE INDEX plays_guild_time ON plays(guild_id, started_at);
CREATE INDEX plays_session ON plays(session_id);
CREATE TABLE sim_cache (
  source TEXT, seed TEXT,         -- source: 'lastfm_track' | 'lastfm_artist' | 'ytmix'
  payload TEXT,                   -- JSON list of {key, artist, title, video_id, score}
  fetched_at INT,
  PRIMARY KEY (source, seed)
);
CREATE TABLE guild_settings (
  guild_id INT PRIMARY KEY, autoplay INT DEFAULT 0, explore REAL DEFAULT 0.2
);
```
Use one connection and run writes via `run_in_executor`, or use `aiosqlite`. The volume is tiny either way.

### 4.4 Play logging hooks (`cogs/music.py`)
- `GuildPlayer` gets: `history: deque[Track](maxlen=10)`, `history_outcomes: deque[str](maxlen=10)`,
  `session_id: str`, `last_activity: float`, `current_play_id: int | None`, `skip_requested: bool`,
  `stop_requested: bool`.
- **Session**: new `session_id` (uuid4) when the bot connects to voice, on `/stopandclear` or `/leave`, or when
  more than 30 min pass with nothing playing. A new session clears `history` / `history_outcomes`, so songs from
  before a stop never seed recommendations for what's queued after it.
- **On real track start** (`start_offset == 0` in `_start`): insert the `plays` row with `listeners` /
  `listener_ids` from `voice_client.channel.members` (exclude bots).
- **On track end** (`_after` / next `play_next`): compute `listened_sec` from the existing
  `start_time` / `total_pause_duration` logic and classify:
  - `skip_requested` and listened < 30s → `skipped_early`
  - `skip_requested` and 30s ≤ listened < 60s → `skipped_late`
  - `skip_requested` and listened ≥ 60s → `skipped_neutral` (logged, but ignored by all scoring:
    after a minute in, a skip is more likely "enough of this" or chat than dislike)
  - `stop_requested` (`/stopandclear`, `/leave`) → `stopped` (neutral, **not** a negative signal)
  - `err` set → `error`
  - else → `completed`

  Then push the track + outcome onto `history`.
- ⚠️ **Seek/jump also call `voice_client.stop()`**. When `seek_target` is set, it's not a track end:
  don't log, don't push history. Only `/skip` sets `skip_requested`.

**Phase 1 acceptance:** play, skip, seek, stop through a few songs → `plays` rows have correct outcomes,
seeks create no rows, parser unit tests pass on ~30 real titles (include Bollywood label uploads).

---

## 5. Phase 2 — Recommender core

### 5.1 Context (queue awareness)
Build seeds from `GuildPlayer`:

| Seed | Weight |
|---|---|
| current track | 1.0 |
| upcoming queue[i] (first 3) | 0.8, 0.6, 0.4 |
| history[k] completed (last 5, newest k=1) | 0.7^k |
| history[k] skipped_early, `is_recommendation` | **−0.5 × 0.7^k** (negative seed) |
| history[k] skipped_late, `is_recommendation` | 0.2 × 0.7^k |
| history[k] skipped (any kind), human-queued | — (not a seed) |
| history[k] skipped_neutral / stopped / error | — (not a seed) |

Session tag profile: weighted sum of the positive seeds' Last.fm tag vectors, L2-normalized.

Tag vectors: `track.getTopTags` counts (0..100) → keep top 10 after dropping junk tags (blocklist:
`seen live`, `favorites`, `favourite`, `my top songs`, `albums i own`, `love`, `awesome`, …) → scale to 0..1.
If a track has no tags, fall back to `artist.getTopTags` (cached 90d). No tags → cosine term is 0 (never negative).

### 5.2 Candidate generation (per seed, all cached in `sim_cache`)
- **YouTube Mix** (needs `video_id`): flat-extract `https://www.youtube.com/watch?v={id}&list=RD{id}`
  with `extract_flat`. Take up to 25 entries, skip the seed itself. Similarity = `1 - rank/len`.
  Parse each entry with `parse_artist_title`. Cache 7 days.
- **Last.fm** (needs artist + title, confidence ≥ 0.5): `track.getSimilar?limit=30&autocorrect=1` → use
  `match` (0..1). If empty, fall back to `artist.getSimilar?limit=10` + `artist.getTopTracks?limit=3` for
  the top 5 similar artists (scaled ×0.6). Cache 30 days.
- **Server co-occurrence** (Phase 3, see §6.3).

Merge candidates on `canonical_key` (fall back to `video_id` match). Keep the best `video_id` seen.

### 5.3 Scoring
```
score(c) = Σ_seeds  w_seed · Σ_sources  W_source · sim_source(seed, c)
         + α · cosine(tags(c), session_tag_profile)      # only for top ~15 pre-ranked candidates
         + β · server_affinity(c)                         # Phase 3; 0 in Phase 2
```
Defaults: `W_ytmix = 1.0`, `W_lastfm = 1.0`, `W_cooc = 0.3` (Phase 3), `α = 0.5`, `β = 0.25`.
Server history is deliberately weighted low: it may nudge/confirm the ranking but never dominate it.
Candidates that show up for **several seeds** naturally rise to the top. That's the queue-aware behaviour.

Artist-dominance controls (a seed's YouTube Mix can be 80% its own artist):
- **Same-artist discount:** a candidate by the seed's own artist gets that seed's contribution ×`REC_SAME_ARTIST_DISCOUNT` (0.5).
- **Session saturation:** after scoring, candidate score ÷ (1 + `REC_ARTIST_SATURATION` (0.5) × n), where n = how many
  songs by that artist are in history / current / queue.
- **Feedback seeds count once per track** (net value). A 👍 on a track already in context boosts that seed ×1.3
  instead of adding a duplicate; otherwise up to 3 extra seeds (👍 +0.6, 👎/Remove −0.6). A 👎 replaces a "completed" seed.
- Empty YouTube Mix results are never cached (they're usually transient failures).
Fetch tags (`track.getTopTags`, cached) only for the top ~15 to bound API calls.

### 5.4 Filters & diversity (applied after scoring)
- Exclude: anything in current / queue / history; anything played in this guild in the last
  `RECENT_HOURS=3`; the seed artist of the current track for the first pick.
- Non-music: duration > 600s or < 60s (when known); titles matching
  `full album|1 hour|10 hours|podcast|mashup compilation|jukebox|nonstop|reaction|interview`.
- Diversity: max 1 per artist per batch (by primary artist: "Kendrick Lamar, SZA" counts as Kendrick);
  a pick's artist may not match either of the **2 songs before it** in play order (history → current → queue → earlier picks).
- Explore: with probability `explore` (default 0.2), replace one pick with a random candidate from rank 6–15.
- Reason string: top 2 contributing positive seeds → `"because you played A & B"`.

### 5.5 Resolving to playable audio
- YT Mix candidates already have a `video_id` → build the `Track` directly (source `"Autoplay -> YouTube"`).
- Last.fm-only candidates: `search_youtube(f"{artist} {title} audio", limit=1)`, **only for the picks
  actually being queued** (2–3 searches per batch, not one per candidate).

### 5.6 Autoplay & commands
- `/autoplay on|off`: persisted in `guild_settings`.
- **Prefetch**: when a track starts and autoplay is on, kick off `recommender.recommend(guild)` as a
  background task and store the result in `player.pending_recs`, so the next refill is instant.
- **Trigger**: after `play_next` pops a track, if autoplay is on and `len(queue) <= 1`, append
  `AUTOPLAY_BATCH=2` tracks from `pending_recs` (recompute if stale or empty). Announce:
  `✨ Autoplay queued **X** — because you played A & B`.
- Autoplay must not trigger after `/stopandclear` or `/leave`.
- `/recommend [count=5]`: show picks using the existing `SearchResultsView` / select pattern, with reasons.
- `/queue` and the now-playing message: mark recommendations with ✨.
- Never fail playback because of the recommender. Wrap it in try/except, log, and just stop autoplaying.

**Phase 2 acceptance:** with autoplay on, queue 2 songs and let them run → the bot keeps adding fitting
songs indefinitely with no repeats inside 3h; works with `LASTFM_API_KEY` unset (YT Mix only); a batch costs
≤ ~10 network calls on a cold cache and ~2 on a warm one.

---

## 6. Phase 3 — Past behaviour (cleaned)

### 6.1 Per-play signal
`completed = +1.0`, `skipped_late = −0.3`, `skipped_early = −1.0`, `skipped_neutral/stopped/error = 0`.
Multiply by time decay `0.5 ^ (age_days / 30)`. Multiply by 0.5 when `listeners == 1` (solo testing).
Recommendations the group accepted (completed) count fully; skipped recommendations count as normal negatives.
**Skips only score when `is_recommendation = 1`.** Skips of human-queued tracks (`requested_by` set) are
treated as 0: they're mostly group chatter or skipping someone else's pick, not taste. They're still
logged, so this rule can be revisited once real data exists. (The "≥ 2 early skips → negative affinity"
exception in §6.2 likewise counts only skips of recommended plays.)

### 6.2 Noise cleaning: a track only becomes "server taste" if
- it has ≥ 2 completed plays across ≥ 2 distinct sessions, **or** was queued by ≥ 2 distinct users; and
- it isn't flagged non-music (§5.4); and
- it isn't an **off-vibe one-off**: if it was skipped and its tag vector is far from that session's
  tag profile (cosine < 0.2), drop that play from affinity entirely.

`server_affinity(c)` = sum of decayed signals for `c`, squashed with `tanh` to stay in −1..1.
Tracks failing the support rule get affinity 0 (neutral), not negative, unless they were skipped early ≥ 2 times.

### 6.3 Server co-occurrence (a third, free candidate source, low weight `W_cooc = 0.3`)
For each positive seed, find sessions where it was **completed**. Collect other tracks completed in those
sessions **±3 plays** apart. This is item-to-item collaborative filtering on the server's own data: one SQL
query, no API calls. Because mixed-taste sessions are noisy, a pair only counts when:
- it co-occurs in **≥ 2 distinct sessions** (a single session contributes nothing);
- **the same user queued both**, or both were completed autoplay picks (A's song next to B's song is not evidence);
- it isn't tag-contradicted: if both tracks have tags and their cosine < 0.1, ignore the pair.

Score = decayed count of qualifying co-occurrences, normalized to 0..1 per seed.

### 6.4 "Who's in voice" boost
When computing affinity, plays whose `listener_ids` overlap the current voice members get ×1.5. The
recommendations shift toward whoever is actually in the call right now.

**Phase 3 acceptance:** a song played once by one person and skipped has no lasting effect; songs the group
repeatedly finishes start appearing as recommendations in matching sessions.

---

## 7. Phase 4 — Polish (optional)
- ✅ **Done (built early):** feedback buttons on recommended tracks, stored in a `feedback` table
  (`guild_id, session_id, track_key, video_id, user_id, value, action, created_at`).
  - "✨ Autoplay queued" message: **👍** (+1.5) and **🗑️ Remove** (−1.5, takes it out of the queue).
  - "Now playing" message of a recommendation: **👍** (+1.5) and **👎 Skip** (−1.5, skips; logged as a skip).
  - Live effect: the session's last 5 feedback items become seeds (👍 +0.8, 👎/Remove −0.6) and are excluded
    from picks. A track whose net feedback in the guild is negative isn't recommended for 30 days.
  - Phase 3 should add feedback values to `server_affinity` (they're explicit, so no support rule needed).
- `/autoplay explore:<0-1>` to set the explore rate per guild.
- `/why`: shows the reason and top contributing seeds/sources for the current recommendation.

---

## 8. Network & caching rules
- One shared `aiohttp.ClientSession`; Last.fm limited to ~4 req/s with an `asyncio.Semaphore` + small delay.
- All yt-dlp calls stay in `run_in_executor` (existing `_run_extract`).
- Cache TTLs: YT Mix 7d, Last.fm similar 30d, track info/tags 90d.
- Timeouts: 5s per HTTP call; on failure, skip that source for that seed.

## 9. Config (`.env`)
```
LASTFM_API_KEY=            # optional; without it → YouTube Mix only
REC_DB_PATH=data/bot.db
AUTOPLAY_BATCH=2
REC_RECENT_HOURS=3
REC_W_YTMIX=1.0
REC_W_LASTFM=1.0
REC_TAG_ALPHA=0.5
REC_HISTORY_BETA=0.25
REC_W_COOC=0.3
REC_EXPLORE=0.2
REC_ARTIST_GAP=2
REC_SAME_ARTIST_DISCOUNT=0.5
REC_ARTIST_SATURATION=0.5
```
Update `README.md` with the new commands and env vars when each phase lands.

## 10. Testing
- `tests/test_metadata.py`: parser on real titles (Western, `- Topic`, VEVO, T-Series / Zee pipe format,
  `ft.` cases, remix/live).
- `tests/test_recommender.py`: scoring/filters with fixture candidate lists (no network). Assert
  multi-seed boost, negative seeds, artist cap, recent-play exclusion.
- Manual: a short session in a test server with autoplay on, then inspect the `plays` table.

## 11. Known limits
- Bollywood label uploads have weak metadata. YouTube Mix covers them, but Last.fm/tags contribute little there.
- YouTube Mix leans toward the same artist; the artist cap and multi-seed scoring counter this.
- History is per server only; a new server starts with metadata-only recommendations.
