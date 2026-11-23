"""End to end check of the Apple Music audio cache with a real ffmpeg and a real HTTP server."""

import asyncio
import hashlib
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat, ItemMapping, ProviderMapping, Track, UniqueList

from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.providers.apple_music.audio_cache import AudioCache, AudioDownload

SRC = Path(sys.argv[1])
KEY = "000102030405060708090a0b0c0d0e0f"
KEY_ID = "0f0e0d0c0b0a09080706050403020100"
IN_FMT = AudioFormat(content_type=ContentType.MP4, codec_type=ContentType.AAC)
OUT_FMT = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2)


async def pcm(audio_input, extra: list[str], check: bool = True) -> bytes:
    proc = FFMpeg(audio_input=audio_input, input_format=IN_FMT, output_format=OUT_FMT, extra_input_args=extra)
    await proc.start()
    out = bytearray()
    async for chunk in proc.iter_chunked(65536):
        out += chunk
    await proc.close()
    assert not check or proc.returncode in (0, None), proc.returncode
    return bytes(out)


COVER = b"\xff\xd8\xff\xe0" + b"stands in for the picture MA fetches"


def fake_mass(session: aiohttp.ClientSession, tasks: list, cover_fails: bool = False) -> SimpleNamespace:
    """Stand in for what the cache uses of MA: the http session, background tasks and the image fetch."""

    def create_task(target, *args, **kwargs) -> asyncio.Task:
        kwargs.pop("task_id", None)
        task = asyncio.Task(target(*args, **kwargs), loop=asyncio.get_running_loop(), eager_start=True)
        tasks.append(task)
        return task

    async def image_url(_item, _image_type) -> str:
        return "https://example.invalid/cover.jpg"

    async def thumbnail(_url, **_kwargs) -> bytes:
        if cover_fails:
            raise MediaNotFoundError("no picture")
        return COVER

    metadata = SimpleNamespace(get_image_url_for_item=image_url, get_thumbnail=thumbnail)
    return SimpleNamespace(http_session=session, create_task=create_task, metadata=metadata)


def track() -> Track:
    pm = {ProviderMapping(item_id="42", provider_domain="apple_music", provider_instance="apple_music--t")}
    artist = ItemMapping(media_type=MediaType.ARTIST, item_id="a", provider="apple_music--t", name="Kid Band")
    t = Track(item_id="42", provider="apple_music--t", name="Sine Song", provider_mappings=pm,
              artists=UniqueList([artist]), track_number=1)
    t.album = ItemMapping(media_type=MediaType.ALBUM, item_id="b", provider="apple_music--t", name="Tones")
    return t


