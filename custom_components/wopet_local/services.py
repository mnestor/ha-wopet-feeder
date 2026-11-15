"""Voice-clip services: upload any audio file to a feeder clip slot, play a clip now."""

from __future__ import annotations

import array
import asyncio
import io
import sys
import wave
from pathlib import Path
from urllib.parse import unquote

import voluptuous as vol
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

SERVICE_UPLOAD_CLIP = "upload_clip"
SERVICE_PLAY_CLIP = "play_clip"
ATTR_CLIP = "clip"
ATTR_FILE = "file"
ATTR_LABEL = "label"
ATTR_ENTRY = "config_entry_id"
MAX_SECONDS = 10
RATE = 8000
MEDIA_LOCAL = "media-source://media_source/local/"
PEAK_TARGET = 29000              # about -1 dBFS: the feeder's speaker is small
MAX_GAIN = 8.0                   # do not blow up the hiss of a very quiet recording

_CLIP = vol.All(vol.Coerce(int), vol.Range(min=1, max=5))
UPLOAD_SCHEMA = vol.Schema({
    vol.Required(ATTR_CLIP): _CLIP,
    vol.Required(ATTR_FILE): cv.string,
    vol.Optional(ATTR_LABEL): vol.All(cv.string, vol.Length(min=1, max=31)),
    vol.Optional(ATTR_ENTRY): cv.string,
})
PLAY_SCHEMA = vol.Schema({
    vol.Required(ATTR_CLIP): _CLIP,
    vol.Optional(ATTR_ENTRY): cv.string,
})


def pick_entry(hass: HomeAssistant, wanted: str | None) -> ConfigEntry:
    entries = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and e.runtime_data.client is not None
    ]
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
    if len(entries) != 1:
        raise ServiceValidationError(
            "No Wopet feeder with a control token is set up"
            if not entries
            else "Several Wopet feeders: give config_entry_id"
        )
    return entries[0]


def _entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    return pick_entry(hass, call.data.get(ATTR_ENTRY))


async def store_clip(
    hass: HomeAssistant, entry: ConfigEntry, index: int, label: str, wav: bytes
) -> list[dict]:
    """Upload a converted clip (slot 0-4) and refresh the clip selects."""
    runtime = entry.runtime_data
    clips = await runtime.client.async_upload_clip(index, label, wav)
    if runtime.coordinator is not None:
        await runtime.coordinator.async_set_clips(clips)
    return clips


def _resolve(hass: HomeAssistant, name: str) -> Path:
    if name.startswith(MEDIA_LOCAL):
        local = hass.config.media_dirs.get("local")
        if local is None:
            raise ServiceValidationError("Home Assistant has no local media folder")
        path = Path(local) / unquote(name[len(MEDIA_LOCAL):])
    else:
        path = Path(name)
    path = path.resolve()
    if not hass.config.is_allowed_path(str(path)):
        raise ServiceValidationError(
            f"{path} is not in an allowed folder (allowlist_external_dirs or media)"
        )
    if not path.is_file():
        raise ServiceValidationError(f"{path} does not exist")
    return path


def normalize(pcm: bytes) -> bytes:
    """Scale 16-bit little-endian PCM so its peak sits just under full scale."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(v) for v in samples), default=0)
    if peak == 0:
        return samples.tobytes()
    gain = min(PEAK_TARGET / peak, MAX_GAIN)
    out = array.array("h", (max(-32768, min(32767, round(v * gain))) for v in samples))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


async def convert_to_clip(hass: HomeAssistant, path: Path) -> bytes:
    """Any audio file -> feeder WAV: PCM 16-bit, mono, 8 kHz, at most 10 s, peak-normalised."""
    binary = get_ffmpeg_manager(hass).binary
    proc = await asyncio.create_subprocess_exec(
        binary, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(path), "-vn", "-t", str(MAX_SECONDS),
        "-af", "highpass=f=200",
        "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pcm, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0 or len(pcm) < RATE // 5:
        raise HomeAssistantError(f"Could not convert {path.name}: {err.decode()[-300:].strip()}")
    pcm = normalize(pcm[: MAX_SECONDS * RATE * 2])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return buf.getvalue()


async def async_setup_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_UPLOAD_CLIP):
        return

    async def upload_clip(call: ServiceCall) -> None:
        entry = _entry(hass, call)
        path = await hass.async_add_executor_job(_resolve, hass, call.data[ATTR_FILE])
        wav = await convert_to_clip(hass, path)
        index = call.data[ATTR_CLIP] - 1
        label = call.data.get(ATTR_LABEL) or path.stem[:31]
        await store_clip(hass, entry, index, label, wav)

    async def play_clip(call: ServiceCall) -> None:
        entry = _entry(hass, call)
        await entry.runtime_data.client.async_play_clip(call.data[ATTR_CLIP] - 1)

    hass.services.async_register(DOMAIN, SERVICE_UPLOAD_CLIP, upload_clip, schema=UPLOAD_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_CLIP, play_clip, schema=PLAY_SCHEMA)
