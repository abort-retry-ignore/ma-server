"""
Subsonic system, user, annotation, play queue and playlist methods.

Also answers the calls for features MA's server doesn't offer (podcasts, radio, shares, ...)
with empty lists, the way Navidrome does when they are empty, so apps don't show errors.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import UserRole
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.helpers import create_uri

from .api_library import track_from_id
from .constants import ERR_GENERIC, ERR_NOT_AUTHORIZED, ERR_NOT_FOUND
from .context import NowPlaying, RequestContext, api_method
from .mapping import (
    EMPTY_DATE,
    PREFIX_ALBUM,
    PREFIX_ARTIST,
    PREFIX_PLAYLIST,
    PREFIX_TRACK,
    iso_time,
    make_id,
    parse_id,
)
from .responses import SubsonicError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.auth import User

    from .index import IndexedTrack

EXTENSIONS = (("formPost", [1]), ("songLyrics", [1]), ("indexBasedQueue", [1]))
MEDIA_TYPES = {
    PREFIX_ARTIST: MediaType.ARTIST,
    PREFIX_ALBUM: MediaType.ALBUM,
    PREFIX_TRACK: MediaType.TRACK,
}
EMPTY_LISTS = {
    "getBookmarks": "bookmarks",
    "getShares": "shares",
    "getPodcasts": "podcasts",
    "getNewestPodcasts": "newestPodcasts",
    "getInternetRadioStations": "internetRadioStations",
    "getChatMessages": "chatMessages",
    "getVideos": "videos",
}


@api_method("ping")
async def ping(ctx: RequestContext) -> dict[str, Any]:  # noqa: ARG001
    """Answer a connection test."""
    return {}


@api_method("getLicense")
async def get_license(ctx: RequestContext) -> dict[str, Any]:  # noqa: ARG001
    """Return the (always valid) license."""
    return {"license": {"valid": True}}


@api_method("getOpenSubsonicExtensions", needs_auth=False)
async def get_open_subsonic_extensions(ctx: RequestContext) -> dict[str, Any]:  # noqa: ARG001
    """Return the OpenSubsonic extensions this server supports."""
    return {
        "openSubsonicExtensions": [
            {"name": name, "versions": versions} for name, versions in EXTENSIONS
        ]
    }


@api_method("getUser")
async def get_user(ctx: RequestContext) -> dict[str, Any]:
    """Return a user's roles: the own user, or any user for an admin."""
    assert ctx.user is not None
    username = ctx.get("username") or ctx.user.username
    if username.casefold() == ctx.user.username.casefold():
        return {"user": _user_entry(ctx, ctx.user)}
    if ctx.user.role != UserRole.ADMIN:
        raise SubsonicError(ERR_NOT_AUTHORIZED, "Not allowed to see other users")
    found = await ctx.server.provider.lookup_user(username)
    if found is None:
        raise SubsonicError(ERR_NOT_FOUND, "User not found")
    return {"user": _user_entry(ctx, found[0])}


@api_method("getUsers")
async def get_users(ctx: RequestContext) -> dict[str, Any]:
    """Return every user with Subsonic access (admin only)."""
    assert ctx.user is not None
    if ctx.user.role != UserRole.ADMIN:
        raise SubsonicError(ERR_NOT_AUTHORIZED, "Not allowed to list users")
    users = [_user_entry(ctx, u) for u in await ctx.server.provider.subsonic_users()]
    return {"users": {"user": users} if users else {}}


@api_method("getScanStatus")
async def get_scan_status(ctx: RequestContext) -> dict[str, Any]:
    """Return whether the music folders are being scanned."""
    return {"scanStatus": _scan_status(ctx)}


@api_method("startScan")
async def start_scan(ctx: RequestContext) -> dict[str, Any]:
    """Start a scan of the music folders."""
    instances = [f.instance_id for f in ctx.server.index.folders]
    if instances:
        await ctx.server.mass.music.start_sync(providers=instances)
    status = _scan_status(ctx)
    status["scanning"] = bool(instances)
    return {"scanStatus": status}


