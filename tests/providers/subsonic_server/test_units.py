"""Unit tests for the Subsonic server's response rendering, login, IDs and lyrics parsing."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import pytest
from music_assistant_models.auth import User

from music_assistant.providers.subsonic_server.api_media import parse_lyrics
from music_assistant.providers.subsonic_server.auth import Authenticator
from music_assistant.providers.subsonic_server.constants import (
    ERR_AUTH_NOT_SUPPORTED,
    ERR_MISSING_PARAM,
    ERR_NOT_FOUND,
    ERR_WRONG_CREDENTIALS,
    MAX_FAILED_LOGINS,
)
from music_assistant.providers.subsonic_server.index import index_letter, normalize
from music_assistant.providers.subsonic_server.mapping import iso_time, make_id, parse_id
from music_assistant.providers.subsonic_server.responses import (
    SubsonicError,
    envelope,
    render,
    to_xml,
)

USER = User(user_id="u1", username="dad", role="user")


async def _lookup(username: str) -> tuple[User, str] | None:
    """
    Return the test user and password for "dad".

    :param username: The username.
    """
    return (USER, "s3cret") if username == "dad" else None


def _token(password: str, salt: str) -> str:
    """
    Return the Subsonic login token.

    :param password: The password.
    :param salt: The salt.
    """
    return hashlib.md5((password + salt).encode()).hexdigest()


def test_xml_rendering_matches_navidrome_style() -> None:
    """Scalars are attributes, lists repeat, empty values are left out, value becomes text."""
    body: dict[str, Any] = {
        **envelope("navidrome", "0.64.2"),
        "album": {
            "id": "al-1",
            "name": 'Rock & "Roll"',
            "userRating": 0,
            "isCompilation": False,
            "musicBrainzId": "",
            "genres": [{"name": "Rock"}, {"name": "Pop"}],
            "moods": [],
            "replayGain": {},
            "song": [{"id": "tr-1", "isrc": ["X1"]}],
        },
        "lyrics": {"artist": "A", "value": "line <1>"},
        "playlists": {},
    }
    xml = to_xml(body)
    assert xml.startswith('<subsonic-response xmlns="http://subsonic.org/restapi" status="ok"')
    assert 'name="Rock &amp; &quot;Roll&quot;"' in xml
    assert 'isCompilation="false"' in xml
    assert "musicBrainzId" not in xml
    assert "moods" not in xml
    assert "replayGain" not in xml
    assert '<genres name="Rock"></genres><genres name="Pop"></genres>' in xml
    assert "<isrc>X1</isrc>" in xml
    assert '<lyrics artist="A">line &lt;1&gt;</lyrics>' in xml
    assert "<playlists></playlists>" in xml


def test_json_and_jsonp_rendering() -> None:
    """JSON keeps empty values but drops None; JSONP wraps it in a safe callback."""
    head = envelope("music-assistant", "2.10")
    response = render(head, {"song": {"id": "tr-1", "genres": [], "year": None}}, "json")
    assert response.content_type == "application/json"
    assert response.body is not None
    text = bytes(response.body).decode()  # type: ignore[arg-type]
    assert '"genres":[]' in text
    assert "year" not in text
    assert '"openSubsonic":true' in text
    jsonp = render(head, {}, "jsonp", "cb")
    assert jsonp.text is not None
    assert jsonp.text.startswith("cb({")
    unsafe = render(head, {}, "jsonp", "alert(1)//")
    assert unsafe.text is not None
    assert unsafe.text.startswith("callback(")


async def test_token_plain_and_hex_logins() -> None:
    """Token auth, plain and enc: passwords all log in; a wrong token does not."""
    auth = Authenticator(_lookup, logging.getLogger("test"))
    assert (
        await auth.authenticate({"u": "dad", "t": _token("s3cret", "ab"), "s": "ab"}, "1") is USER
    )
    assert await auth.authenticate({"u": "dad", "p": "s3cret"}, "1") is USER
    assert await auth.authenticate({"u": "dad", "p": "enc:" + b"s3cret".hex()}, "1") is USER
    with pytest.raises(SubsonicError) as err:
        await auth.authenticate({"u": "dad", "t": _token("wrong", "ab"), "s": "ab"}, "1")
    assert err.value.code == ERR_WRONG_CREDENTIALS


async def test_unknown_user_missing_params_and_api_key() -> None:
    """Unknown users fail like wrong passwords; missing parameters and apiKey have own codes."""
    auth = Authenticator(_lookup, logging.getLogger("test"))
    with pytest.raises(SubsonicError) as err:
        await auth.authenticate({"u": "nobody", "p": "s3cret"}, "1")
    assert err.value.code == ERR_WRONG_CREDENTIALS
    with pytest.raises(SubsonicError) as err:
        await auth.authenticate({"u": "dad"}, "1")
    assert err.value.code == ERR_MISSING_PARAM
    with pytest.raises(SubsonicError) as err:
        await auth.authenticate({"apiKey": "x"}, "1")
    assert err.value.code == ERR_AUTH_NOT_SUPPORTED


async def test_lockout_after_repeated_failures() -> None:
    """An address that keeps failing is refused even with the right password, others are not."""
    auth = Authenticator(_lookup, logging.getLogger("test"))
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(SubsonicError):
            await auth.authenticate({"u": "dad", "p": "nope"}, "10.0.0.9")
    with pytest.raises(SubsonicError):
        await auth.authenticate({"u": "dad", "p": "s3cret"}, "10.0.0.9")
    assert await auth.authenticate({"u": "dad", "p": "s3cret"}, "10.0.0.8") is USER


def test_ids_round_trip_and_reject_bad_values() -> None:
    """IDs carry their type; malformed or unexpected IDs are "not found"."""
    assert parse_id(make_id("al", 87)) == ("al", 87)
    assert parse_id("tr-5", "tr") == ("tr", 5)
    for bad in ("", "al-", "al-x", "zz-1", "87", None):
        with pytest.raises(SubsonicError) as err:
            parse_id(bad)
        assert err.value.code == ERR_NOT_FOUND
    with pytest.raises(SubsonicError):
        parse_id("al-1", "tr")


def test_iso_time_and_index_letters() -> None:
    """Times look like Navidrome's; artists are grouped by letter with "#" for the rest."""
    assert iso_time(0) is None
    assert iso_time(1700000000) == "2023-11-14T22:13:20.000Z"
    assert index_letter("abba") == "A"
    assert index_letter("élan") == "E"
    assert index_letter(".eehou") == "E"
    assert index_letter("2pac") == "#"
    assert normalize("Anvil of Crom (Remastered)!") == "anvil of crom remastered"


def test_lrc_lyrics_are_synced_and_sorted() -> None:
    """LRC timestamps become millisecond starts, tags are dropped, offsets applied."""
    synced = parse_lyrics(
        "[ar:Someone]\n[offset:+500]\n[00:12.50]Second\n[00:02.00][00:30.00]First"
    )
    assert synced == (
        True,
        [
            {"start": 1500, "value": "First"},
            {"start": 12000, "value": "Second"},
            {"start": 29500, "value": "First"},
        ],
    )
    assert parse_lyrics("\nLine one\nLine two\n") == (
        False,
        [{"value": "Line one"}, {"value": "Line two"}],
    )
    assert parse_lyrics("  \n") is None
