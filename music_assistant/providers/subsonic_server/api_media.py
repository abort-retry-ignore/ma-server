"""Subsonic media methods: stream and download files, cover art and lyrics."""

from __future__ import annotations

import asyncio
import os
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.errors import MusicAssistantError

from .api_library import track_from_id
from .constants import ERR_NOT_FOUND
from .context import RequestContext, api_method
from .index import normalize
from .responses import SubsonicError

if TYPE_CHECKING:
    from .index import IndexedTrack

LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
LRC_TAG = re.compile(r"^\[[a-zA-Z]+:.*\]$")
LRC_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]$", re.IGNORECASE)


@api_method("stream", "download")
async def stream(ctx: RequestContext) -> web.StreamResponse:
    """Send a song's file as it is, with range support (like Navidrome's raw format)."""
    track = track_from_id(ctx, ctx.require("id"))
    path = _checked_path(ctx, track)
    asked_format = ctx.get("format")
    max_bit_rate = ctx.get_int("maxBitRate", 0) or 0
    if (asked_format and asked_format not in ("raw", track.suffix)) or (
        max_bit_rate and max_bit_rate < track.bit_rate
    ):
        ctx.note = f" (asked for {asked_format or track.suffix} at {max_bit_rate} kbps, sent as is)"
    headers = {
        "Content-Type": track.content_type,
        "X-Content-Duration": f"{float(track.duration):.2f}",
        "Accept-Ranges": "bytes",
    }
    if ctx.method == "download":
        name = urllib.parse.quote(Path(track.rel_path).name)
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{name}"
    return web.FileResponse(path, headers=headers)


@api_method("getCoverArt")
async def get_cover_art(ctx: RequestContext) -> web.StreamResponse:
    """Send the cover art of an album, song, artist or playlist."""
    size = ctx.get_int("size", 0) or 0
    image = await ctx.server.artwork.get(ctx.require("id"), max(0, size))
    if image is None:
        raise SubsonicError(ERR_NOT_FOUND, "Artwork not found")
    return ctx.server.artwork.response(ctx.request, image)


@api_method("getAvatar")
async def get_avatar(ctx: RequestContext) -> web.StreamResponse:  # noqa: ARG001
    """Users have no avatar here."""
    raise SubsonicError(ERR_NOT_FOUND, "Avatar not found")


@api_method("getLyricsBySongId")
async def get_lyrics_by_song_id(ctx: RequestContext) -> dict[str, Any]:
    """Return a song's lyrics, synced line by line when an LRC file or LRC lyrics exist."""
    track = track_from_id(ctx, ctx.require("id"))
    lyrics = await _lyrics(ctx, track)
    if lyrics is None:
        return {"lyricsList": {}}
    synced, lines = lyrics
    entry: dict[str, Any] = {
        "displayArtist": ctx.mapper.song(track)["displayArtist"],
        "displayTitle": track.name,
        "lang": "xxx",
        "line": lines,
        "synced": synced,
    }
    return {"lyricsList": {"structuredLyrics": [entry]}}


@api_method("getLyrics")
async def get_lyrics(ctx: RequestContext) -> dict[str, Any]:
    """Return the plain lyrics of the first song matching an artist and title."""
    title = normalize(ctx.get("title") or "")
    artist = normalize(ctx.get("artist") or "")
    index = ctx.server.index
    for track in index.visible_tracks():
        if not title or normalize(track.name) != title:
            continue
        names = " ".join(normalize(index.artists[a].name) for a in track.artist_ids)
        if artist and artist not in names:
            continue
        lyrics = await _lyrics(ctx, track)
        if lyrics is None:
            continue
        text = "\n".join(str(line["value"]) for line in lyrics[1])
        return {"lyrics": {"artist": ctx.get("artist"), "title": track.name, "value": text}}
    return {"lyrics": {"value": ""}}


def _checked_path(ctx: RequestContext, track: IndexedTrack) -> str:
    """
    Return the track's file path after checking that it is still inside its music folder.

    :param ctx: The request context.
    :param track: The track.
    :raises SubsonicError: Not found (70) when the file is gone or outside the folder.
    """
    folder = next((f for f in ctx.server.index.folders if f.id == track.folder_id), None)
    if folder is None:
        raise SubsonicError(ERR_NOT_FOUND, "Song not found")
    real_path = os.path.realpath(track.abs_path)
    real_base = os.path.realpath(folder.base_path)
    if os.path.commonpath([real_path, real_base]) != real_base or not Path(real_path).is_file():
        raise SubsonicError(ERR_NOT_FOUND, "Song not found")
    return real_path


async def _lyrics(
    ctx: RequestContext, track: IndexedTrack
) -> tuple[bool, list[dict[str, Any]]] | None:
    """
    Return (synced, lines) for a track: its .lrc file first, then MA's stored lyrics.

    :param ctx: The request context.
    :param track: The track.
    """
    lrc_path = str(Path(track.abs_path).with_suffix(".lrc"))
    text = await asyncio.to_thread(_read_text, lrc_path)
    if text is None:
        try:
            item = await ctx.server.mass.music.tracks.get_library_item(track.id)
        except MusicAssistantError:
            return None
        metadata = item.metadata
        text = (metadata.lrc_lyrics or metadata.lyrics) if metadata else None
    if not text:
        return None
    return parse_lyrics(text)


def parse_lyrics(text: str) -> tuple[bool, list[dict[str, Any]]] | None:
    """
    Return (synced, lines) for LRC or plain lyrics text.

    :param text: The lyrics.
    """
    offset = 0
    timed: list[tuple[int, str]] = []
    plain: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if match := LRC_OFFSET.match(line):
            offset = int(match.group(1))
            continue
        stamps = LRC_TIME.findall(line)
        if stamps:
            words = LRC_TIME.sub("", line).strip()
            for minutes, seconds in stamps:
                start = int((int(minutes) * 60 + float(seconds.replace(":", "."))) * 1000)
                timed.append((max(0, start - offset), words))
            continue
        if LRC_TAG.match(line):
            continue
        plain.append(line)
    if timed:
        timed.sort(key=lambda t: t[0])
        return True, [{"start": start, "value": words} for start, words in timed]
    while plain and not plain[0]:
        plain.pop(0)
    while plain and not plain[-1]:
        plain.pop()
    if not plain:
        return None
    return False, [{"value": line} for line in plain]


def _read_text(path: str) -> str | None:
    """
    Return a text file's content, or None when it can't be read.

    :param path: The file path.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            return file.read()
    except OSError:
        return None
