import asyncio
import random
import time

import pytest

import utils.recommender as rec
from utils.recommender import (
    Candidate, Recommender, Seed, build_seeds, cosine, reason_for, score_candidates,
    select_picks, tag_profile,
)
from utils.store import Store
from utils.track_resolver import Track

W = {"ytmix": 1.0, "lastfm": 1.0}


def track(artist, title, vid=""):
    return Track(title=title, artist=artist, video_id=vid).apply_metadata()


def entry(artist, title, score, vid="", duration=200):
    return {"key": f"{artist.lower()}|{title.lower()}", "artist": artist, "title": title,
            "raw_title": f"{artist} - {title}", "channel": artist, "video_id": vid,
            "duration": duration, "score": score}


def ranked(cands):
    return sorted(cands.values(), key=lambda c: -c.score)


# ---- seeds -------------------------------------------------------------

def test_build_seed_weights():
    cur = track("The Weeknd", "Blinding Lights")
    queue = [track("Dua Lipa", "Levitating"), track("The Weeknd", "Starboy"),
             track("Doja Cat", "Say So"), track("X", "Fourth In Queue")]
    history = [track("Imagine Dragons", "Believer"), track("Billie Eilish", "Bad Guy"),
               track("Linkin Park", "Numb"), track("Queen", "Bohemian Rhapsody")]
    history[1].is_recommendation = True   # Bad Guy was an autoplay pick
    outcomes = ["completed", "skipped_early", "completed", "stopped"]   # oldest first
    w = {s.label: round(s.weight, 4) for s in build_seeds(cur, queue, history, outcomes)}
    assert w == {
        "Blinding Lights": 1.0, "Levitating": 0.8, "Starboy": 0.6, "Say So": 0.4,
        # Bohemian Rhapsody (k=1) was stopped: neutral, no seed
        "Numb": 0.49,                       # k=2 completed
        "Bad Guy": round(-0.5 * 0.7 ** 3, 4),  # k=3 recommended + skipped early -> negative
        "Believer": round(0.7 ** 4, 4),     # k=4 completed
    }


def test_skips_of_human_queued_tracks_are_ignored():
    history = [track("A", "Rec Late"), track("B", "Rec Early"), track("C", "Mine Late"), track("D", "Mine Early")]
    history[0].is_recommendation = history[1].is_recommendation = True
    outcomes = ["skipped_late", "skipped_early", "skipped_late", "skipped_early"]
    w = {s.label: round(s.weight, 4) for s in build_seeds(None, [], history, outcomes)}
    assert w == {"Rec Early": round(-0.5 * 0.7 ** 3, 4), "Rec Late": round(0.2 * 0.7 ** 4, 4)}


# ---- scoring -----------------------------------------------------------

def test_multi_seed_boost():
    a, b = Seed(track("The Weeknd", "Blinding Lights"), 1.0), Seed(track("Dua Lipa", "Levitating"), 0.8)
    cands = score_candidates([
        (a, "ytmix", [entry("Single", "Only A", 0.9), entry("Both", "Shared", 0.6)]),
        (b, "ytmix", [entry("Both", "Shared", 0.6)]),
        (b, "lastfm", [entry("Both", "Shared", 0.5)]),
    ], W)
    order = [c.title for c in ranked(cands)]
    assert order[0] == "Shared"     # 0.6 + 0.48 + 0.4 beats a single strong 0.9
    assert cands["both|shared"].sources == {"ytmix", "lastfm"}
    assert reason_for(cands["both|shared"]) == "because you played Levitating & Blinding Lights"


def test_negative_seed_pushes_down():
    good, bad = Seed(track("A", "Liked"), 1.0), Seed(track("B", "Skipped"), -0.5)
    cands = score_candidates([
        (good, "ytmix", [entry("X", "Near Skipped", 0.8), entry("Y", "Clean", 0.6)]),
        (bad, "ytmix", [entry("X", "Near Skipped", 1.0), entry("Z", "Only Bad", 0.9)]),
    ], W)
    assert cands["x|near skipped"].score == pytest.approx(0.3)
    assert cands["z|only bad"].score < 0
    picks = select_picks(ranked(cands), 3, exclude_keys=set(), exclude_videos=set())
    assert [p.title for p in picks] == ["Clean", "Near Skipped"]   # negative score never picked


