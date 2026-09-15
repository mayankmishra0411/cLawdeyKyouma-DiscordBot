# Discord Music Bot

A self-hosted Discord bot that plays music from YouTube and SoundCloud directly,
and resolves Spotify links (tracks, albums, playlists, including your own
private ones) by matching them to playable audio.

## Why it's built this way (read this first)

No app — free or paid, official or third-party — can stream raw audio directly
from Spotify's or Apple Music's API. Both companies only expose **metadata**
(track names, artists, album art, track order) through their public APIs;
actual audio playback is locked to their own licensed apps. This isn't a
missing feature here, it's how their platforms work for every developer.

So for Spotify/Apple Music input, the bot:
1. Reads the track/album/playlist metadata from Spotify's API.
2. Searches YouTube for matching audio. (It utilizes a sequential circuit-breaker pattern: it searches YouTube first and only falls back to SoundCloud if YouTube fails, saving memory and API limits).
3. Streams that. The `/search`, `/queue`, and "now playing" messages always
   show you the **actual source of the audio**, e.g. `Spotify -> YouTube`,
   so you always know where it's really coming from.

## Features

- `/play <query or URL>` — plays a song by name, or from a YouTube/SoundCloud URL, or a Spotify track link.
- `/search <query>` — shows top matches from platforms, querying YouTube first and falling back to SoundCloud if needed. 
- `/playlist <url>` — queues an entire YouTube/SoundCloud playlist or a Spotify album/playlist. Pass `private:true` for your own private Spotify playlists.
- `/nowplaying` — Shows the currently playing track along with a dynamic ASCII progress bar and precise elapsed timestamps.
- `/seek <timestamp>` — Jump directly to a specific timestamp in the current track (e.g., `1:25` or `85`).
- `/jump <seconds>` — Skip forward or rewind backward by a specific number of seconds (use negative numbers to rewind).
- `/playlistclear` — Instantly wipes all upcoming tracks from the queue without interrupting the currently playing song.
- `/queue`, `/skip`, `/pause`, `/resume`, `/stop`, `/leave`.

## Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

You also need **ffmpeg** installed and on your PATH:
- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: download from ffmpeg.org and add the `bin` folder to PATH

### 2. Create the Discord bot

1. Go to https://discord.com/developers/applications → New Application.
2. Bot tab → Add Bot → copy the token.
3. Under "Privileged Gateway Intents", enable **Message Content Intent**.
4. OAuth2 → URL Generator → scopes: `bot`, `applications.commands`.
   Bot permissions: Connect, Speak, Send Messages, Use Slash Commands.
5. Use the generated URL to invite the bot to your server.

### 3. (Optional but recommended) Spotify app

Only needed if you want Spotify links/playlists to work at all.

1. Go to https://developer.spotify.com/dashboard → Create App.
2. Copy the Client ID and Client Secret.
3. If you want your own **private** playlists readable, add
   `http://127.0.0.1:8888/callback` as a Redirect URI in the app settings.

### 4. Configure environment

```bash
cp .env.example .env
```

Fill in `DISCORD_TOKEN`, and optionally `SPOTIFY_CLIENT_ID` /
`SPOTIFY_CLIENT_SECRET`. 
- Set `SPOTIFY_ENABLE_PRIVATE=true` for private-playlist support.
- Set `SEARCH_RESULTS_PER_SOURCE=3` to control YouTube results.
- Set `SOUNDCLOUD_RESULTS_COUNT=5` to control fallback results.

### 5. Run it

```bash
python bot.py
```

If `SPOTIFY_ENABLE_PRIVATE=true`, a browser tab will open on first run asking
you to log into Spotify and approve access — you only need to do this once,
the token gets cached to `.spotify_cache`.

## Notes & limits

- yt-dlp occasionally needs updating as YouTube changes its site —
  `pip install -U yt-dlp` if playback starts failing.
- Very large playlists (100+ tracks) take a few seconds to resolve since each
  Spotify track needs a matching YouTube search.
- This bot is for personal/self-hosted use across servers you control. Playing
  music this way is a widely used pattern (it's how most public Discord music
  bots work under the hood), but you're responsible for complying with
  YouTube's and SoundCloud's Terms of Service in your jurisdiction.