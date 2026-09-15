import asyncio
import os
from collections import deque
import time
import discord
from discord import app_commands
from discord.ext import commands

from utils.track_resolver import (
    Track, classify_url, search_youtube, search_soundcloud, multi_search,
    resolve_youtube_playlist, resolve_soundcloud_playlist, get_stream_url,
    SpotifyResolver, FFMPEG_OPTS,
)

SEARCH_RESULTS_PER_SOURCE = int(os.getenv("SEARCH_RESULTS_PER_SOURCE", "10"))
SOUNDCLOUD_RESULTS_COUNT = int(os.getenv("SOUNDCLOUD_RESULTS_COUNT", "5"))


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


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot, spotify_resolver: SpotifyResolver | None):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.spotify = spotify_resolver

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer()
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
        player.text_channel = interaction.channel
        return player

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
            if not player.queue:
                player.current = None
                return
            track = player.queue.popleft()
            player.current = track
            start_offset = 0.0

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
                self.bot.loop.call_soon_threadsafe(lambda: self.play_next(guild_id))

            # Set the start time minus the offset so /nowplaying accurately tracks progress
            player.start_time = time.time() - start_offset
            player.pause_time = 0.0
            player.total_pause_duration = 0.0

            player.voice_client.play(source, after=_after)
            
            # Announce only for new tracks, not seeks
            if start_offset == 0 and player.text_channel:
                await player.text_channel.send(
                    f"🎶 Now playing **{track.display_name}** — *{track.source}*"
                )

        asyncio.run_coroutine_threadsafe(_start(), self.bot.loop)

    async def queue_tracks(self, interaction: discord.Interaction, tracks: list[Track]):
        player = await self.ensure_voice(interaction)
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
        player.current = None
        if player.voice_client:
            player.voice_client.stop()
        await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

    @app_commands.command(name="queue", description="Show the current queue")
    async def show_queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        lines = []
        if player.current:
            lines.append(f"**Now Playing:** {player.current.display_name} — *{player.current.source}*")
        if player.queue:
            lines.append("\n**Up Next:**")
            for i, t in enumerate(list(player.queue)[:15], 1):
                lines.append(f"{i}. {t.display_name} — *{t.source}*")
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
            f"🎶 **{player.current.display_name}** — *{player.current.source}*\n"
            f"`{elapsed_str} {bar} {total_str}`\n"
            f"{player.current.webpage_url}"
        )

    @app_commands.command(name="leave", description="Disconnect the bot from voice")
    async def leave(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if player.voice_client:
            await player.voice_client.disconnect()
            player.queue.clear()
            player.current = None
        await interaction.response.send_message("👋 Disconnected.")


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
        cog: MusicCog = interaction.client.get_cog("MusicCog")
        await interaction.response.defer()
        await cog.queue_tracks(interaction, [track])
        await interaction.followup.send(f"✅ Queued **{track.display_name}** — *{track.source}*")


async def setup(bot: commands.Bot):
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    spotify_resolver = SpotifyResolver(client_id, client_secret) if client_id and client_secret else None
    await bot.add_cog(MusicCog(bot, spotify_resolver))
