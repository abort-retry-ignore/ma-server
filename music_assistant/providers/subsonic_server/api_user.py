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

from .api_library import song_entry, track_from_id
from .apple import PREFIX_APPLE_ALBUM, PREFIX_APPLE_ARTIST, PREFIX_APPLE_TRACK, apple_id
from .constants import ERR_GENERIC, ERR_NOT_AUTHORIZED, ERR_NOT_FOUND
from .context import NowPlaying, RequestContext, api_method
from .mapping import (
    EMPTY_DATE,
    PREFIX_ALBUM,
    PREFIX_ARTIST,
    PREFIX_TRACK,
    iso_time,
    make_id,
    parse_id,
)
from .responses import SubsonicError

if TYPE_CHECKING:

    from music_assistant_models.auth import User

    from .index import IndexedTrack

EXTENSIONS = (("formPost", [1]), ("songLyrics", [1]), ("indexBasedQueue", [1]))
APPLE_MEDIA_TYPES = {
    PREFIX_APPLE_ARTIST: MediaType.ARTIST,
    PREFIX_APPLE_ALBUM: MediaType.ALBUM,
    PREFIX_APPLE_TRACK: MediaType.TRACK,
}
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
        if (ref := apple_id(value)) is not None:
            await _star_apple(ctx, ref, favorite)
            continue
        prefix, item_id = parse_id(value, PREFIX_ARTIST, PREFIX_ALBUM, PREFIX_TRACK)
        media_type = MEDIA_TYPES[prefix]
        if item_id < 0:
            ctx.note = " (an [Unknown Album] can't be a favourite, ignored)"
            continue
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
    if apple_id(value) is None:
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
    for requested in ctx.get_all("id"):
        value = requested
        if (ref := apple_id(requested, PREFIX_APPLE_TRACK)) is not None:
            saved = ctx.server.catalog.library_track(ref[1])
            if saved is None:
                await _scrobble_apple(ctx, ref[1], submission)
                continue
            value = make_id(PREFIX_TRACK, saved.id)
        track = track_from_id(ctx, value)
        if not submission:
            ctx.server.now_playing[(ctx.user.username, ctx.client)] = NowPlaying(
                ctx.mapper.song(track), ctx.user.username, ctx.client, time.time()
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
    entries: list[dict[str, Any]] = []
    current_index = None
    for position, value in enumerate(saved.ids):
        try:
            entry = await song_entry(ctx, value)
        except SubsonicError:
            continue
        if position == saved.current_index:
            current_index = len(entries)
        entries.append(entry)
    queue: dict[str, Any] = {}
    if entries:
        queue["entry"] = entries
    if current_index is not None:
        if ctx.method == "getPlayQueueByIndex":
            queue["currentIndex"] = current_index
        else:
            queue["current"] = entries[current_index]["id"]
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


async def _star_apple(ctx: RequestContext, ref: tuple[str, str], favorite: bool) -> None:
    """
    Add an Apple Music song, album or artist to MA's favourites, or remove it.

    :param ctx: The request context.
    :param ref: The Apple prefix and id.
    :param favorite: True to add, False to remove.
    """
    media_type = APPLE_MEDIA_TYPES[ref[0]]
    music = ctx.server.mass.music
    provider = ctx.server.catalog.provider
    if provider is None:
        raise SubsonicError(ERR_NOT_FOUND, "Apple Music is not available")
    try:
        if favorite:
            await music.add_item_to_favorites(create_uri(media_type, provider.instance_id, ref[1]))
            return
        library_item = await music.get_library_item_by_prov_id(
            media_type, ref[1], provider.instance_id
        )
        if library_item is not None:
            await music.remove_item_from_favorites(media_type, library_item.item_id)
    except MusicAssistantError as err:
        raise SubsonicError(ERR_NOT_FOUND, f"Item not found: {ref[1]}") from err
    ctx.server.schedule_index_rebuild()


async def _scrobble_apple(ctx: RequestContext, item_id: str, submission: bool) -> None:
    """
    Record a play of (or "now playing" for) an Apple Music song that has no file yet.

    :param ctx: The request context.
    :param item_id: The Apple Music track id.
    :param submission: True for a finished play, False for "now playing".
    """
    assert ctx.user is not None
    track = await ctx.server.catalog.track(item_id)
    if not submission:
        if (entry := ctx.mapper.apple_song(track)) is not None:
            ctx.server.now_playing[(ctx.user.username, ctx.client)] = NowPlaying(
                entry, ctx.user.username, ctx.client, time.time()
            )
        ctx.note = f" (now playing '{track.name}' from Apple Music)"
        return
    await ctx.server.mass.music.mark_item_played(
        track,
        fully_played=True,
        seconds_played=track.duration,
        userid=ctx.user.user_id,
        user_initiated=True,
    )
    ctx.note = f" (played '{track.name}' from Apple Music)"
