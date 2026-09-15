import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} slash commands.")


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

    # Optional: log into your own Spotify account once at startup so /playlist
    # can read your PRIVATE playlists too (not just public ones).
    if os.getenv("SPOTIFY_ENABLE_PRIVATE", "false").lower() == "true":
        from utils.spotify_auth import build_user_client
        from cogs.music import MusicCog
        user_client = build_user_client(
            os.getenv("SPOTIFY_CLIENT_ID"),
            os.getenv("SPOTIFY_CLIENT_SECRET"),
            os.getenv("SPOTIFY_REDIRECT_URI"),
        )
        await bot.load_extension("cogs.music")
        cog: MusicCog = bot.get_cog("MusicCog")
        if cog.spotify:
            cog.spotify.set_user_client(user_client)
    else:
        await bot.load_extension("cogs.music")

    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
