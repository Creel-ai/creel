#!/usr/bin/env python3
"""Spotify executor - interact with Spotify Web API.

Requires SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, and SPOTIFY_REFRESH_TOKEN
environment variables.
Outputs JSON to stdout.
"""

from __future__ import annotations

import json
import os
import sys

SPOTIFY_SCOPES = "user-read-currently-playing user-read-recently-played"


def register_skill():
    """Register the spotify skill with the skill registry."""
    import json
    from typing import TYPE_CHECKING

    from creel.skills.models import Param, SkillMeta, ToolSpec

    if TYPE_CHECKING:
        from creel.models import ExecutorConfig

    meta = SkillMeta(
        id="spotify",
        label="Spotify",
        tools=(
            ToolSpec(
                name="now_playing",
                description="Get the currently playing track on Spotify (artist, song, album)",
                params=(),
            ),
            ToolSpec(
                name="recent_tracks",
                description="Get recently played tracks from Spotify",
                params=(
                    Param(
                        name="limit",
                        type="string",
                        description="Number of tracks to return (1-50, default: 10)",
                    ),
                ),
            ),
            ToolSpec(
                name="search_music",
                description="Search Spotify for tracks, artists, or albums",
                params=(
                    Param(
                        name="query",
                        type="string",
                        description="Search query (track, artist, or album name)",
                        required=True,
                    ),
                    Param(
                        name="search_type",
                        type="string",
                        description=(
                            "Type to search: 'track', 'artist', or 'album' (default: 'track')"
                        ),
                    ),
                    Param(
                        name="limit",
                        type="string",
                        description="Number of results to return (1-50, default: 5)",
                    ),
                ),
            ),
        ),
        needs_network=True,
    )

    def execute(config: ExecutorConfig) -> str:
        tool_name = config.args.get("_tool", config.name)

        if tool_name == "now_playing":
            result = get_now_playing()
        elif tool_name == "recent_tracks":
            limit = int(config.args.get("limit", "10"))
            result = get_recent_tracks(limit=limit)
        elif tool_name == "search_music":
            query = config.args.get("query", "")
            search_type = config.args.get("search_type", "track")
            limit = int(config.args.get("limit", "5"))
            result = search_music(query, search_type=search_type, limit=limit)
        else:
            result = {"error": f"Unknown tool: {tool_name}"}

        return json.dumps(result, indent=2)

    return meta, execute


def _get_client():  # -> spotipy.Spotify
    """Build an authenticated Spotify client using refresh token credentials."""
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")

    if not client_id:
        raise RuntimeError("SPOTIFY_CLIENT_ID is not set")
    if not client_secret:
        raise RuntimeError("SPOTIFY_CLIENT_SECRET is not set")
    if not refresh_token:
        raise RuntimeError("SPOTIFY_REFRESH_TOKEN is not set")

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost:8888/callback",
        scope=SPOTIFY_SCOPES,
    )
    # Inject the refresh token to obtain a fresh access token
    token_info = auth_manager.refresh_access_token(refresh_token)

    return spotipy.Spotify(auth=token_info["access_token"])


def get_now_playing() -> dict:
    """Get the currently playing track."""
    sp = _get_client()
    current = sp.current_user_playing_track()

    if not current or not current.get("item"):
        return {"playing": False, "message": "Nothing is currently playing"}

    track = current["item"]
    return {
        "playing": True,
        "is_playing": current.get("is_playing", False),
        "track": track.get("name", ""),
        "artist": ", ".join(a.get("name", "") for a in track.get("artists", [])),
        "album": track.get("album", {}).get("name", ""),
        "duration_ms": track.get("duration_ms", 0),
        "progress_ms": current.get("progress_ms", 0),
        "url": track.get("external_urls", {}).get("spotify", ""),
    }


def get_recent_tracks(*, limit: int = 10) -> list[dict]:
    """Get recently played tracks."""
    sp = _get_client()
    limit = max(1, min(limit, 50))
    results = sp.current_user_recently_played(limit=limit)

    tracks = []
    for item in results.get("items", []):
        track = item.get("track", {})
        tracks.append(
            {
                "track": track.get("name", ""),
                "artist": ", ".join(a.get("name", "") for a in track.get("artists", [])),
                "album": track.get("album", {}).get("name", ""),
                "played_at": item.get("played_at", ""),
                "url": track.get("external_urls", {}).get("spotify", ""),
            }
        )

    return tracks


def search_music(
    query: str,
    *,
    search_type: str = "track",
    limit: int = 5,
) -> list[dict]:
    """Search Spotify for tracks, artists, or albums."""
    if not query:
        raise RuntimeError("Search query is required")

    sp = _get_client()
    search_type = search_type if search_type in ("track", "artist", "album") else "track"
    limit = max(1, min(limit, 50))

    results = sp.search(q=query, type=search_type, limit=limit)

    items_key = f"{search_type}s"
    raw_items = results.get(items_key, {}).get("items", [])

    formatted = []
    for item in raw_items:
        if search_type == "track":
            formatted.append(
                {
                    "type": "track",
                    "name": item.get("name", ""),
                    "artist": ", ".join(a.get("name", "") for a in item.get("artists", [])),
                    "album": item.get("album", {}).get("name", ""),
                    "url": item.get("external_urls", {}).get("spotify", ""),
                }
            )
        elif search_type == "artist":
            formatted.append(
                {
                    "type": "artist",
                    "name": item.get("name", ""),
                    "genres": item.get("genres", []),
                    "followers": item.get("followers", {}).get("total", 0),
                    "url": item.get("external_urls", {}).get("spotify", ""),
                }
            )
        elif search_type == "album":
            formatted.append(
                {
                    "type": "album",
                    "name": item.get("name", ""),
                    "artist": ", ".join(a.get("name", "") for a in item.get("artists", [])),
                    "release_date": item.get("release_date", ""),
                    "total_tracks": item.get("total_tracks", 0),
                    "url": item.get("external_urls", {}).get("spotify", ""),
                }
            )

    return formatted


def main() -> None:
    tool = os.environ.get("TOOL", "now_playing")

    try:
        if tool == "now_playing":
            result = get_now_playing()
        elif tool == "recent_tracks":
            limit = int(os.environ.get("LIMIT", "10"))
            result = get_recent_tracks(limit=limit)
        elif tool == "search_music":
            query = os.environ.get("QUERY", "")
            search_type = os.environ.get("SEARCH_TYPE", "track")
            limit = int(os.environ.get("LIMIT", "5"))
            result = search_music(query, search_type=search_type, limit=limit)
        else:
            result = {"error": f"Unknown tool: {tool}"}
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