@api_method("star", "unstar")
async def star(ctx: RequestContext) -> dict[str, Any]:
    """Add or remove favourites (MA favourites, shared by everyone)."""
    favorite = ctx.method == "star"
    music = ctx.server.mass.music
    index = ctx.server.index
    values = ctx.get_all("id") + ctx.get_all("albumId") + ctx.get_all("artistId")
    for value in values:
        prefix, item_id = parse_id(value, PREFIX_ARTIST, PREFIX_ALBUM, PREFIX_TRACK)
        media_type = MEDIA_TYPES[prefix]
        try:
            if favorite:
                await music.add_item_to_favorites(create_uri(media_type, "library", str(item_id)))
            else:
                await music.remove_item_from_favorites(media_type, item_id)
        except MusicAssistantError as err:
            raise SubsonicError(ERR_NOT_FOUND, f"Item not found: {value}") from err
        item: Any
        if prefix == PREFIX_ARTIST:
            item = index.artists.get(item_id)
        elif prefix == PREFIX_ALBUM:
            item = index.albums.get(item_id)
        else:
            item = index.tracks.get(item_id)
        if item is not None:
            item.favorite = favorite
            item.timestamp_modified = int(time.time())
    return {}


@api_method("setRating")
async def set_rating(ctx: RequestContext) -> dict[str, Any]:
    """Rate an item from 1 to 5 stars, or remove the rating with 0."""
    value = ctx.require("id")
    parse_id(value, PREFIX_ARTIST, PREFIX_ALBUM, PREFIX_TRACK)
    rating = ctx.get_int("rating")
    if rating is None or not 0 <= rating <= 5:
        raise SubsonicError(ERR_GENERIC, "Rating must be between 0 and 5")
    await ctx.server.storage.set_rating(ctx.user_id, value, rating)
    return {}


@api_method("scrobble")
async def scrobble(ctx: RequestContext) -> dict[str, Any]:
    """Record finished plays in MA's play history, or remember what is playing now."""
    assert ctx.user is not None
    submission = ctx.get_bool("submission", True)
    mass = ctx.server.mass
    for value in ctx.get_all("id"):
        track = track_from_id(ctx, value)
        if not submission:
            ctx.server.now_playing[(ctx.user.username, ctx.client)] = NowPlaying(
                track.id, ctx.user.username, ctx.client, time.time()
            )
            ctx.note = f" (now playing '{track.name}')"
            continue
        item = await mass.music.tracks.get_library_item(track.id)
        await mass.music.mark_item_played(
            item,
            fully_played=True,
            seconds_played=track.duration,
            userid=ctx.user.user_id,
            user_initiated=True,
        )
        _count_play(ctx, track)
        ctx.note = f" (played '{track.name}')"
    return {}


@api_method("getPlayQueue", "getPlayQueueByIndex")
async def get_play_queue(ctx: RequestContext) -> dict[str, Any]:
    """Return the play queue the user's apps saved."""
    assert ctx.user is not None
    saved = await ctx.server.storage.get_play_queue(ctx.user_id)
    tracks: list[IndexedTrack] = []
    current_index = None
    for position, value in enumerate(saved.ids):
        try:
            track = track_from_id(ctx, value)
        except SubsonicError:
            continue
        if position == saved.current_index:
            current_index = len(tracks)
        tracks.append(track)
    queue: dict[str, Any] = {}
    if tracks:
        queue["entry"] = [ctx.mapper.song(t) for t in tracks]
    if current_index is not None:
        if ctx.method == "getPlayQueueByIndex":
            queue["currentIndex"] = current_index
        else:
            queue["current"] = make_id(PREFIX_TRACK, tracks[current_index].id)
        queue["position"] = saved.position
    queue["username"] = ctx.user.username
    queue["changed"] = iso_time(saved.changed) or EMPTY_DATE
    queue["changedBy"] = saved.changed_by
    return {"playQueueByIndex" if ctx.method == "getPlayQueueByIndex" else "playQueue": queue}


@api_method("savePlayQueue", "savePlayQueueByIndex")
async def save_play_queue(ctx: RequestContext) -> dict[str, Any]:
    """Save the user's play queue so another app or device can continue it."""
    ids = ctx.get_all("id")
    if ctx.method == "savePlayQueueByIndex":
        current_index = ctx.get_int("currentIndex")
        if current_index is not None and not 0 <= current_index < len(ids):
            current_index = None
    else:
        current = ctx.get("current")
        current_index = ids.index(current) if current in ids else None
    position = ctx.get_int("position", 0) or 0
    await ctx.server.storage.save_play_queue(ctx.user_id, ids, current_index, position, ctx.client)
    return {}