def test_merge_on_video_id():
    s = Seed(track("A", "Seed"), 1.0)
    cands = score_candidates([
        (s, "ytmix", [{**entry("", "Tum Hi Ho", 0.5, vid="v1"), "key": "|tum hi ho"}]),
        (s, "ytmix", [{**entry("Arijit Singh", "Tum Hi Ho", 0.5, vid="v1")}]),
    ], W)
    assert len(cands) == 1
    [c] = cands.values()
    assert c.score == pytest.approx(1.0) and c.artist == "Arijit Singh"


# ---- filters & diversity ----------------------------------------------

def _cands(*rows):
    return [Candidate(key=f"{a.lower()}|{t.lower()}", artist=a, title=t, score=s,
                      duration=d, raw_title=f"{a} - {t}") for a, t, s, d in rows]


def test_artist_cap_and_no_back_to_back():
    pool = _cands(("The Weeknd", "Save Your Tears", 3, 200), ("The Weeknd", "Starboy", 2.9, 200),
                  ("Dua Lipa", "Physical", 2, 200), ("Dua Lipa", "Hallucinate", 1.9, 200),
                  ("Doja Cat", "Say So", 1, 200))
    picks = select_picks(pool, 3, exclude_keys=set(), exclude_videos=set())
    assert [p.title for p in picks] == ["Save Your Tears", "Physical", "Say So"]
    # Current artist can't be the first pick
    picks = select_picks(pool, 2, exclude_keys=set(), exclude_videos=set(), recent_artists=["The Weeknd"])
    assert [p.artist for p in picks] == ["Dua Lipa", "Doja Cat"]   # The Weeknd needs 2 songs in between


def test_recent_and_queued_exclusion():
    pool = _cands(("A", "Played Recently", 3, 200), ("B", "In Queue", 2, 200), ("C", "Fresh", 1, 200))
    picks = select_picks(pool, 3, exclude_keys={"a|played recently", "b|in queue"}, exclude_videos=set())
    assert [p.title for p in picks] == ["Fresh"]


def test_low_confidence_title_match_is_excluded():
    pool = _cands(("", "Tum Hi Ho", 3, 200), ("X", "Other", 1, 200))
    picks = select_picks(pool, 2, exclude_keys={"arijit singh|tum hi ho"}, exclude_videos=set())
    assert [p.title for p in picks] == ["Other"]


def test_non_music_filtered():
    pool = _cands(("A", "Best Of Full Album", 5, 200), ("B", "Too Long", 4, 3600),
                  ("C", "Intro", 3, 20), ("D", "Song Reaction", 2, 200), ("E", "Real Song", 1, 200))
    picks = select_picks(pool, 5, exclude_keys=set(), exclude_videos=set())
    assert [p.title for p in picks] == ["Real Song"]


def test_explore_swaps_in_lower_rank():
    pool = _cands(*[(f"Artist{i}", f"Song{i}", 20 - i, 200) for i in range(15)])
    always = select_picks(pool, 2, exclude_keys=set(), exclude_videos=set(), explore=1.0,
                          rng=random.Random(1))
    assert always[0].title == "Song0"
    assert 5 <= int(always[1].title[4:]) < 15
    never = select_picks(pool, 2, exclude_keys=set(), exclude_videos=set(), explore=0.0)
    assert [p.title for p in never] == ["Song0", "Song1"]


# ---- tags --------------------------------------------------------------

def test_tag_profile_and_cosine():
    prof = tag_profile([(1.0, {"synthpop": 1.0, "pop": 0.8}), (0.8, {"pop": 1.0, "disco": 0.6}),
                        (-0.3, {"metal": 1.0})])
    assert "metal" not in prof
    assert sum(v * v for v in prof.values()) == pytest.approx(1.0)
    assert cosine({"synthpop": 1.0, "pop": 0.7}, prof) > 0.8
    assert cosine({"metal": 1.0, "rock": 0.5}, prof) == 0.0
    assert cosine({}, prof) == 0.0


# ---- end-to-end with fake sources ----------------------------------------

