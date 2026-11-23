"""Tests for decrypting encrypted AAC with FFmpeg (raw + AsyncProcess path)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Final

import pytest
from music_assistant.helpers.process import AsyncProcess

from music_assistant.providers.apple_music.audio_cache import AudioDownload

ENCRYPTED_TEST_FILE: Final = Path(__file__).parent / "enc_test.m4a"
DECRYPTION_KEY: Final = "000102030405060708090a0b0c0d0e0f"


def _ensure_encrypted_test_track() -> Path:
    """Create a short encrypted AAC/MP4 test file if it does not exist."""
    if ENCRYPTED_TEST_FILE.exists() and ENCRYPTED_TEST_FILE.stat().st_size > 100_000:
        return ENCRYPTED_TEST_FILE

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=6",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-encryption_scheme",
        "cenc-aes-ctr",
        "-encryption_key",
        DECRYPTION_KEY,
        "-encryption_kid",
        "0f0e0d0c0b0a09080706050403020100",
        str(ENCRYPTED_TEST_FILE),
    ]
    subprocess.run(cmd, check=True)
    return ENCRYPTED_TEST_FILE


def test_ffmpeg_raw_decrypt_to_clear_aac(tmp_path: Path) -> None:
    """Practice test: raw subprocess ffmpeg decrypt + -c:a copy."""
    encrypted = _ensure_encrypted_test_track()
    clear_out = tmp_path / "clear_raw.m4a"

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-decryption_key",
        DECRYPTION_KEY,
        "-i",
        str(encrypted),
        "-c:a",
        "copy",
        str(clear_out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed: {result.stderr}"

    # Validate output
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(clear_out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    aac = next((s for s in streams if s.get("codec_name") == "aac"), None)
    assert aac is not None
    assert float(info["format"]["duration"]) > 5.0
    assert int(info["format"]["size"]) > 50_000

    # No encryption atoms left
    data = clear_out.read_bytes()
    assert b"tenc" not in data and b"pssh" not in data


@pytest.mark.asyncio
async def test_ffmpeg_asyncprocess_decrypt_to_clear_aac(tmp_path: Path) -> None:
    """Exact AsyncProcess code path used in AudioCache (post-processing style)."""
    encrypted = _ensure_encrypted_test_track()
    part_path = tmp_path / "part.m4a"
    clear_part = tmp_path / "clear_async.m4a"

    # Simulate what stream_and_store does: copy the encrypted file first
    part_path.write_bytes(encrypted.read_bytes())

    # This mirrors the post-processing block we have in audio_cache.py
    proc = AsyncProcess(
        [
            "ffmpeg",
            "-decryption_key",
            DECRYPTION_KEY,
            "-i",
            str(part_path),
            "-c:a",
            "copy",
            "-y",
            str(clear_part),
        ],
        stdin=False,
        stdout=False,
        stderr=False,
        name="ffmpeg-clear-test",
    )
    await proc.start()
    await proc.wait()

    assert proc.returncode == 0, "ffmpeg post-processing failed"
    assert clear_part.exists() and clear_part.stat().st_size > 50_000

    # Quick validation
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(clear_part),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    assert float(info["format"]["duration"]) > 5.0
    data = clear_part.read_bytes()
    assert b"tenc" not in data
