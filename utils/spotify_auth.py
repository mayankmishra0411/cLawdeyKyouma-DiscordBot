"""
spotify_auth.py

Public Spotify playlists work with just a Client ID/Secret (app-only auth).
To read a PRIVATE playlist on your own account, Spotify requires you to log in
once via OAuth (Authorization Code flow) and grant the 'playlist-read-private'
scope. This module runs a tiny local web server to catch the redirect and
caches the resulting token to disk (.spotify_cache) so you only log in once.

There is no equivalent for Apple Music without paying for an Apple Developer
account ($99/yr) and generating a MusicKit token, since Apple gates its
playlist API behind that program - see README for details.
"""

import threading
import webbrowser
from flask import Flask, request
import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPE = "playlist-read-private playlist-read-collaborative user-library-read"


def build_user_client(client_id: str, client_secret: str, redirect_uri: str) -> spotipy.Spotify:
    """
    Blocks until the user completes the browser login, then returns an
    authenticated Spotify client that can read private playlists.
    Call this once at startup if SPOTIFY_ENABLE_PRIVATE=true.
    """
    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPE,
        cache_path=".spotify_cache",
        open_browser=False,
    )

    # Already cached from a previous run
    token_info = auth_manager.get_cached_token()
    if token_info:
        return spotipy.Spotify(auth_manager=auth_manager)

    auth_url = auth_manager.get_authorize_url()
    print("\n[Spotify] Open this URL, log in, and approve access:")
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    code_holder = {}
    app = Flask(__name__)

    @app.route("/callback")
    def callback():
        code_holder["code"] = request.args.get("code")
        return "Spotify login complete - you can close this tab and return to the bot."

    port = int(redirect_uri.rsplit(":", 1)[-1].split("/")[0])

    server_thread = threading.Thread(
        target=lambda: app.run(port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    print("[Spotify] Waiting for login in browser...")
    while "code" not in code_holder:
        pass  # simple blocking wait; startup-time only

    token_info = auth_manager.get_access_token(code_holder["code"], as_dict=True)
    return spotipy.Spotify(auth_manager=auth_manager)