class FakeLastFM:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def similar_tracks(self, artist, title, limit=30):
        self.calls += 1
        return [{"artist": "Dua Lipa", "title": "Physical", "match": 0.9},
                {"artist": "The Weeknd", "title": "Save Your Tears", "match": 0.8},
                {"artist": "Doja Cat", "title": "Say So", "match": 0.3}]

    async def track_tags(self, artist, title):
        self.calls += 1
        return {"pop": 1.0, "synthpop": 0.5}

    async def artist_tags(self, artist):
        self.calls += 1
        return {}

    async def close(self):
        pass


def test_recommend_end_to_end_and_cache(monkeypatch):
    mix_calls = []

    async def fake_mix(video_id, limit=25):
        mix_calls.append(video_id)
        return [{"video_id": "vTears", "raw_title": "The Weeknd - Save Your Tears (Official Video)",
                 "channel": "TheWeekndVEVO", "duration": 215},
                {"video_id": "vRecent", "raw_title": "Harry Styles - As It Was", "channel": "Harry Styles",
                 "duration": 170},
                {"video_id": "vHour", "raw_title": "Synthwave 1 hour mix", "channel": "x", "duration": 3600}]

    searched = []

    async def fake_search(q, limit=1):
        searched.append(q)   # only Last.fm-only picks should be searched
        return [Track(title="Dua Lipa - Physical (Official Video)", video_id="vPhys", source="YouTube")]

    monkeypatch.setattr(rec, "fetch_youtube_mix", fake_mix)
    monkeypatch.setattr(rec, "search_youtube", fake_search)

    store = Store(":memory:")
    store.insert_play(1, "s", "harry styles|as it was", 1, False, 170, [1], started_at=int(time.time()) - 600)
    lastfm = FakeLastFM()
    r = Recommender(store, lastfm)
    cur = Track(title="The Weeknd - Blinding Lights", channel="TheWeekndVEVO", video_id="vBL").apply_metadata()

    async def go():
        return await r.recommend(1, cur, [], [], [], count=3, explore=0.0)

    picks = asyncio.run(go())
    # Save Your Tears scores highest (both sources) but The Weeknd is playing now, so it has to
    # wait for two other artists. As It Was was played 10 min ago; the 1-hour mix is non-music.
    assert [p.display_name for p in picks] == [
        "Dua Lipa - Physical", "Doja Cat - Say So", "The Weeknd - Save Your Tears"]
    assert searched == ["Dua Lipa Physical audio", "Doja Cat Say So audio"]
    tears = picks[2]
    assert tears.video_id == "vTears" and tears.is_recommendation and tears.requested_by is None
    assert tears.rec_reason == "because you played Blinding Lights"
    assert tears.source == "Autoplay -> YouTube"
    cold_calls = len(mix_calls) + lastfm.calls

    asyncio.run(go())
    warm_calls = len(mix_calls) + lastfm.calls - cold_calls
    assert cold_calls <= 10
    assert warm_calls == 0


def test_physical_resolved_via_search(monkeypatch):
    async def fake_mix(video_id, limit=25):
        return []

    searched = []

    async def fake_search(q, limit=1):
        searched.append(q)
        return [Track(title="Dua Lipa - Physical (Official Video)", video_id="vPhys", source="YouTube")]

    monkeypatch.setattr(rec, "fetch_youtube_mix", fake_mix)
    monkeypatch.setattr(rec, "search_youtube", fake_search)
    r = Recommender(Store(":memory:"), FakeLastFM())
    cur = Track(title="Blinding Lights", artist="The Weeknd", video_id="vBL").apply_metadata()
    picks = asyncio.run(r.recommend(1, cur, [], [], [], count=1, explore=0.0))
    assert searched == ["Dua Lipa Physical audio"]
    assert picks[0].video_id == "vPhys" and picks[0].display_name == "Dua Lipa - Physical"


def test_works_without_lastfm(monkeypatch):
    async def fake_mix(video_id, limit=25):
        return [{"video_id": "v2", "raw_title": "Chahun Main Ya Naa Full Video Song Aashiqui 2 | Aditya",
                 "channel": "T-Series", "duration": 241}]

    monkeypatch.setattr(rec, "fetch_youtube_mix", fake_mix)
    lastfm = rec.LastFM(api_key="")
    r = Recommender(Store(":memory:"), lastfm)
    cur = Track(title="Tum Hi Ho Full Video Song | Aashiqui 2", channel="T-Series", video_id="v1").apply_metadata()
    picks = asyncio.run(r.recommend(1, cur, [], [], [], count=2, explore=0.0))
    assert [p.video_id for p in picks] == ["v2"]
    assert picks[0].canonical_title == "Chahun Main Ya Naa"


