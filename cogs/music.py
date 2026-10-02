import asyncio
import os
from collections import deque
import time
import uuid
import discord
from discord import app_commands
from discord.ext import commands

from utils.track_resolver import (
    Track, classify_url, search_youtube, search_soundcloud, multi_search,
    resolve_youtube_playlist, resolve_soundcloud_playlist, get_stream_url,
    SpotifyResolver, FFMPEG_OPTS,
)
from utils.store import Store, classify_outcome
from utils.recommender import Recommender

SEARCH_RESULTS_PER_SOURCE = int(os.getenv("SEARCH_RESULTS_PER_SOURCE", "10"))
SOUNDCLOUD_RESULTS_COUNT = int(os.getenv("SOUNDCLOUD_RESULTS_COUNT", "5"))
REC_DB_PATH = os.getenv("REC_DB_PATH", "data/bot.db")
SESSION_IDLE_SEC = 30 * 60
AUTOPLAY_BATCH = int(os.getenv("AUTOPLAY_BATCH", "2"))
PENDING_RECS_TTL = 10 * 60
FEEDBACK_VALUES = {"like": 1.5, "dislike": -1.5, "remove": -1.5}


def label(track: Track) -> str:
    """Display name with a ✨ marker for recommended tracks."""
    return f"✨ {track.display_name}" if track.is_recommendation else track.display_name


