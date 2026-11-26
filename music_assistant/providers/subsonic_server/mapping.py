"""Subsonic IDs and the conversion of indexed library items into Subsonic entries."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.media_items import Album, ItemMapping

from .apple import (
    PREFIX_APPLE_ALBUM,
    PREFIX_APPLE_ARTIST,
    PREFIX_APPLE_TRACK,
    is_name_ref,
    is_youtube_id,
    name_ref,
)
from .constants import APPLE_BIT_RATE, CONTENT_TYPES, ERR_NOT_FOUND, YOUTUBE_BIT_RATE
from .responses import SubsonicError

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.media_items import Artist, Track

    from .apple import AppleCatalog
    from .index import IndexedAlbum, IndexedArtist, IndexedTrack, LibraryIndex

PREFIX_ARTIST = "ar"
PREFIX_ALBUM = "al"
PREFIX_TRACK = "tr"
PREFIX_PLAYLIST = "pl"
# ReplayGain 2 reference loudness
REPLAYGAIN_REFERENCE_LUFS = -18.0
EMPTY_DATE = "0001-01-01T00:00:00Z"


def make_id(prefix: str, item_id: int | str) -> str:
    """
    Return a Subsonic ID for a library item.

    :param prefix: The item type prefix ("ar", "al", "tr" or "pl").
    :param item_id: The library id.
    """
    return f"{prefix}-{item_id}"


def parse_id(value: str | None, *allowed: str) -> tuple[str, int]:
    """
    Return the type prefix and library id of a Subsonic ID.

    :param value: The Subsonic ID.
    :param allowed: The prefixes accepted here (all when empty).
    :raises SubsonicError: Not found (70) for a malformed ID or a type not accepted here.
    """
    prefix, _, number = (value or "").partition("-")
    # "[Unknown Album]" albums have negative ids ("al--204")
    negative = prefix == PREFIX_ALBUM and number.startswith("-")
    digits = number[1:] if negative else number
    if not digits.isdigit() or (allowed and prefix not in allowed):
        raise SubsonicError(ERR_NOT_FOUND, "Item not found")
    if prefix not in (PREFIX_ARTIST, PREFIX_ALBUM, PREFIX_TRACK, PREFIX_PLAYLIST):
        raise SubsonicError(ERR_NOT_FOUND, "Item not found")
    return prefix, -int(digits) if negative else int(digits)


def iso_time(timestamp: float | None) -> str | None:
    """
    Return a UTC timestamp formatted like Navidrome, or None when it is not set.

    :param timestamp: Seconds since the epoch.
    """
    if not timestamp:
        return None
    moment = datetime.fromtimestamp(timestamp, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def display_name(name: str, version: str) -> str:
    """
    Return a title with its version, the way it appears in file tags.

    :param name: The name.
    :param version: The version, such as "2017 Remaster".
    """
    if version and version.casefold() not in name.casefold():
        return f"{name} ({version})"
    return name


class Mapper:
    """Builds Subsonic entries from the library index."""

    def __init__(
        self,
        index: LibraryIndex,
        image_url: Callable[[str], str],
        ratings: dict[str, int],
        catalog: AppleCatalog | None = None,
    ) -> None:
        """
        Initialize the mapper for one request.

        :param index: The library index.
        :param image_url: Returns a signed, login-free image URL for a cover art ID.
        :param ratings: The user's ratings by Subsonic ID.
        :param catalog: The Apple Music catalogue, for Apple search results.
        """
        self.index = index
        self.image_url = image_url
        self.ratings = ratings
        self.catalog = catalog

    def song(self, track: IndexedTrack) -> dict[str, Any]:
        """
        Return the Subsonic song ("child") entry of a track.

        :param track: The track.
        """
        album = self.index.albums.get(track.album_id) if track.album_id is not None else None
        artists = self._artists(track.artist_ids)
        album_artists = self._artists(album.artist_ids) if album else artists
        song_id = make_id(PREFIX_TRACK, track.id)
        album_id = make_id(PREFIX_ALBUM, album.id) if album else None
        entry: dict[str, Any] = {
            "id": song_id,
            "parent": album_id,
            "isDir": False,
            "title": display_name(track.name, track.version),
            "album": display_name(album.name, album.version) if album else None,
            "artist": _join(artists),
            "track": track.track_number or None,
            "year": track.year,
            "genre": track.genres[0] if track.genres else None,
            "coverArt": album_id or song_id,
            "size": track.size,
            "contentType": track.content_type,
            "suffix": track.suffix,
            "duration": track.duration,
            "bitRate": track.bit_rate,
            "path": track.rel_path,
            "playCount": track.play_count or None,
            "discNumber": (track.disc_number or 1) if album else None,
            "created": iso_time(track.timestamp_added),
            "albumId": album_id,
            "artistId": make_id(PREFIX_ARTIST, artists[0]["id"]) if artists else None,
            "type": "music",
            "starred": iso_time(track.timestamp_modified) if track.favorite else None,
            "played": iso_time(track.last_played),
            "userRating": self.ratings.get(song_id),
            "bpm": 0,
            "comment": "",
            "sortName": track.sort_name,
            "mediaType": "song",
            "musicBrainzId": "",
            "isrc": [],
            "genres": [{"name": g} for g in track.genres],
            "replayGain": _replay_gain(track),
            "channelCount": track.channels,
            "samplingRate": track.sample_rate,
            "bitDepth": track.bit_depth,
            "moods": [],
            "artists": _refs(artists),
            "displayArtist": _join(artists),
            "albumArtists": _refs(album_artists),
            "displayAlbumArtist": _join(album_artists),
            "contributors": [],
            "displayComposer": "",
            "explicitStatus": "",
            "groupings": [],
            "works": [],
            "movements": [],
        }
        return entry

    def album(self, album: IndexedAlbum, with_songs: bool = False) -> dict[str, Any]:
        """
        Return the Subsonic albumID3 entry of an album.

        :param album: The album.
        :param with_songs: Include the album's songs.
        """
        tracks = self.index.album_tracks(album)
        artists = self._artists(album.artist_ids)
        album_id = make_id(PREFIX_ALBUM, album.id)
        entry: dict[str, Any] = {
            "id": album_id,
            "name": display_name(album.name, album.version),
            "artist": _join(artists),
            "artistId": make_id(PREFIX_ARTIST, artists[0]["id"]) if artists else None,
            "coverArt": album_id,
            "songCount": len(tracks),
            "duration": sum(t.duration for t in tracks),
            "playCount": album.play_count or None,
            "created": iso_time(album.timestamp_added),
            "starred": iso_time(album.timestamp_modified) if album.favorite else None,
            "year": album.year,
            "genre": album.genres[0] if album.genres else None,
            "played": iso_time(album.last_played),
            "userRating": self.ratings.get(album_id, 0),
            "genres": [{"name": g} for g in album.genres],
            "musicBrainzId": "",
            "isCompilation": album.album_type == "compilation",
            "sortName": album.sort_name,
            "discTitles": [],
            "originalReleaseDate": {},
            "releaseDate": {"year": album.year} if album.year else {},
            "releaseTypes": [album.album_type] if album.album_type not in ("", "unknown") else [],
            "recordLabels": [],
            "moods": [],
            "artists": _refs(artists),
            "displayArtist": _join(artists),
            "explicitStatus": "",
            "version": album.version,
        }
        if with_songs:
            entry["song"] = [self.song(t) for t in tracks]
        return entry

    def album_child(self, album: IndexedAlbum) -> dict[str, Any]:
        """
        Return an album as a folder-style "child" entry (getAlbumList, search2, directories).

        :param album: The album.
        """
        tracks = self.index.album_tracks(album)
        artists = self._artists(album.artist_ids)
        album_id = make_id(PREFIX_ALBUM, album.id)
        artist_id = make_id(PREFIX_ARTIST, artists[0]["id"]) if artists else None
        name = display_name(album.name, album.version)
        return {
            "id": album_id,
            "parent": artist_id,
            "isDir": True,
            "title": name,
            "name": name,
            "album": name,
            "artist": _join(artists),
            "year": album.year,
            "genre": album.genres[0] if album.genres else None,
            "coverArt": album_id,
            "duration": sum(t.duration for t in tracks),
            "playCount": album.play_count or None,
            "created": iso_time(album.timestamp_added),
            "starred": iso_time(album.timestamp_modified) if album.favorite else None,
            "artistId": artist_id,
            "songCount": len(tracks),
            "played": iso_time(album.last_played),
            "userRating": self.ratings.get(album_id),
            "bpm": 0,
            "comment": "",
            "sortName": album.sort_name,
            "mediaType": "album",
            "musicBrainzId": "",
            "isrc": [],
            "genres": [{"name": g} for g in album.genres],
            "replayGain": {},
            "channelCount": 0,
            "samplingRate": 0,
            "bitDepth": 0,
            "moods": [],
            "artists": _refs(artists),
            "displayArtist": _join(artists),
            "albumArtists": _refs(artists),
            "displayAlbumArtist": _join(artists),
            "contributors": [],
            "displayComposer": "",
            "explicitStatus": "",
            "groupings": [],
            "works": [],
            "movements": [],
        }

    def artist(
        self, artist: IndexedArtist, albums: list[IndexedAlbum] | None = None
    ) -> dict[str, Any]:
        """
        Return the Subsonic artistID3 entry of an artist.

        :param artist: The artist.
        :param albums: The artist's albums to include (none when omitted).
        """
        artist_id = make_id(PREFIX_ARTIST, artist.id)
        own_albums = self.index.artist_albums(artist)
        roles = []
        if artist.album_ids:
            roles.append("albumartist")
        if artist.track_album_ids:
            roles.append("artist")
        entry: dict[str, Any] = {
            "id": artist_id,
            "name": artist.name,
            "coverArt": artist_id,
            "albumCount": len(own_albums),
            "starred": iso_time(artist.timestamp_modified) if artist.favorite else None,
            "artistImageUrl": self.image_url(artist_id),
            "musicBrainzId": "",
            "sortName": artist.sort_name,
            "roles": roles,
        }
        if albums is not None:
            entry["album"] = [self.album(a) for a in albums]
        return entry

    def index_artist(self, artist: IndexedArtist) -> dict[str, Any]:
        """
        Return an artist entry of the folder-style index (getIndexes, search2, getStarred).

        :param artist: The artist.
        """
        artist_id = make_id(PREFIX_ARTIST, artist.id)
        return {
            "id": artist_id,
            "name": artist.name,
            "coverArt": artist_id,
            "starred": iso_time(artist.timestamp_modified) if artist.favorite else None,
            "artistImageUrl": self.image_url(artist_id),
        }

    def apple_song(self, track: Track) -> dict[str, Any] | None:
        """
        Return the song entry of an Apple or YouTube Music track, or its file's library song.

        Returns None for a track that is on neither service (or whose service is switched off).

        :param track: The track, from search, an Apple album or the library.
        """
        assert self.catalog is not None
        item_id = self.catalog.apple_track_id(track)
        if item_id is None or self.catalog.is_blocked(item_id):
            return None
        if (saved := self.catalog.library_track(item_id)) is not None:
            return self.song(saved)
        album = track.album if isinstance(track.album, Album) else None
        artists = [r for r in (self._apple_ref(a) for a in track.artists) if r is not None]
        first_artist = artists[0][1] if artists else None
        album_ref = self._apple_ref(track.album, first_artist) if track.album is not None else None
        song_id = make_id(PREFIX_APPLE_TRACK, item_id)
        album_id = make_id(PREFIX_APPLE_ALBUM, album_ref[0]) if album_ref else None
        # a song listed without its album link (playlists) shows its own picture
        cover_id = album_id if album_ref and not is_name_ref(album_ref[0]) else song_id
        self.catalog.remember_images(song_id, track)
        if album is not None and album_id is not None:
            self.catalog.remember_images(album_id, album)
        elif album_ref is not None and album_id is not None and cover_id == song_id:
            # an album known only by name: its songs carry the album art, so its cover is
            # the song's picture (also under the id older app caches use, without the artist)
            self.catalog.remember_images(album_id, track)
            legacy = make_id(PREFIX_APPLE_ALBUM, name_ref(album_ref[0], album_ref[1]))
            self.catalog.remember_images(legacy, track)
        genres = sorted(track.metadata.genres or []) if track.metadata else []
        youtube = is_youtube_id(item_id)
        bit_rate = YOUTUBE_BIT_RATE if youtube else APPLE_BIT_RATE
        suffix = "opus" if youtube else "m4a"
        title = display_name(track.name, track.version)
        album_name = album_ref[1] if album_ref else ""
        artist_name = "; ".join(a[1] for a in artists)
        return {
            "id": song_id,
            "parent": album_id,
            "isDir": False,
            "title": title,
            "album": album_name or None,
            "artist": artist_name,
            "track": track.track_number or None,
            "year": album.year if album else None,
            "genre": genres[0] if genres else None,
            "coverArt": cover_id,
            "size": int(track.duration * bit_rate * 1000 / 8),
            "contentType": CONTENT_TYPES[suffix],
            "suffix": suffix,
            "duration": track.duration,
            "bitRate": bit_rate,
            "path": f"{self.catalog.service_name(item_id)}/{artist_name}/{album_name}/{title}.{suffix}",
            "discNumber": (track.disc_number or 1) if album_id else None,
            "albumId": album_id,
            "artistId": make_id(PREFIX_APPLE_ARTIST, artists[0][0]) if artists else None,
            "type": "music",
            "starred": iso_time(time.time()) if track.favorite else None,
            "userRating": self.ratings.get(song_id),
            "bpm": 0,
            "comment": "",
            "sortName": (track.sort_name or track.name).casefold(),
            "mediaType": "song",
            "musicBrainzId": "",
            "isrc": [],
            "genres": [{"name": g} for g in genres],
            "replayGain": {},
            "channelCount": 2,
            "samplingRate": 44100,
            "bitDepth": 16,
            "moods": [],
            "artists": [{"id": make_id(PREFIX_APPLE_ARTIST, a[0]), "name": a[1]} for a in artists],
            "displayArtist": artist_name,
            "albumArtists": [],
            "displayAlbumArtist": "",
            "contributors": [],
            "displayComposer": "",
            "explicitStatus": "",
            "groupings": [],
            "works": [],
            "movements": [],
        }

    def apple_album(self, album: Album, tracks: list[Track] | None = None) -> dict[str, Any]:
        """
        Return the albumID3 entry of an Apple Music album.

        :param album: The album.
        :param tracks: The album's tracks to include as songs (none when omitted).
        """
        assert self.catalog is not None
        item_id = name_ref(self.catalog.apple_item_id(album) or album.item_id, album.name)
        album_id = make_id(PREFIX_APPLE_ALBUM, item_id)
        self.catalog.remember_images(album_id, album)
        artists = [r for r in (self._apple_ref(a) for a in album.artists) if r is not None]
        songs = [s for s in (self.apple_song(t) for t in tracks or []) if s is not None]
        genres = sorted(album.metadata.genres or []) if album.metadata else []
        entry: dict[str, Any] = {
            "id": album_id,
            "name": display_name(album.name, album.version),
            "artist": "; ".join(a[1] for a in artists),
            "artistId": make_id(PREFIX_APPLE_ARTIST, artists[0][0]) if artists else None,
            "coverArt": album_id,
            "songCount": len(songs),
            "duration": sum(int(s["duration"] or 0) for s in songs),
            "year": album.year,
            "genre": genres[0] if genres else None,
            "userRating": self.ratings.get(album_id, 0),
            "genres": [{"name": g} for g in genres],
            "musicBrainzId": "",
            "isCompilation": str(album.album_type) == "compilation",
            "sortName": (album.sort_name or album.name).casefold(),
            "discTitles": [],
            "originalReleaseDate": {},
            "releaseDate": {"year": album.year} if album.year else {},
            "releaseTypes": [],
            "recordLabels": [],
            "moods": [],
            "artists": [{"id": make_id(PREFIX_APPLE_ARTIST, a[0]), "name": a[1]} for a in artists],
            "displayArtist": "; ".join(a[1] for a in artists),
            "explicitStatus": "",
            "version": album.version or "",
        }
        if tracks is not None:
            entry["song"] = songs
        return entry

    def apple_album_child(self, album: Album) -> dict[str, Any]:
        """
        Return an Apple Music album as a folder-style "child" entry (search2, directories).

        :param album: The album.
        """
        entry = self.apple_album(album)
        return {
            "id": entry["id"],
            "parent": entry["artistId"],
            "isDir": True,
            "title": entry["name"],
            "name": entry["name"],
            "album": entry["name"],
            "artist": entry["artist"],
            "year": entry["year"],
            "genre": entry["genre"],
            "coverArt": entry["coverArt"],
            "artistId": entry["artistId"],
            "songCount": 0,
            "sortName": entry["sortName"],
            "mediaType": "album",
            "artists": entry["artists"],
            "displayArtist": entry["displayArtist"],
        }

    def apple_artist(self, artist: Artist, albums: list[Album] | None = None) -> dict[str, Any]:
        """
        Return the artistID3 entry of an Apple Music artist.

        :param artist: The artist.
        :param albums: The artist's albums to include (none when omitted).
        """
        assert self.catalog is not None
        artist_id = make_id(
            PREFIX_APPLE_ARTIST,
            name_ref(self.catalog.apple_item_id(artist) or artist.item_id, artist.name),
        )
        self.catalog.remember_images(artist_id, artist)
        entry: dict[str, Any] = {
            "id": artist_id,
            "name": artist.name,
            "coverArt": artist_id,
            "albumCount": len(albums) if albums is not None else 0,
            "artistImageUrl": self.image_url(artist_id),
            "musicBrainzId": "",
            "sortName": (artist.sort_name or artist.name).casefold(),
            "roles": [],
        }
        if albums is not None:
            entry["album"] = [self.apple_album(a) for a in albums]
        return entry

    def apple_index_artist(self, artist: Artist) -> dict[str, Any]:
        """
        Return an Apple Music artist as a folder-style index entry (search2).

        :param artist: The artist.
        """
        entry = self.apple_artist(artist)
        return {k: entry[k] for k in ("id", "name", "coverArt", "artistImageUrl")}

    def _apple_ref(
        self, item: Album | Artist | ItemMapping, artist: str | None = None
    ) -> tuple[str, str] | None:
        """
        Return (Apple Music id, name) of an album or artist reference, if it has one.

        :param item: The album or artist (or its short reference).
        :param artist: For an album, its artist (helps find it when only its name is known).
        """
        assert self.catalog is not None
        if isinstance(item, ItemMapping):
            if item.provider == "library":
                return None
            item_id = self.catalog.ref_id(item.provider, item.item_id)
            return (name_ref(item_id, item.name, artist), item.name) if item_id else None
        item_id = self.catalog.apple_item_id(item)
        return (name_ref(item_id, item.name, artist), item.name) if item_id else None

    def _artists(self, artist_ids: list[int]) -> list[dict[str, Any]]:
        """
        Return id and name of the given artists.

        :param artist_ids: Library artist ids.
        """
        result = []
        for artist_id in artist_ids:
            if (artist := self.index.artists.get(artist_id)) is not None:
                result.append({"id": artist.id, "name": artist.name})
        return result


def _refs(artists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return artist references in the OpenSubsonic form.

    :param artists: Artists with library id and name.
    """
    return [{"id": make_id(PREFIX_ARTIST, a["id"]), "name": a["name"]} for a in artists]


def _join(artists: list[dict[str, Any]]) -> str:
    """
    Return the display string for several artists.

    :param artists: Artists with library id and name.
    """
    return "; ".join(a["name"] for a in artists)


def _replay_gain(track: IndexedTrack) -> dict[str, float]:
    """
    Return the ReplayGain values from MA's loudness measurement, empty when not measured.

    :param track: The track.
    """
    gain: dict[str, float] = {}
    if track.loudness is not None:
        gain["trackGain"] = round(REPLAYGAIN_REFERENCE_LUFS - track.loudness, 2)
    if track.album_loudness is not None:
        gain["albumGain"] = round(REPLAYGAIN_REFERENCE_LUFS - track.album_loudness, 2)
    if track.true_peak is not None:
        gain["trackPeak"] = round(10 ** (track.true_peak / 20), 6)
    return gain
