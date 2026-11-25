"""Subsonic IDs and the conversion of indexed library items into Subsonic entries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .constants import ERR_NOT_FOUND
from .responses import SubsonicError

if TYPE_CHECKING:
    from collections.abc import Callable

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
    if not number.isdigit() or (allowed and prefix not in allowed):
        raise SubsonicError(ERR_NOT_FOUND, "Item not found")
    if prefix not in (PREFIX_ARTIST, PREFIX_ALBUM, PREFIX_TRACK, PREFIX_PLAYLIST):
        raise SubsonicError(ERR_NOT_FOUND, "Item not found")
    return prefix, int(number)


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
    ) -> None:
        """
        Initialize the mapper for one request.

        :param index: The library index.
        :param image_url: Returns a signed, login-free image URL for a cover art ID.
        :param ratings: The user's ratings by Subsonic ID.
        """
        self.index = index
        self.image_url = image_url
        self.ratings = ratings

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
