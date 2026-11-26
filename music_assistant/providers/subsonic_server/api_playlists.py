"""Subsonic playlist methods: MA's own playlists and the Apple Music playlists in the library."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.helpers import create_uri

from .api_library import track_from_id
from .apple import PREFIX_APPLE_TRACK, apple_id
from .constants import APPLE_DOMAIN, ERR_GENERIC, ERR_NOT_AUTHORIZED
from .context import RequestContext, api_method
from .index import IndexedTrack
from .mapping import EMPTY_DATE, PREFIX_PLAYLIST, iso_time, make_id
from .playlists import BUILTIN
from .responses import SubsonicError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.media_items import Track


@api_method("getPlaylists")
async def get_playlists(ctx: RequestContext) -> dict[str, Any]:
    """Return MA's own playlists and the Apple Music playlists in the library."""
    book = ctx.server.playlists
    book.start()
    entries = []
    for row in await book.rows():
        count, duration = book.count(row) or (0, 0)
        entries.append(_playlist_entry(ctx, row, count, duration))
    return {"playlists": {"playlist": entries} if entries else {}}


@api_method("getPlaylist")
async def get_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Return a playlist with its songs, read fresh."""
    row = await ctx.server.playlists.row(ctx.require("id"))
    return await _playlist_response(ctx, row)


@api_method("createPlaylist")
async def create_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Create a playlist in MA (or replace the songs of an existing editable one)."""
    playlists = ctx.server.mass.music.playlists
    song_uris = [_track_uri(ctx, v) for v in ctx.get_all("songId")]
    if (existing := ctx.get("playlistId")) is not None:
        row = _editable(await ctx.server.playlists.row(existing))
        playlist_id = int(row["item_id"])
        items = await ctx.server.playlists.items(playlist_id, refresh=True, everything=True)
        if positions := tuple(p for p, _ in items):
            await _remove(ctx, playlist_id, positions)
    else:
        playlist = await playlists.create_playlist(ctx.require("name"))
        playlist_id = int(playlist.item_id)
    if song_uris:
        await _add(ctx, playlist_id, song_uris)
    row = await ctx.server.playlists.row(make_id(PREFIX_PLAYLIST, playlist_id))
    return await _playlist_response(ctx, row)


@api_method("updatePlaylist")
async def update_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Add songs to or remove songs from a playlist that can be edited."""
    row = _editable(await ctx.server.playlists.row(ctx.require("playlistId")))
    playlist_id = int(row["item_id"])
    remove = {int(v) for v in ctx.get_all("songIndexToRemove") if v.isdigit()}
    if remove:
        items = await ctx.server.playlists.items(playlist_id, refresh=True)
        positions = tuple(p for i, (p, _) in enumerate(items) if i in remove)
        if positions:
            await _remove(ctx, playlist_id, positions)
    song_uris = [_track_uri(ctx, v) for v in ctx.get_all("songIdToAdd")]
    if song_uris:
        await _add(ctx, playlist_id, song_uris)
    if ctx.get("name") or ctx.get("comment"):
        ctx.note = " (renaming and comments are not supported, ignored)"
    return {}


@api_method("deletePlaylist")
async def delete_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Delete one of MA's own playlists (Apple Music playlists are deleted in Apple Music)."""
    row = await ctx.server.playlists.row(ctx.require("id"))
    if row["provider_domain"] != BUILTIN:
        raise SubsonicError(
            ERR_NOT_AUTHORIZED, "Apple Music playlists can only be deleted in Apple Music"
        )
    await ctx.server.mass.music.remove_item_from_library(MediaType.PLAYLIST, int(row["item_id"]))
    return {}


async def _playlist_response(ctx: RequestContext, row: Mapping[str, Any]) -> dict[str, Any]:
    """
    Return the playlist response with its songs, reading the playlist fresh.

    :param ctx: The request context.
    :param row: The playlist's database row.
    """
    items = await ctx.server.playlists.items(int(row["item_id"]), refresh=True)
    songs = [entry for _, song in items if (entry := _song(ctx, song)) is not None]
    entry = _playlist_entry(ctx, row, len(songs), sum(int(s.get("duration") or 0) for s in songs))
    entry["entry"] = songs
    return {"playlist": entry}


def _song(ctx: RequestContext, song: IndexedTrack | Track | None) -> dict[str, Any] | None:
    """
    Return the song entry of a playlist song.

    :param ctx: The request context.
    :param song: A song with a file, or an Apple Music track.
    """
    if song is None:
        return None
    if isinstance(song, IndexedTrack):
        return ctx.mapper.song(song)
    return ctx.mapper.apple_song(song)


def _editable(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """
    Return the row of a playlist that can be edited.

    :param row: The playlist's database row.
    :raises SubsonicError: Not allowed (50) for a read-only playlist.
    """
    if not row["is_editable"]:
        raise SubsonicError(ERR_NOT_AUTHORIZED, "This playlist can't be changed")
    return row


async def _add(ctx: RequestContext, playlist_id: int, uris: list[str]) -> None:
    """
    Add songs to a playlist and wait until MA (and Apple Music, for its playlists) has them.

    :param ctx: The request context.
    :param playlist_id: The library playlist id.
    :param uris: The songs' uris.
    """
    try:
        # the internal handler (as the smart playlist provider uses it) finishes the change
        # before this call returns, so the app sees it on its next request
        await ctx.server.mass.music.playlists._handle_add_playlist_tracks(playlist_id, uris)
    except MusicAssistantError as err:
        raise SubsonicError(ERR_GENERIC, f"Could not add the songs: {err}") from err


async def _remove(ctx: RequestContext, playlist_id: int, positions: tuple[int, ...]) -> None:
    """
    Remove songs from a playlist by their MA positions.

    :param ctx: The request context.
    :param playlist_id: The library playlist id.
    :param positions: The positions to remove.
    """
    try:
        await ctx.server.mass.music.playlists._handle_remove_playlist_tracks(playlist_id, positions)
    except MusicAssistantError as err:
        raise SubsonicError(ERR_GENERIC, f"Could not remove the songs: {err}") from err


def _track_uri(ctx: RequestContext, value: str) -> str:
    """
    Return the uri of a visible song: a library uri, or an Apple Music uri.

    :param ctx: The request context.
    :param value: The Subsonic song ID.
    """
    if (ref := apple_id(value, PREFIX_APPLE_TRACK)) is not None:
        return create_uri(MediaType.TRACK, APPLE_DOMAIN, ref[1])
    return create_uri(MediaType.TRACK, "library", str(track_from_id(ctx, value).id))


def _playlist_entry(
    ctx: RequestContext, row: Mapping[str, Any], count: int, duration: int
) -> dict[str, Any]:
    """
    Return the Subsonic playlist entry.

    :param ctx: The request context.
    :param row: The playlist's database row.
    :param count: The number of songs shown.
    :param duration: Their total duration in seconds.
    """
    assert ctx.user is not None
    playlist_id = make_id(PREFIX_PLAYLIST, row["item_id"])
    return {
        "id": playlist_id,
        "name": row["name"],
        "songCount": count,
        "duration": duration,
        "public": False,
        "owner": ctx.user.username,
        "created": iso_time(row["timestamp_added"]) or EMPTY_DATE,
        "changed": iso_time(row["timestamp_modified"]) or EMPTY_DATE,
        "coverArt": playlist_id,
        "readonly": not row["is_editable"],
    }
