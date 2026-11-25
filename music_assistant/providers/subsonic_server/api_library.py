"""Subsonic browsing, list and search methods, answered from the library index."""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import MusicAssistantError

from .constants import (
    DEFAULT_LIST_SIZE,
    DEFAULT_SEARCH_COUNT,
    ERR_GENERIC,
    ERR_MISSING_PARAM,
    ERR_NOT_FOUND,
    IGNORED_ARTICLES,
)
from .context import RequestContext, api_method
from .index import IndexedAlbum, IndexedArtist, IndexedTrack, index_letter
from .mapping import (
    PREFIX_ALBUM,
    PREFIX_ARTIST,
    PREFIX_TRACK,
    make_id,
    parse_id,
)
from .responses import SubsonicError

if TYPE_CHECKING:
    from collections.abc import Callable

IMAGE_SIZES = (("smallImageUrl", 300), ("mediumImageUrl", 600), ("largeImageUrl", 1200))


@api_method("getMusicFolders")
async def get_music_folders(ctx: RequestContext) -> dict[str, Any]:
    """Return the music folders."""
    folders = [{"id": f.id, "name": f.name} for f in ctx.server.index.folders]
    return {"musicFolders": {"musicFolder": folders} if folders else {}}


@api_method("getIndexes")
async def get_indexes(ctx: RequestContext) -> dict[str, Any]:
    """Return the artist index in the folder-style form."""
    return {"indexes": _artist_index(ctx, ctx.mapper.index_artist)}


@api_method("getArtists")
async def get_artists(ctx: RequestContext) -> dict[str, Any]:
    """Return the artist index (ID3)."""
    return {"artists": _artist_index(ctx, ctx.mapper.artist)}


@api_method("getArtist")
async def get_artist(ctx: RequestContext) -> dict[str, Any]:
    """Return an artist with its albums."""
    artist = _artist(ctx, ctx.require("id"))
    albums = ctx.server.index.artist_albums(artist)
    return {"artist": ctx.mapper.artist(artist, albums)}


@api_method("getAlbum")
async def get_album(ctx: RequestContext) -> dict[str, Any]:
    """Return an album with its songs."""
    album = _album(ctx, ctx.require("id"))
    return {"album": ctx.mapper.album(album, with_songs=True)}


@api_method("getSong")
async def get_song(ctx: RequestContext) -> dict[str, Any]:
    """Return a song."""
    return {"song": ctx.mapper.song(track_from_id(ctx, ctx.require("id")))}


@api_method("getMusicDirectory")
async def get_music_directory(ctx: RequestContext) -> dict[str, Any]:
    """Return an artist (its albums) or an album (its songs) as a directory."""
    value = ctx.require("id")
    prefix, _ = parse_id(value, PREFIX_ARTIST, PREFIX_ALBUM)
    index = ctx.server.index
    if prefix == PREFIX_ARTIST:
        artist = _artist(ctx, value)
        albums = index.artist_albums(artist)
        return {
            "directory": {
                "child": [ctx.mapper.album_child(a) for a in albums],
                "id": value,
                "name": artist.name,
                "albumCount": len(albums),
            }
        }
    album = _album(ctx, value)
    child = ctx.mapper.album_child(album)
    return {
        "directory": {
            "child": [ctx.mapper.song(t) for t in index.album_tracks(album)],
            "id": value,
            "name": child["name"],
            "parent": child["parent"],
            "coverArt": value,
            "songCount": child["songCount"],
        }
    }


@api_method("getGenres")
async def get_genres(ctx: RequestContext) -> dict[str, Any]:
    """Return the genres with their song and album counts."""
    genres = sorted(ctx.server.index.genres().items(), key=lambda g: g[0].casefold())
    entries = [{"value": name, "songCount": c[0], "albumCount": c[1]} for name, c in genres]
    return {"genres": {"genre": entries} if entries else {}}


@api_method("getArtistInfo", "getArtistInfo2")
async def get_artist_info(ctx: RequestContext) -> dict[str, Any]:
    """Return an artist's biography and images."""
    artist = _artist(ctx, ctx.require("id"))
    info: dict[str, Any] = {}
    try:
        item = await ctx.server.mass.music.artists.get_library_item(artist.id)
    except MusicAssistantError:
        item = None
    if item is not None and item.metadata and item.metadata.description:
        info["biography"] = item.metadata.description
    if item is not None and item.mbid:
        info["musicBrainzId"] = item.mbid
    for key, size in IMAGE_SIZES:
        info[key] = ctx.server.image_url(ctx.request, make_id(PREFIX_ARTIST, artist.id), size)
    key = "artistInfo2" if ctx.method == "getArtistInfo2" else "artistInfo"
    return {key: info}


