import asyncio

import pytest

from utils.store import Store, classify_outcome


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.mark.parametrize("listened,skip,stop,err,expected", [
    (200, False, False, False, "completed"),
    (10, True, False, False, "skipped_early"),
    (29.9, True, False, False, "skipped_early"),
    (30, True, False, False, "skipped_late"),
    (59.9, True, False, False, "skipped_late"),
    (60, True, False, False, "skipped_neutral"),
    (240, True, False, False, "skipped_neutral"),
    (90, False, True, False, "stopped"),
    (5, True, True, False, "stopped"),   # /stopandclear wins over a pending skip
    (50, False, False, True, "error"),
])
def test_classify_outcome(listened, skip, stop, err, expected):
    assert classify_outcome(listened, skip, stop, err) == expected


def test_play_lifecycle(store):
    pid = store.insert_play(1, "sess", "adele|hello", 42, False, 295, [42, 43])
    store.finish_play(pid, 12, "skipped_early")
    [row] = store.get_plays(1)
    assert row["track_key"] == "adele|hello"
    assert row["listeners"] == 2 and row["listener_ids"] == "[42, 43]"
    assert (row["listened_sec"], row["outcome"]) == (12, "skipped_early")
    assert store.get_plays(2) == []


def test_track_upsert_keeps_video_and_tags(store):
    store.upsert_track("adele|hello", "Adele", "Hello", "vid1", 295, tags={"pop": 1.0}, info_fetched_at=5)
    store.upsert_track("adele|hello", "Adele", "Hello", "", 0)
    t = store.get_track("adele|hello")
    assert t["video_id"] == "vid1" and t["duration"] == 295
    assert t["tags"] == {"pop": 1.0} and t["info_fetched_at"] == 5
    assert store.get_track_by_video("vid1")["key"] == "adele|hello"


def test_sim_cache_ttl(store):
    store.cache_put("ytmix", "vid1", [{"key": "a|b"}])
    assert store.cache_get("ytmix", "vid1", max_age_sec=60) == [{"key": "a|b"}]
    assert store.cache_get("ytmix", "vid1", max_age_sec=-1) is None
    assert store.cache_get("ytmix", "other", max_age_sec=60) is None


def test_guild_settings(store):
    assert store.get_guild_settings(7) == {"autoplay": False, "explore": 0.2}
    store.set_guild_setting(7, autoplay=True)
    assert store.get_guild_settings(7) == {"autoplay": True, "explore": 0.2}


def test_async_run(store):
    pid = asyncio.run(store.run("insert_play", 1, "s", "k", None, True, 0, []))
    assert store.get_plays(1)[0]["id"] == pid