async def main() -> None:
    requests = 0
    tasks: list[asyncio.Task] = []

    async def serve(request: web.Request) -> web.StreamResponse:
        nonlocal requests
        requests += 1
        return web.Response(body=SRC.read_bytes())

    async def slow(request: web.Request) -> web.StreamResponse:
        body = SRC.read_bytes()
        response = web.StreamResponse(headers={"Content-Length": str(len(body))})
        await response.prepare(request)
        await response.write(body[:40000])
        await asyncio.sleep(5)
        await response.write(body[40000:])
        return response

    app = web.Application()
    app.router.add_get("/track.m4a", serve)
    app.router.add_get("/slow.m4a", slow)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/track.m4a"

    reference = await pcm(str(SRC), ["-decryption_key", KEY])
    print("reference pcm bytes:", len(reference))

    async with aiohttp.ClientSession() as session:
        provider = MagicMock(instance_id="apple_music--t")
        provider.logger = logging.getLogger("e2e")
        provider.mass = fake_mass(session, tasks)
        with tempfile.TemporaryDirectory() as tmp:
            cache = AudioCache(provider, tmp)
            assert await cache.setup()
            dl = AudioDownload(url=url, key_id=KEY_ID)

            # 1. miss: play from the start through a pipe, copy is kept
            first = await pcm(cache.stream_and_store("42", dl, track()), ["-decryption_key", KEY])
            cached = await cache.lookup("42", KEY_ID)
            print("1. miss  -> pcm identical to reference:", first == reference, "| cached:", cached and Path(cached).relative_to(tmp))
            assert first == reference and cached
            assert Path(cached).read_bytes() == SRC.read_bytes(), "cached file is not byte-identical to what the server sent"
            print("   cached file byte-identical to download:", True, "| requests:", requests)
            await asyncio.gather(*tasks)
            cover = Path(cached).parent / "cover.jpg"
            assert cover.read_bytes() == COVER, "no cover.jpg next to the cached track"
            print("   cover.jpg next to the track:", cover.relative_to(tmp), "| audio file still byte-identical:", Path(cached).read_bytes() == SRC.read_bytes())

            # 2. hit: play from the cached file with a fresh key, no download
            second = await pcm(cached, ["-decryption_key", KEY])
            print("2. hit   -> pcm identical to reference:", second == reference, "| requests still:", requests)
            assert second == reference and requests == 1

            # 3. hit with seek
            seeked = await pcm(cached, ["-ss", "3", "-decryption_key", KEY])
            print("3. hit+seek 3s -> pcm bytes:", len(seeked), "(about half of", len(reference), ")")

            # 4. wrong key on the cached file is not audio
            garbage = await pcm(cached, ["-decryption_key", "00" * 16], check=False)
            print("4. wrong key -> equals reference:", garbage == reference)
            assert garbage != reference

    # 5. miss with a seek: file is read to the end through the pipe, copy still complete
    async with aiohttp.ClientSession() as session:
        provider.mass = fake_mass(session, tasks)
        with tempfile.TemporaryDirectory() as tmp:
            cache = AudioCache(provider, tmp)
            await cache.setup()
            seeked_miss = await pcm(cache.stream_and_store("42", dl, track()), ["-ss", "3", "-decryption_key", KEY])
            cached = await cache.lookup("42", KEY_ID)
            print("5. miss+seek 3s -> pcm bytes:", len(seeked_miss), "| copy kept whole:", bool(cached))
            assert cached and Path(cached).read_bytes() == SRC.read_bytes()
            await asyncio.gather(*tasks)

    # 6. miss, player stops early (ffmpeg killed after 1 chunk): nothing kept
    async with aiohttp.ClientSession() as session:
        provider.mass = fake_mass(session, tasks)
        with tempfile.TemporaryDirectory() as tmp:
            cache = AudioCache(provider, tmp)
            await cache.setup()
            stream = cache.stream_and_store("42", AudioDownload(url=url.replace("track", "slow"), key_id=KEY_ID), track())
            proc = FFMpeg(audio_input=stream, input_format=IN_FMT, output_format=OUT_FMT, extra_input_args=["-decryption_key", KEY])
            await proc.start()
            async for _ in proc.iter_chunked(4096):
                break
            await proc.close()
            await asyncio.sleep(0.3)
            leftovers = [p.name for p in Path(tmp).rglob("*") if p.is_file()]
            print("6. stopped while the download was still running -> files left:", leftovers, "| cached:", await cache.lookup("42", KEY_ID))
            assert leftovers == [], leftovers

    # 7. miss, the cover can not be fetched: the audio is kept, no cover.jpg, nothing half written
    async with aiohttp.ClientSession() as session:
        provider.mass = fake_mass(session, tasks, cover_fails=True)
        with tempfile.TemporaryDirectory() as tmp:
            cache = AudioCache(provider, tmp)
            await cache.setup()
            _ = await pcm(cache.stream_and_store("42", dl, track()), ["-decryption_key", KEY])
            await asyncio.gather(*tasks)
            cached = await cache.lookup("42", KEY_ID)
            names = sorted(p.name for p in Path(tmp).rglob("*") if p.is_file() and ".ma-audio-cache" not in p.parts)
            print("7. cover fetch fails -> audio kept:", bool(cached), "| files:", names)
            assert cached and names == ["01 Sine Song.m4a"], names

    await runner.cleanup()
    print("ALL OK")


asyncio.run(main())