@api_method("getAlbumInfo", "getAlbumInfo2")
async def get_album_info(ctx: RequestContext) -> dict[str, Any]:
    """Return an album's notes and images."""
    album = _album(ctx, ctx.require("id"))
    info: dict[str, Any] = {}
    try:
        item = await ctx.server.mass.music.albums.get_library_item(album.id)
    except MusicAssistantError:
        item = None
    if item is not None and item.metadata:
        notes = item.metadata.description or item.metadata.review
        if notes:
            info["notes"] = notes
    if item is not None and item.mbid:
        info["musicBrainzId"] = item.mbid
    for key, size in IMAGE_SIZES:
        info[key] = ctx.server.image_url(ctx.request, make_id(PREFIX_ALBUM, album.id), size)
    return {"albumInfo": info}


@api_method("getTopSongs")
async def get_top_songs(ctx: RequestContext) -> dict[str, Any]:
    """Return an artist's most played songs."""
    name = ctx.require("artist").casefold()
    count = ctx.get_size("count", 50)
    index = ctx.server.index
    artist_ids = {a.id for a in index.artists.values() if a.name.casefold() == name}
    tracks = [
        t
        for t in index.visible_tracks()
        if t.play_count > 0 and artist_ids.intersection(t.artist_ids)
    ]
    tracks.sort(key=lambda t: (-t.play_count, t.sort_name))
    return {"topSongs": _songs(ctx, tracks[:count])}


@api_method("getSimilarSongs", "getSimilarSongs2")
async def get_similar_songs(ctx: RequestContext) -> dict[str, Any]:
    """Return songs like an artist, album or song: same artist first, then the same genre."""
    value = ctx.require("id")
    prefix, _ = parse_id(value, PREFIX_ARTIST, PREFIX_ALBUM, PREFIX_TRACK)
    index = ctx.server.index
    if prefix == PREFIX_TRACK:
        seeds = [track_from_id(ctx, value)]
    elif prefix == PREFIX_ALBUM:
        seeds = index.album_tracks(_album(ctx, value))
    else:
        seeds = index.artist_tracks(_artist(ctx, value))
    tracks = index.similar_tracks([t.id for t in seeds], ctx.get_size("count", 50))
    key = "similarSongs2" if ctx.method == "getSimilarSongs2" else "similarSongs"
    return {key: _songs(ctx, tracks)}


@api_method("getAlbumList", "getAlbumList2")
async def get_album_list(ctx: RequestContext) -> dict[str, Any]:
    """Return a list of albums: newest, recent, frequent, random, by name and so on."""
    list_type = ctx.require("type")
    size = ctx.get_size("size", DEFAULT_LIST_SIZE)
    offset = ctx.get_offset()
    albums = _sorted_albums(ctx, list_type)
    page = albums[offset : offset + size]
    if ctx.method == "getAlbumList2":
        entries = [ctx.mapper.album(a) for a in page]
        return {"albumList2": {"album": entries} if entries else {}}
    entries = [ctx.mapper.album_child(a) for a in page]
    return {"albumList": {"album": entries} if entries else {}}


@api_method("getRandomSongs")
async def get_random_songs(ctx: RequestContext) -> dict[str, Any]:
    """Return random songs."""
    tracks = ctx.server.index.random_tracks(
        ctx.get_size("size", DEFAULT_LIST_SIZE),
        genre=ctx.get("genre"),
        from_year=ctx.get_int("fromYear"),
        to_year=ctx.get_int("toYear"),
        folder_id=ctx.folder_id(),
    )
    return {"randomSongs": _songs(ctx, tracks)}


@api_method("getSongsByGenre")
async def get_songs_by_genre(ctx: RequestContext) -> dict[str, Any]:
    """Return the songs of a genre."""
    genre = ctx.require("genre").casefold()
    count = ctx.get_size("count", DEFAULT_LIST_SIZE)
    offset = ctx.get_offset()
    tracks = [
        t
        for t in ctx.server.index.visible_tracks(ctx.folder_id())
        if any(g.casefold() == genre for g in t.genres)
    ]
    tracks.sort(key=lambda t: (t.sort_name, t.id))
    return {"songsByGenre": _songs(ctx, tracks[offset : offset + count])}