def test_clean_tags_drops_junk():
    from utils.lastfm import clean_tags
    raw = [{"name": "synthwave", "count": 100}, {"name": "The Weeknd", "count": 60},
           {"name": "2019", "count": 37}, {"name": "-1001740215468", "count": 3},
           {"name": "seen live", "count": 50}, {"name": "2010s", "count": 12},
           {"name": "pop", "count": 50}]
    assert clean_tags(raw, "The Weeknd") == {"synthwave": 1.0, "pop": 0.5, "2010s": 0.12}


def test_artist_cap_uses_primary_artist():
    pool = _cands(("Kendrick Lamar", "Money Trees", 3, 200), ("Kendrick Lamar, SZA", "All The Stars", 2.9, 200),
                  ("A$AP Rocky", "Praise The Lord", 2, 200), ("The Weeknd", "Starboy", 1, 200))
    picks = select_picks(pool, 3, exclude_keys=set(), exclude_videos=set(), recent_artists=["Kendrick Lamar"])
    # Kendrick can't return until 2 other artists have played
    assert [p.title for p in picks] == ["Praise The Lord", "Starboy", "Money Trees"]


def test_last_two_artists_blocked():
    # Queue ends: ELEMENT. (Kendrick) -> a lot (21 Savage) -> YAH. (Kendrick)
    pool = _cands(("Kendrick Lamar", "FEEL.", 3, 200), ("21 Savage", "redrum", 2.9, 200),
                  ("Denzel Curry", "Walkin", 2, 200), ("J. Cole", "MIDDLE CHILD", 1, 200))
    picks = select_picks(pool, 3, exclude_keys=set(), exclude_videos=set(),
                         recent_artists=["21 Savage", "Kendrick Lamar"])
    # Walkin: 21 Savage & Kendrick blocked. redrum: only YAH. & Walkin are the last two now.
    # FEEL.: Kendrick is allowed again once two other artists sit in between.
    assert [p.title for p in picks] == ["Walkin", "redrum", "FEEL."]


def test_feedback_seeds():
    liked, disliked = track("SZA", "Snooze"), track("Drake", "Teenage Fever")
    w = {s.label: s.weight for s in build_seeds(track("A", "Now"), [], [], [],
                                                feedback=[(liked, 1.5), (disliked, -1.5)])}
    assert w == {"Now": 1.0, "Snooze": rec.LIKE_SEED_WEIGHT, "Teenage Fever": rec.DISLIKE_SEED_WEIGHT}


def test_store_disliked_is_net_negative():
    s = Store(":memory:")
    s.add_feedback(1, "s", "drake|teenage fever", "v1", 10, -1.5, "remove")
    s.add_feedback(1, "s", "sza|snooze", "v2", 10, 1.5, "like")
    s.add_feedback(1, "s", "joji|tarmac", "v3", 10, -1.5, "dislike")
    s.add_feedback(1, "s", "joji|tarmac", "v3", 11, 1.5, "like")      # someone else liked it: cancels out
    assert s.get_disliked(1, 0) == ({"drake|teenage fever"}, {"v1"})
    assert s.get_disliked(2, 0) == (set(), set())


def test_disliked_tracks_never_recommended(monkeypatch):
    async def fake_mix(video_id, limit=25):
        return [{"video_id": "vBad", "raw_title": "Drake - Teenage Fever", "channel": "Drake", "duration": 200},
                {"video_id": "vOk", "raw_title": "SZA - Snooze", "channel": "SZA", "duration": 200}]

    monkeypatch.setattr(rec, "fetch_youtube_mix", fake_mix)
    store = Store(":memory:")
    store.add_feedback(1, "old", "drake|teenage fever", "vBad", 10, -1.5, "remove")
    r = Recommender(store, rec.LastFM(api_key=""))
    cur = Track(title="Steve Lacy - Infrunami", channel="Steve Lacy", video_id="v0").apply_metadata()
    picks = asyncio.run(r.recommend(1, cur, [], [], [], count=2, explore=0.0))
    assert [p.video_id for p in picks] == ["vOk"]
