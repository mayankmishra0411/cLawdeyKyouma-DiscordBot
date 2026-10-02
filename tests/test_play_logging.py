"""Drives MusicCog.play_next with a fake voice client to check what lands in `plays`."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import cogs.music as music
from utils.store import Store
from utils.track_resolver import Track


class FakeVoiceClient:
    def __init__(self):
        self.after = None
        self.playing = False
        self.channel = SimpleNamespace(members=[
            SimpleNamespace(id=1, bot=False), SimpleNamespace(id=2, bot=False),
            SimpleNamespace(id=99, bot=True),
        ])

    def play(self, source, after):
        self.after, self.playing = after, True

    def is_playing(self):
        return self.playing

    def stop(self):
        if self.playing:
            self.playing = False
            self.after(None)

    def end_naturally(self):
        self.playing = False
        self.after(None)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0.01)


@pytest.fixture
def harness(monkeypatch):
    async def fake_stream_url(q):
        return "http://audio"

    monkeypatch.setattr(music, "get_stream_url", fake_stream_url)
    monkeypatch.setattr(music.discord, "FFmpegPCMAudio", lambda *a, **k: object())

    def make(loop):
        cog = music.MusicCog.__new__(music.MusicCog)
        cog.bot = SimpleNamespace(loop=loop)
        cog.players = {}
        cog.spotify = None
        cog.store = Store(":memory:")
        player = cog.get_player(5)
        player.voice_client = FakeVoiceClient()
        return cog, player

    return make


def tracks(n):
    return [Track(title=f"Artist{i} - Song{i}", channel="x", video_id=f"v{i}", requested_by=1).apply_metadata()
            for i in range(n)]


def test_outcomes_and_seek(harness):
    async def scenario():
        cog, player = harness(asyncio.get_running_loop())
        vc = player.voice_client
        player.queue.extend(tracks(4))
        cog.play_next(5)
        await settle()

        # Track 0: seek, then finish naturally -> one row, completed
        player.seek_target = 30.0
        vc.stop()
        await settle()
        assert len(cog.store.get_plays(5)) == 1
        vc.end_naturally()
        await settle()

        # Track 1: skipped immediately -> skipped_early
        player.skip_requested = True
        vc.stop()
        await settle()

        # Track 2: skipped after 45s -> skipped_late
        player.start_time = time.time() - 45
        player.skip_requested = True
        vc.stop()
        await settle()

        # Track 3: /stopandclear -> stopped
        player.queue.clear()
        player.stop_requested = True
        cog._finish_current_play(player)
        player.current = None
        vc.stop()
        await settle()
        return cog, player

    cog, player = asyncio.run(scenario())
    rows = cog.store.get_plays(5)
    assert [r["outcome"] for r in rows] == ["completed", "skipped_early", "skipped_late", "stopped"]
    assert [r["track_key"] for r in rows] == [f"artist{i}|song{i}" for i in range(4)]
    assert all(r["listeners"] == 2 and r["requested_by"] == 1 for r in rows)
    assert len({r["session_id"] for r in rows}) == 1
    assert list(player.history_outcomes) == ["completed", "skipped_early", "skipped_late", "stopped"]
    assert cog.store.get_track("artist0|song0")["video_id"] == "v0"


class FakeRecommender:
    def __init__(self):
        self.n = 0

    async def recommend(self, guild_id, current, queue, history, outcomes, count=2, explore=None):
        out = []
        for _ in range(count):
            self.n += 1
            t = Track(title=f"Rec{self.n} - Auto{self.n}", channel="x", video_id=f"r{self.n}").apply_metadata()
            t.is_recommendation, t.rec_reason = True, "because you played Song0"
            out.append(t)
        return out


def test_autoplay_keeps_going_and_stops_on_stop(harness):
    sent = []

    async def scenario():
        cog, player = harness(asyncio.get_running_loop())
        cog.recommender = FakeRecommender()
        player.autoplay = True
        vc = player.voice_client
        vc.is_connected = lambda: True

        async def send(msg):
            sent.append(msg)
        player.text_channel = SimpleNamespace(send=send)

        player.queue.extend(tracks(1))
        cog.play_next(5)
        await settle()
        for _ in range(6):          # let 6 tracks finish on their own
            vc.end_naturally()
            await settle()
        played = [r["track_key"] for r in cog.store.get_plays(5)]

        # /stopandclear: autoplay must not refill
        player.queue.clear()
        player.stop_requested = True
        player.autoplay_suppressed = True
        cog._finish_current_play(player)
        player.current = None
        vc.stop()
        await settle()
        return cog, player, played

    cog, player, played = asyncio.run(scenario())
    assert len(played) == 7 and len(set(played)) == 7
    assert all(k.startswith("rec") for k in played[1:])
    rows = cog.store.get_plays(5)
    assert all(r["is_recommendation"] == 1 and r["requested_by"] is None for r in rows[1:])
    assert player.current is None and not player.queue
    assert any(m.startswith("✨ Autoplay queued **Rec1 - Auto1** — because you played Song0") for m in sent)
    assert any("🎶 Now playing **✨ Rec1 - Auto1**" in m for m in sent)