@api_method("getPlaylists")
async def get_playlists(ctx: RequestContext) -> dict[str, Any]:
    """Return MA's own playlists."""
    entries = []
    for row in await _builtin_playlists(ctx):
        tracks = await _playlist_tracks(ctx, row["item_id"])
        entries.append(_playlist_entry(ctx, row, [t for _, t in tracks]))
    return {"playlists": {"playlist": entries} if entries else {}}


@api_method("getPlaylist")
async def get_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Return one of MA's own playlists with its songs."""
    row = await _playlist_row(ctx, ctx.require("id"))
    tracks = [t for _, t in await _playlist_tracks(ctx, row["item_id"])]
    entry = _playlist_entry(ctx, row, tracks)
    entry["entry"] = [ctx.mapper.song(t) for t in tracks]
    return {"playlist": entry}


@api_method("createPlaylist")
async def create_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Create a playlist in MA (or replace the songs of an existing one)."""
    playlists = ctx.server.mass.music.playlists
    song_uris = [_track_uri(ctx, v) for v in ctx.get_all("songId")]
    if (existing := ctx.get("playlistId")) is not None:
        row = await _playlist_row(ctx, existing)
        playlist_id = int(row["item_id"])
        positions = tuple(p for p, _ in await _playlist_tracks(ctx, playlist_id, everything=True))
        if positions:
            await playlists._handle_remove_playlist_tracks(playlist_id, positions)
    else:
        playlist = await playlists.create_playlist(ctx.require("name"))
        playlist_id = int(playlist.item_id)
    if song_uris:
        # the internal handler (as the smart playlist provider uses it) finishes the change
        # before this call returns, so the app sees it on its next request
        await playlists._handle_add_playlist_tracks(playlist_id, song_uris)
    return await _playlist_by_id(ctx, playlist_id)


@api_method("updatePlaylist")
async def update_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Add songs to or remove songs from one of MA's own playlists."""
    row = await _playlist_row(ctx, ctx.require("playlistId"))
    playlist_id = int(row["item_id"])
    playlists = ctx.server.mass.music.playlists
    remove = {int(v) for v in ctx.get_all("songIndexToRemove") if v.isdigit()}
    if remove:
        tracks = await _playlist_tracks(ctx, playlist_id)
        positions = tuple(p for i, (p, _) in enumerate(tracks) if i in remove)
        if positions:
            await playlists._handle_remove_playlist_tracks(playlist_id, positions)
    song_uris = [_track_uri(ctx, v) for v in ctx.get_all("songIdToAdd")]
    if song_uris:
        await playlists._handle_add_playlist_tracks(playlist_id, song_uris)
    if ctx.get("name") or ctx.get("comment"):
        ctx.note = " (renaming and comments are not supported, ignored)"
    return {}


@api_method("deletePlaylist")
async def delete_playlist(ctx: RequestContext) -> dict[str, Any]:
    """Delete one of MA's own playlists."""
    row = await _playlist_row(ctx, ctx.require("id"))
    await ctx.server.mass.music.remove_item_from_library(MediaType.PLAYLIST, int(row["item_id"]))
    return {}


@api_method(*EMPTY_LISTS)
async def empty_list(ctx: RequestContext) -> dict[str, Any]:
    """Answer calls for features this server doesn't have with an empty list."""
    return {EMPTY_LISTS[ctx.method]: {}}


def _user_entry(ctx: RequestContext, user: User) -> dict[str, Any]:
    """
    Return the Subsonic user entry with its roles.

    :param ctx: The request context.
    :param user: The MA user.
    """
    return {
        "username": user.username,
        "scrobblingEnabled": True,
        "adminRole": user.role == UserRole.ADMIN,
        "settingsRole": False,
        "downloadRole": True,
        "uploadRole": False,
        "playlistRole": True,
        "coverArtRole": True,
        "commentRole": False,
        "podcastRole": False,
        "streamRole": True,
        "jukeboxRole": False,
        "shareRole": False,
        "videoConversionRole": False,
        "folder": [f.id for f in ctx.server.index.folders],
    }


def _scan_status(ctx: RequestContext) -> dict[str, Any]:
    """
    Return the scan status entry.

    :param ctx: The request context.
    """
    index = ctx.server.index
    tracks = index.visible_tracks()
    return {
        "scanning": bool(ctx.server.mass.music.active_sync_tasks),
        "count": len(tracks),
        "folderCount": len({os.path.dirname(t.rel_path) for t in tracks}),
        "lastScan": iso_time(index.built_at),
        "scanType": "full",
        "elapsedTime": 0,
    }