# class GuildPlayer:
#     """Per-guild queue + voice state."""
#     def __init__(self):
#         self.queue: deque[Track] = deque()
#         self.voice_client: discord.VoiceClient | None = None
#         self.current: Track | None = None
#         self.text_channel: discord.abc.Messageable | None = None
class GuildPlayer:
    """Per-guild queue + voice state."""
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.current: Track | None = None
        self.text_channel: discord.abc.Messageable | None = None
        
        # New time-tracking attributes
        self.start_time: float = 0.0
        self.pause_time: float = 0.0
        self.total_pause_duration: float = 0.0

        # New seek flag
        self.seek_target: float | None = None

        # Recommendation / play-logging state
        self.history: deque[Track] = deque(maxlen=10)
        self.history_outcomes: deque[str] = deque(maxlen=10)
        self.session_id: str = str(uuid.uuid4())
        self.last_activity: float = time.time()
        self.current_play_id: int | None = None
        self.skip_requested: bool = False
        self.stop_requested: bool = False
        self.playback_error: bool = False

        # Autoplay state
        self.autoplay: bool = False
        self.autoplay_suppressed: bool = False   # set by /stopandclear and /leave
        self.pending_recs: list[Track] = []
        self.pending_at: float = 0.0
        self.session_feedback: list[tuple[Track, float]] = []   # from 👍 / 👎 / Remove buttons
        self.refilling: bool = False

    def new_session(self):
        """Start a fresh listening session: earlier tracks stop influencing recommendations."""
        self.session_id = str(uuid.uuid4())
        self.last_activity = time.time()
        self.history.clear()
        self.history_outcomes.clear()
        self.session_feedback = []
        self.pending_recs = []

    def elapsed(self) -> float:
        if not self.start_time:
            return 0.0
        end = self.pause_time if self.pause_time > 0 else time.time()
        return max(0.0, end - self.start_time - self.total_pause_duration)


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot, spotify_resolver: SpotifyResolver | None):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.spotify = spotify_resolver
        self.store = Store(REC_DB_PATH)
        self.recommender = Recommender(self.store)

    async def cog_unload(self):
        await self.recommender.close()

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            player = GuildPlayer()
            try:
                player.autoplay = self.store.get_guild_settings(guild_id)["autoplay"]
            except Exception as e:
                print(f"Couldn't load guild settings: {e}")
            self.players[guild_id] = player
        return self.players[guild_id]

    # ------------------------------------------------------------------
    # Voice connection helpers
    # ------------------------------------------------------------------

    async def ensure_voice(self, interaction: discord.Interaction) -> GuildPlayer:
        player = self.get_player(interaction.guild_id)
        if player.voice_client is None or not player.voice_client.is_connected():
            if interaction.user.voice is None:
                raise RuntimeError("You need to be in a voice channel first.")
            player.voice_client = await interaction.user.voice.channel.connect()
            player.new_session()
        player.text_channel = interaction.channel
        return player

    # ------------------------------------------------------------------
    # Play logging (never allowed to break playback)
    # ------------------------------------------------------------------

    def _finish_current_play(self, player: GuildPlayer):
        """Log how the current track ended and push it onto the history."""
        track = player.current
        play_id = player.current_play_id
        player.current_play_id = None
        if track is not None and play_id is not None:
            listened = player.elapsed()
            outcome = classify_outcome(listened, player.skip_requested,
                                       player.stop_requested, player.playback_error)
            player.history.append(track)
            player.history_outcomes.append(outcome)
            asyncio.ensure_future(self._safe_store("finish_play", play_id, int(listened), outcome))
        player.skip_requested = False
        player.stop_requested = False
        player.playback_error = False
        player.last_activity = time.time()

    async def _log_play_start(self, guild_id: int, player: GuildPlayer, track: Track):
        if not track.canonical_key:
            track.apply_metadata()
        listener_ids = []
        if player.voice_client and player.voice_client.channel:
            listener_ids = [m.id for m in player.voice_client.channel.members if not m.bot]
        await self._safe_store("upsert_track", track.canonical_key, track.canonical_artist,
                               track.canonical_title, track.video_id, track.duration)
        player.current_play_id = await self._safe_store(
            "insert_play", guild_id, player.session_id, track.canonical_key,
            track.requested_by, track.is_recommendation, track.duration, listener_ids)

    async def _safe_store(self, method: str, *args):
        try:
            return await self.store.run(method, *args)
        except Exception as e:
            print(f"Play logging failed ({method}): {e}")
            return None

    # ------------------------------------------------------------------
    # Autoplay (never allowed to break playback)
    # ------------------------------------------------------------------

    async def _compute_recs(self, guild_id: int, player: GuildPlayer, count: int) -> list[Track]:
        return await self.recommender.recommend(
            guild_id, player.current, list(player.queue),
            list(player.history), list(player.history_outcomes), count=count,
            feedback=list(player.session_feedback))

    # ------------------------------------------------------------------
    # Feedback on recommendations (👍 / 👎 / Remove buttons)
    # ------------------------------------------------------------------

    async def record_feedback(self, guild_id: int, track: Track, user_id: int, action: str):
        value = FEEDBACK_VALUES[action]
        player = self.get_player(guild_id)
        player.session_feedback.append((track, value))
        player.pending_recs = []   # precomputed picks didn't know about this
        await self._safe_store("add_feedback", guild_id, player.session_id, track.canonical_key,
                               track.video_id, user_id, value, action)

    def remove_upcoming(self, guild_id: int, track: Track) -> str:
        """Take a recommended track out of play. Returns 'queue', 'skipped' or 'gone'."""
        player = self.get_player(guild_id)
        for t in list(player.queue):
            if t is track:
                player.queue.remove(t)
                self._maybe_autoplay(guild_id)
                return "queue"
        if player.current is track and player.voice_client and player.voice_client.is_playing():
            player.skip_requested = True   # log it as a skip, never as "completed"
            player.voice_client.stop()     # after= -> play_next
            return "skipped"
        return "gone"

    @staticmethod
    def _is_known(player: GuildPlayer, track: Track) -> bool:
        for t in [player.current, *player.queue, *player.history]:
            if t and ((track.video_id and t.video_id == track.video_id)
                      or (track.canonical_key and t.canonical_key == track.canonical_key)):
                return True
        return False

    async def _prefetch_recs(self, guild_id: int):
        player = self.players.get(guild_id)
        if not player or not player.autoplay or player.refilling:
            return
        try:
            player.pending_recs = await self._compute_recs(guild_id, player, AUTOPLAY_BATCH)
            player.pending_at = time.time()
        except Exception as e:
            print(f"Autoplay prefetch failed: {e}")

    def _maybe_autoplay(self, guild_id: int):
        player = self.players.get(guild_id)
        if (player and player.autoplay and not player.autoplay_suppressed
                and not player.refilling and len(player.queue) <= 1):
            asyncio.ensure_future(self._autoplay_refill(guild_id))

    async def _autoplay_refill(self, guild_id: int):
        player = self.players.get(guild_id)
        if not player or player.refilling:
            return
        player.refilling = True
        try:
            recs = []
            if time.time() - player.pending_at < PENDING_RECS_TTL:
                recs = [t for t in player.pending_recs if not self._is_known(player, t)]
            player.pending_recs = []
            if len(recs) < AUTOPLAY_BATCH:
                recs = await self._compute_recs(guild_id, player, AUTOPLAY_BATCH)
            recs = recs[:AUTOPLAY_BATCH]

            vc = player.voice_client
            if (not recs or player.autoplay_suppressed or not player.autoplay
                    or vc is None or not vc.is_connected()):
                return
            player.queue.extend(recs)
            if player.text_channel:
                for t in recs:
                    reason = f" — {t.rec_reason}" if t.rec_reason else ""
                    await player.text_channel.send(f"✨ Autoplay queued **{t.display_name}**{reason}",
                                                   view=RecFeedbackView(self, guild_id, t, playing=False))
            if player.current is None and not vc.is_playing():
                self.play_next(guild_id)
        except Exception as e:
            print(f"Autoplay failed, skipping this refill: {e}")
        finally:
            player.refilling = False

    def play_next(self, guild_id: int):
        player = self.players.get(guild_id)
        if not player:
            return

        # Check if we are seeking inside the current track
        if getattr(player, "seek_target", None) is not None:
            track = player.current
            start_offset = player.seek_target
            player.seek_target = None  # Reset for next time
        else:
            self._finish_current_play(player)
            if not player.queue:
                player.current = None
                self._maybe_autoplay(guild_id)
                return
            track = player.queue.popleft()
            player.current = track
            self._maybe_autoplay(guild_id)
            start_offset = 0.0
            if time.time() - player.last_activity > SESSION_IDLE_SEC:
                player.new_session()

        async def _start():
            try:
                stream_url = await get_stream_url(track.stream_query)
            except Exception as e:
                if player.text_channel:
                    await player.text_channel.send(f"⚠️ Couldn't stream **{track.display_name}**: {e}")
                self.bot.loop.call_soon_threadsafe(lambda: self.play_next(guild_id))
                return

            # Apply FFmpeg -ss parameter for fast seeking
            custom_ffmpeg_opts = dict(FFMPEG_OPTS)
            if start_offset > 0:
                existing = custom_ffmpeg_opts.get("before_options", "")
                custom_ffmpeg_opts["before_options"] = f"-ss {start_offset} {existing}"

            source = discord.FFmpegPCMAudio(stream_url, **custom_ffmpeg_opts)

            def _after(err):
                if err:
                    print(f"Playback error: {err}")
                    player.playback_error = True
                self.bot.loop.call_soon_threadsafe(lambda: self.play_next(guild_id))

            # Set the start time minus the offset so /nowplaying accurately tracks progress
            player.start_time = time.time() - start_offset
            player.pause_time = 0.0
            player.total_pause_duration = 0.0

            if start_offset == 0:
                await self._log_play_start(guild_id, player, track)

            player.voice_client.play(source, after=_after)
            
            # Announce only for new tracks, not seeks
            if start_offset == 0 and player.text_channel:
                view = RecFeedbackView(self, guild_id, track, playing=True) if track.is_recommendation else None
                await player.text_channel.send(
                    f"🎶 Now playing **{label(track)}** — *{track.source}*",
                    **({"view": view} if view else {}),
                )
            if start_offset == 0 and player.autoplay and not player.autoplay_suppressed:
                asyncio.ensure_future(self._prefetch_recs(guild_id))

        asyncio.run_coroutine_threadsafe(_start(), self.bot.loop)

    async def queue_tracks(self, interaction: discord.Interaction, tracks: list[Track]):
        player = await self.ensure_voice(interaction)
        player.autoplay_suppressed = False
        for t in tracks:
            if t.requested_by is None and not t.is_recommendation:
                t.requested_by = interaction.user.id
        player.queue.extend(tracks)
        if player.voice_client and not player.voice_client.is_playing() and player.current is None:
            self.play_next(interaction.guild_id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @app_commands.command(name="play", description="Play a song by name or URL (YouTube, SoundCloud, Spotify link)")
    @app_commands.describe(query="Song name, or a YouTube/SoundCloud/Spotify URL")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        try:
            kind = classify_url(query)
            tracks: list[Track] = []

            if kind == "spotify_track" and self.spotify:
                t = await self.spotify.resolve_track(query)
                tracks = [t] if t else []
            elif kind == "youtube":
                tracks = await resolve_youtube_playlist(query)  # single video also works
                tracks = tracks[:1] if tracks else []
            elif kind == "soundcloud":
                tracks = await resolve_soundcloud_playlist(query)
                tracks = tracks[:1] if tracks else []
            else:
                results = await search_youtube(query, limit=1)
                tracks = results[:1]

            if not tracks:
                await interaction.followup.send("Couldn't find that track anywhere.")
                return

            await self.queue_tracks(interaction, tracks)
            await interaction.followup.send(f"✅ Queued **{tracks[0].display_name}** — *{tracks[0].source}*")
        except RuntimeError as e:
            await interaction.followup.send(str(e))
        except Exception as e:
            await interaction.followup.send(f"Something went wrong: {e}")

    @app_commands.command(name="playlist", description="Queue a full album or playlist (YouTube, SoundCloud, or Spotify link)")
    @app_commands.describe(url="Playlist/album URL", private="Is this YOUR private Spotify playlist? (requires login setup)")
    async def playlist(self, interaction: discord.Interaction, url: str, private: bool = False):
        await interaction.response.defer()
        try:
            kind = classify_url(url)
            tracks: list[Track] = []

            if kind == "spotify_playlist" and self.spotify:
                tracks = await self.spotify.resolve_playlist(url, private=private)
            elif kind == "spotify_album" and self.spotify:
                tracks = await self.spotify.resolve_album(url)
            elif kind == "youtube":
                tracks = await resolve_youtube_playlist(url)
            elif kind == "soundcloud":
                tracks = await resolve_soundcloud_playlist(url)
            else:
                await interaction.followup.send("That doesn't look like a playlist/album URL I can read.")
                return

            if not tracks:
                await interaction.followup.send("No tracks found (or I couldn't match them to playable audio).")
                return

            await self.queue_tracks(interaction, tracks)
            await interaction.followup.send(f"✅ Queued **{len(tracks)} tracks** from that {kind.replace('_', ' ')}.")
        except RuntimeError as e:
            await interaction.followup.send(str(e))
        except Exception as e:
            await interaction.followup.send(f"Something went wrong: {e}")

    @app_commands.command(name="playlistclear", description="Clear all upcoming tracks from the queue without stopping current playback")
    async def playlistclear(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        queue_size = len(player.queue)
        
        if queue_size == 0:
            await interaction.response.send_message("The queue is already empty.")
            return
            
        player.queue.clear()
        await interaction.response.send_message(f"🗑️ Cleared **{queue_size}** tracks from the queue. The current song will continue playing.")

    @app_commands.command(name="search", description="Search for a song across platforms and see the source of each result")
    @app_commands.describe(query="What to search for")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        results = await multi_search(
            query, 
            yt_limit=SEARCH_RESULTS_PER_SOURCE, 
            sc_limit=SOUNDCLOUD_RESULTS_COUNT
        )
        if not results:
            await interaction.followup.send("No results found.")
            return

        view = SearchResultsView(self, results, interaction.user.id)
        lines = [f"**{i+1}.** {t.display_name} — *{t.source}*" for i, t in enumerate(results)]
        await interaction.followup.send("\n".join(lines), view=view)

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client and player.voice_client.is_playing():
            player.skip_requested = True
            player.voice_client.stop()  # triggers `after=` -> play_next
            await interaction.response.send_message("⏭️ Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing.")

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client and player.voice_client.is_playing():
            player.voice_client.pause()
            player.pause_time = time.time()  # Log when it was paused
            await interaction.response.send_message("⏸️ Paused.")
        else:
            await interaction.response.send_message("Nothing is playing.")

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client and player.voice_client.is_paused():
            player.voice_client.resume()
            if player.pause_time > 0:
                # Accumulate the time spent paused
                player.total_pause_duration += (time.time() - player.pause_time)
                player.pause_time = 0.0
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("Nothing is paused.")

    @app_commands.command(name="seek", description="Jump to a specific time in the current track")
    @app_commands.describe(timestamp="Time to jump to (e.g. 1:25 or 85)")
    async def seek(self, interaction: discord.Interaction, timestamp: str):
        player = self.get_player(interaction.guild_id)
        if not player.current or not player.voice_client:
            await interaction.response.send_message("Nothing is playing right now.")
            return

        try:
            parts = timestamp.split(":")
            if len(parts) == 3:
                target = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif len(parts) == 2:
                target = int(parts[0]) * 60 + int(parts[1])
            else:
                target = int(parts[0])
        except ValueError:
            await interaction.response.send_message("Invalid format. Use seconds (90) or MM:SS (1:30).")
            return

        # Ensure we don't seek past the end of the song
        if player.current.duration > 0:
            target = min(max(0, target), player.current.duration - 1)
        else:
            target = max(0, target)

        player.seek_target = float(target)
        player.voice_client.stop() # Automatically triggers play_next to handle the seek
        await interaction.response.send_message(f"🔄 Jumped to {timestamp}.")

    @app_commands.command(name="jump", description="Skip forward or backward by N seconds")
    @app_commands.describe(seconds="Seconds to skip (use negative numbers to rewind)")
    async def jump(self, interaction: discord.Interaction, seconds: int):
        player = self.get_player(interaction.guild_id)
        if not player.current or not player.voice_client:
            await interaction.response.send_message("Nothing is playing right now.")
            return

        # Fetch current elapsed time
        if player.pause_time > 0:
            elapsed = player.pause_time - player.start_time - player.total_pause_duration
        else:
            elapsed = time.time() - player.start_time - player.total_pause_duration
            
        target = elapsed + seconds
        
        # Ensure we don't seek past the end of the song or into negative time
        if player.current.duration > 0:
            target = min(max(0, target), player.current.duration - 1)
        else:
            target = max(0, target)

        player.seek_target = float(target)
        player.voice_client.stop()
        
        direction = "⏩ Skipped forward" if seconds > 0 else "⏪ Rewound"
        await interaction.response.send_message(f"{direction} by {abs(seconds)} seconds.")

    @app_commands.command(name="stopandclear", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        player.queue.clear()
        player.stop_requested = True
        player.autoplay_suppressed = True
        player.pending_recs = []
        self._finish_current_play(player)
        player.new_session()
        player.current = None
        if player.voice_client:
            player.voice_client.stop()
        await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

    @app_commands.command(name="queue", description="Show the current queue")
    async def show_queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        lines = []
        if player.current:
            lines.append(f"**Now Playing:** {label(player.current)} — *{player.current.source}*")
        if player.queue:
            lines.append("\n**Up Next:**")
            for i, t in enumerate(list(player.queue)[:15], 1):
                lines.append(f"{i}. {label(t)} — *{t.source}*")
            if len(player.queue) > 15:
                lines.append(f"...and {len(player.queue) - 15} more")
        if not lines:
            lines = ["The queue is empty."]
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if not player.current or not player.start_time:
            await interaction.response.send_message("Nothing is playing.")
            return

        # Calculate active elapsed time
        current_time = time.time()
        if player.pause_time > 0:
            elapsed = player.pause_time - player.start_time - player.total_pause_duration
        else:
            elapsed = current_time - player.start_time - player.total_pause_duration

        def format_time(seconds: int) -> str:
            m, s = divmod(int(seconds), 60)
            h, m = divmod(m, 60)
            return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

        elapsed_str = format_time(elapsed)
        total_duration = player.current.duration
        
        # Build the progress bar
        bar_length = 20
        if total_duration and total_duration > 0:
            progress = min(1.0, max(0.0, elapsed / total_duration))
            filled_blocks = int(bar_length * progress)
            bar = "▬" * filled_blocks + "🔘" + "▬" * (bar_length - filled_blocks - 1)
            total_str = format_time(total_duration)
        else:
            # Fallback for livestreams or missing duration metadata
            bar = "🔴 ▬ ▬ ▬ ▬ ▬ LIVE ▬ ▬ ▬ ▬ ▬"
            total_str = "∞"

        await interaction.response.send_message(
            f"🎶 **{label(player.current)}** — *{player.current.source}*\n"
            f"`{elapsed_str} {bar} {total_str}`\n"
            f"{player.current.webpage_url}"
        )

    @app_commands.command(name="autoplay", description="Keep the music going with recommendations when the queue runs low")
    @app_commands.describe(mode="Turn autoplay on or off for this server")
    @app_commands.choices(mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def autoplay(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        player = self.get_player(interaction.guild_id)
        enabled = mode.value == "on"
        player.autoplay = enabled
        player.pending_recs = []
        await self.store.run("set_guild_setting", interaction.guild_id, autoplay=enabled)
        if enabled:
            player.autoplay_suppressed = False
            await interaction.response.send_message(
                "✨ Autoplay **on** — I'll add similar songs when the queue runs low.")
            if player.current is not None:
                self._maybe_autoplay(interaction.guild_id)
        else:
            await interaction.response.send_message("Autoplay **off**.")

    @app_commands.command(name="recommend", description="Suggest songs based on what's playing and queued")
    @app_commands.describe(count="How many suggestions (1-10)")
    async def recommend(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 10] = 5):
        await interaction.response.defer()
        player = self.get_player(interaction.guild_id)
        try:
            recs = await self._compute_recs(interaction.guild_id, player, count)
        except Exception as e:
            print(f"/recommend failed: {e}")
            recs = []
        if not recs:
            await interaction.followup.send(
                "No recommendations yet — play something first (I need a song to go on).")
            return
        view = SearchResultsView(self, recs, interaction.user.id)
        lines = [f"**{i+1}.** ✨ {t.display_name}" + (f" — *{t.rec_reason}*" if t.rec_reason else "")
                 for i, t in enumerate(recs)]
        await interaction.followup.send("\n".join(lines), view=view)

    @app_commands.command(name="leave", description="Disconnect the bot from voice")
    async def leave(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client:
            player.queue.clear()
            player.stop_requested = True
            player.autoplay_suppressed = True
            player.pending_recs = []
            self._finish_current_play(player)
            player.new_session()
            player.current = None
            await player.voice_client.disconnect()
            player.current = None
        await interaction.response.send_message("👋 Disconnected.")


class RecFeedbackView(discord.ui.View):
    """Buttons under autoplay / now-playing messages for recommended tracks.

    Queued:  👍  |  🗑️ Remove (takes it out of the queue)
    Playing: 👍  |  👎 Skip
    Any member can press them; each press is logged as feedback for that track.
    """
    def __init__(self, cog: "MusicCog", guild_id: int, track: Track, playing: bool):
        super().__init__(timeout=3 * 3600)
        self.cog, self.guild_id, self.track = cog, guild_id, track
        self.message_note = ""
        like = discord.ui.Button(emoji="👍", style=discord.ButtonStyle.secondary)
        like.callback = self.on_like
        self.add_item(like)
        bad = discord.ui.Button(emoji="👎", label="Skip", style=discord.ButtonStyle.secondary) if playing else \
            discord.ui.Button(emoji="🗑️", label="Remove", style=discord.ButtonStyle.secondary)
        bad.callback = self.on_dislike if playing else self.on_remove
        self.add_item(bad)

    async def _finish(self, interaction: discord.Interaction, note: str):
        for item in self.children:
            item.disabled = True
        content = f"{interaction.message.content}\n-# {note}"
        await interaction.response.edit_message(content=content, view=self)
        self.stop()

    async def on_like(self, interaction: discord.Interaction):
        await self.cog.record_feedback(self.guild_id, self.track, interaction.user.id, "like")
        await self._finish(interaction, f"👍 {interaction.user.display_name} liked this — more like it coming")

    async def on_dislike(self, interaction: discord.Interaction):
        await self.cog.record_feedback(self.guild_id, self.track, interaction.user.id, "dislike")
        where = self.cog.remove_upcoming(self.guild_id, self.track)
        did = "skipped it" if where == "skipped" else "noted"
        await self._finish(interaction, f"👎 {interaction.user.display_name} disliked this — {did}, "
                                        f"won't recommend it again for a while")

    async def on_remove(self, interaction: discord.Interaction):
        await self.cog.record_feedback(self.guild_id, self.track, interaction.user.id, "remove")
        where = self.cog.remove_upcoming(self.guild_id, self.track)
        did = {"queue": "removed from the queue", "skipped": "skipped it"}.get(where, "it already played")
        await self._finish(interaction, f"🗑️ {interaction.user.display_name}: {did} — "
                                        f"won't recommend it again for a while")


class SearchResultsView(discord.ui.View):
    """Lets the user pick a numbered result from /search to queue it."""
    def __init__(self, cog: MusicCog, results: list[Track], author_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.results = results
        self.author_id = author_id
        self.add_item(SearchResultSelect(results))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id


class SearchResultSelect(discord.ui.Select):
    def __init__(self, results: list[Track]):
        options = [
            discord.SelectOption(
                label=t.display_name[:100],
                description=t.source,
                value=str(i),
            )
            for i, t in enumerate(results)
        ]
        super().__init__(placeholder="Pick a track to queue...", options=options)
        self.results = results

    async def callback(self, interaction: discord.Interaction):
        track = self.results[int(self.values[0])]
        track.requested_by = interaction.user.id
        cog: MusicCog = interaction.client.get_cog("MusicCog")
        await interaction.response.defer()
        await cog.queue_tracks(interaction, [track])
        await interaction.followup.send(f"✅ Queued **{label(track)}** — *{track.source}*")


async def setup(bot: commands.Bot):
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    spotify_resolver = SpotifyResolver(client_id, client_secret) if client_id and client_secret else None
    await bot.add_cog(MusicCog(bot, spotify_resolver))