@api_method("getStarred", "getStarred2")
async def get_starred(ctx: RequestContext) -> dict[str, Any]:
    """Return the favourite artists, albums and songs."""
    index = ctx.server.index
    folder_id = ctx.folder_id()
    artists = [a for a in index.album_artists(folder_id) if a.favorite]
    albums = [a for a in index.visible_albums(folder_id) if a.favorite]
    tracks = [t for t in index.visible_tracks(folder_id) if t.favorite]
    result: dict[str, Any] = {}
    id3 = ctx.method == "getStarred2"
    if artists:
        result["artist"] = [
            ctx.mapper.artist(a) if id3 else ctx.mapper.index_artist(a)
            for a in sorted(artists, key=lambda a: a.sort_name)
        ]
    if albums:
        result["album"] = [
            ctx.mapper.album(a) if id3 else ctx.mapper.album_child(a)
            for a in sorted(albums, key=lambda a: a.sort_name)
        ]
    if tracks:
        result["song"] = [ctx.mapper.song(t) for t in sorted(tracks, key=lambda t: t.sort_name)]
    return {"starred2" if id3 else "starred": result}


@api_method("getNowPlaying")
async def get_now_playing(ctx: RequestContext) -> dict[str, Any]:
    """Return what clients reported as playing in the last hour."""
    now = time.time()
    entries = []
    for number, playing in enumerate(ctx.server.now_playing.values(), start=1):
        track = ctx.server.index.get_track(playing.track_id)
        minutes = int((now - playing.since) / 60)
        if track is None or minutes > 60:
            continue
        entry = ctx.mapper.song(track)
        entry.update(
            {
                "username": playing.username,
                "minutesAgo": minutes,
                "playerId": number,
                "playerName": playing.client,
            }
        )
        entries.append(entry)
    return {"nowPlaying": {"entry": entries} if entries else {}}


@api_method("search2", "search3")
async def search(ctx: RequestContext) -> dict[str, Any]:
    """Search artists, albums and songs; an empty query lists everything, page by page."""
    query = ctx.get("query", "") or ""
    folder_id = ctx.folder_id()
    index = ctx.server.index
    artist_count = ctx.get_size("artistCount", DEFAULT_SEARCH_COUNT)
    album_count = ctx.get_size("albumCount", DEFAULT_SEARCH_COUNT)
    song_count = ctx.get_size("songCount", DEFAULT_SEARCH_COUNT)
    artist_offset = ctx.get_offset("artistOffset")
    album_offset = ctx.get_offset("albumOffset")
    song_offset = ctx.get_offset("songOffset")
    artists = index.search_artists(query, folder_id)[artist_offset : artist_offset + artist_count]
    albums = index.search_albums(query, folder_id)[album_offset : album_offset + album_count]
    tracks = index.search_tracks(query, folder_id)
    if song_offset == 0 and query.strip('" '):
        tracks = await _with_heard_tracks_first(ctx, query, tracks)
    tracks = tracks[song_offset : song_offset + song_count]
    id3 = ctx.method == "search3"
    result: dict[str, Any] = {}
    if artists:
        result["artist"] = [
            ctx.mapper.artist(a) if id3 else ctx.mapper.index_artist(a) for a in artists
        ]
    if albums:
        result["album"] = [
            ctx.mapper.album(a) if id3 else ctx.mapper.album_child(a) for a in albums
        ]
    if tracks:
        result["song"] = [ctx.mapper.song(t) for t in tracks]
    return {"searchResult3" if id3 else "searchResult2": result}


def track_from_id(ctx: RequestContext, value: str) -> IndexedTrack:
    """
    Return the visible track for a song ID.

    :param ctx: The request context.
    :param value: The Subsonic song ID.
    :raises SubsonicError: Not found (70).
    """
    _, track_id = parse_id(value, PREFIX_TRACK)
    track = ctx.server.index.get_track(track_id)
    if track is None:
        raise SubsonicError(ERR_NOT_FOUND, "Song not found")
    return track


def _artist(ctx: RequestContext, value: str) -> IndexedArtist:
    """
    Return the visible artist for an artist ID.

    :param ctx: The request context.
    :param value: The Subsonic artist ID.
    :raises SubsonicError: Not found (70).
    """
    _, artist_id = parse_id(value, PREFIX_ARTIST)
    artist = ctx.server.index.get_artist(artist_id)
    if artist is None:
        raise SubsonicError(ERR_NOT_FOUND, "Artist not found")
    return artist


def _album(ctx: RequestContext, value: str) -> IndexedAlbum:
    """
    Return the visible album for an album ID.

    :param ctx: The request context.
    :param value: The Subsonic album ID.
    :raises SubsonicError: Not found (70).
    """
    _, album_id = parse_id(value, PREFIX_ALBUM)
    album = ctx.server.index.get_album(album_id)
    if album is None:
        raise SubsonicError(ERR_NOT_FOUND, "Album not found")
    return album


def _songs(ctx: RequestContext, tracks: list[IndexedTrack]) -> dict[str, Any]:
    """
    Return songs wrapped the Subsonic way: an empty object when there are none.

    :param ctx: The request context.
    :param tracks: The tracks.
    """
    return {"song": [ctx.mapper.song(t) for t in tracks]} if tracks else {}