def _count_play(ctx: RequestContext, track: IndexedTrack) -> None:
    """
    Count a play in the index right away, before the next rebuild picks it up.

    :param ctx: The request context.
    :param track: The track that was played.
    """
    now = int(time.time())
    track.play_count += 1
    track.last_played = now
    if track.album_id is not None and (album := ctx.server.index.albums.get(track.album_id)):
        album.play_count += 1
        album.last_played = now


def _track_uri(ctx: RequestContext, value: str) -> str:
    """
    Return the library uri of a visible song.

    :param ctx: The request context.
    :param value: The Subsonic song ID.
    """
    return create_uri(MediaType.TRACK, "library", str(track_from_id(ctx, value).id))


async def _builtin_playlists(ctx: RequestContext) -> list[Mapping[str, Any]]:
    """
    Return MA's own playlists from the library database.

    :param ctx: The request context.
    """
    return await ctx.server.mass.music.database.get_rows_from_query(
        "SELECT p.item_id, p.name, p.is_editable, p.timestamp_added, p.timestamp_modified "
        "FROM playlists p JOIN provider_mappings pm "
        "ON pm.item_id = p.item_id AND pm.media_type = 'playlist' "
        "WHERE pm.provider_domain = 'builtin' ORDER BY p.sort_name",
        limit=0,
    )


async def _playlist_row(ctx: RequestContext, value: str) -> Mapping[str, Any]:
    """
    Return the database row of one of MA's own playlists.

    :param ctx: The request context.
    :param value: The Subsonic playlist ID.
    :raises SubsonicError: Not found (70).
    """
    _, playlist_id = parse_id(value, PREFIX_PLAYLIST)
    for row in await _builtin_playlists(ctx):
        if int(row["item_id"]) == playlist_id:
            return row
    raise SubsonicError(ERR_NOT_FOUND, "Playlist not found")


async def _playlist_by_id(ctx: RequestContext, playlist_id: int) -> dict[str, Any]:
    """
    Return the playlist response for a playlist that was just created or changed.

    :param ctx: The request context.
    :param playlist_id: The library playlist id.
    """
    row = await _playlist_row(ctx, make_id(PREFIX_PLAYLIST, playlist_id))
    tracks = [t for _, t in await _playlist_tracks(ctx, playlist_id)]
    entry = _playlist_entry(ctx, row, tracks)
    entry["entry"] = [ctx.mapper.song(t) for t in tracks]
    return {"playlist": entry}


async def _playlist_tracks(
    ctx: RequestContext, playlist_id: int | str, everything: bool = False
) -> list[tuple[int, IndexedTrack]]:
    """
    Return (MA position, track) for the playlist songs a client can see.

    :param ctx: The request context.
    :param playlist_id: The library playlist id.
    :param everything: Also return the positions of songs a client can't see (track None).
    """
    index = ctx.server.index
    result: list[tuple[int, Any]] = []
    # MA's own playlists are local, so a fresh read is cheap and shows edits at once
    async for item in ctx.server.mass.music.playlists.tracks(
        str(playlist_id), "library", force_refresh=True
    ):
        track_id = index.track_by_uri.get(item.uri or "")
        if track_id is None:
            for mapping in getattr(item, "provider_mappings", ()):
                uri = create_uri(MediaType.TRACK, mapping.provider_instance, mapping.item_id)
                if (track_id := index.track_by_uri.get(uri)) is not None:
                    break
        track = index.get_track(track_id) if track_id is not None else None
        position = int(getattr(item, "position", 0) or len(result) + 1)
        if track is not None or everything:
            result.append((position, track))
    return result


def _playlist_entry(
    ctx: RequestContext, row: Mapping[str, Any], tracks: list[IndexedTrack]
) -> dict[str, Any]:
    """
    Return the Subsonic playlist entry.

    :param ctx: The request context.
    :param row: The playlist's database row.
    :param tracks: The playlist's visible tracks.
    """
    assert ctx.user is not None
    playlist_id = make_id(PREFIX_PLAYLIST, row["item_id"])
    return {
        "id": playlist_id,
        "name": row["name"],
        "songCount": len(tracks),
        "duration": sum(t.duration for t in tracks),
        "public": False,
        "owner": ctx.user.username,
        "created": iso_time(row["timestamp_added"]) or EMPTY_DATE,
        "changed": iso_time(row["timestamp_modified"]) or EMPTY_DATE,
        "coverArt": playlist_id,
        "readonly": not row["is_editable"],
    }