def _artist_index(
    ctx: RequestContext, to_entry: Callable[[IndexedArtist], dict[str, Any]]
) -> dict[str, Any]:
    """
    Return the album artists grouped by first letter, with the index metadata.

    :param ctx: The request context.
    :param to_entry: Converts an artist into its entry.
    """
    index = ctx.server.index
    result: dict[str, Any] = {}
    since = ctx.get_int("ifModifiedSince")
    if since is None or index.last_modified > since:
        groups: dict[str, list[IndexedArtist]] = {}
        for artist in index.album_artists(ctx.folder_id()):
            groups.setdefault(index_letter(artist.sort_name), []).append(artist)
        result["index"] = [
            {"name": letter, "artist": [to_entry(a) for a in sorted(members, key=_sort_key)]}
            for letter, members in sorted(groups.items())
        ]
    result["lastModified"] = index.last_modified
    result["ignoredArticles"] = IGNORED_ARTICLES
    return result


def _sort_key(artist: IndexedArtist) -> tuple[str, int]:
    """
    Return the sort key of an artist.

    :param artist: The artist.
    """
    return artist.sort_name, artist.id


def _sorted_albums(ctx: RequestContext, list_type: str) -> list[IndexedAlbum]:
    """
    Return the visible albums for an album list type, in list order.

    :param ctx: The request context.
    :param list_type: The Subsonic list type.
    :raises SubsonicError: For an unknown type or missing type parameters.
    """
    index = ctx.server.index
    albums = index.visible_albums(ctx.folder_id())
    if list_type == "random":
        return random.sample(albums, len(albums))
    if list_type == "newest":
        return sorted(albums, key=lambda a: (-a.timestamp_added, a.sort_name))
    if list_type == "recent":
        played = [a for a in albums if a.last_played]
        return sorted(played, key=lambda a: -a.last_played)
    if list_type == "frequent":
        played = [a for a in albums if a.play_count]
        return sorted(played, key=lambda a: (-a.play_count, a.sort_name))
    if list_type == "highest":
        rated = [a for a in albums if ctx.ratings.get(make_id(PREFIX_ALBUM, a.id))]
        return sorted(rated, key=lambda a: -ctx.ratings[make_id(PREFIX_ALBUM, a.id)])
    if list_type == "alphabeticalByName":
        return sorted(albums, key=lambda a: (a.sort_name, a.id))
    if list_type == "alphabeticalByArtist":
        return sorted(albums, key=lambda a: (_album_artist_sort(ctx, a), a.sort_name, a.id))
    if list_type == "starred":
        return sorted((a for a in albums if a.favorite), key=lambda a: a.sort_name)
    if list_type == "byYear":
        from_year = ctx.get_int("fromYear")
        to_year = ctx.get_int("toYear")
        if from_year is None or to_year is None:
            raise SubsonicError(ERR_MISSING_PARAM, "Required parameter is missing: fromYear")
        low, high = sorted((from_year, to_year))
        dated = [a for a in albums if a.year is not None and low <= a.year <= high]
        return sorted(dated, key=lambda a: (a.year or 0, a.sort_name), reverse=from_year > to_year)
    if list_type == "byGenre":
        genre = ctx.require("genre").casefold()
        return sorted(
            (a for a in albums if any(g.casefold() == genre for g in a.genres)),
            key=lambda a: a.sort_name,
        )
    raise SubsonicError(ERR_GENERIC, f"Unknown list type: {list_type}")


def _album_artist_sort(ctx: RequestContext, album: IndexedAlbum) -> str:
    """
    Return the sort name of an album's first artist.

    :param ctx: The request context.
    :param album: The album.
    """
    for artist_id in album.artist_ids:
        if (artist := ctx.server.index.artists.get(artist_id)) is not None:
            return artist.sort_name
    return ""


async def _with_heard_tracks_first(
    ctx: RequestContext, query: str, tracks: list[IndexedTrack]
) -> list[IndexedTrack]:
    """
    Return the tracks with close matches the household listened to (play memory) on top.

    :param ctx: The request context.
    :param query: The search query.
    :param tracks: The tracks found by the search.
    """
    index = ctx.server.index
    promoted: list[IndexedTrack] = []
    for uri in await ctx.server.mass.music.play_memory.heard_uris_matching(query):
        track_id = index.track_by_uri.get(uri)
        track = index.get_track(track_id) if track_id is not None else None
        if track is not None and track not in promoted:
            promoted.append(track)
    if not promoted:
        return tracks
    return promoted + [t for t in tracks if t not in promoted]
