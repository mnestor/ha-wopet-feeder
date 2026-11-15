"""Record a voice clip in the dashboard: an authenticated upload API and the recorder card."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from aiohttp import web
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN
from .services import convert_to_clip, pick_entry, store_clip

FRONTEND = Path(__file__).parent / "frontend"
CARDS = ("wopet-clip-card.js", "wopet-schedule-card.js")   # served at /wopet_local/<file>
MAX_UPLOAD = 5 * 1024 * 1024        # a 10 s phone recording is well under 1 MB
SUFFIX = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
          "audio/aac": ".aac", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav"}
_READY = f"{DOMAIN}_recorder_ready"


def _write_temp(data: bytes, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="wopet_clip_", suffix=suffix)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    return Path(name)


class WopetClipsView(HomeAssistantView):
    """GET the feeder's clip slots."""

    url = "/api/wopet_local/clips"
    name = "api:wopet_local:clips"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        try:
            entry = pick_entry(hass, request.query.get("entry_id"))
        except ServiceValidationError as exc:
            return self.json_message(str(exc), 400)
        coordinator = entry.runtime_data.coordinator
        return self.json({"clips": coordinator.clips if coordinator else []})


class WopetClipUploadView(HomeAssistantView):
    """POST a recording (any format ffmpeg reads) into clip slot ?clip=1-5&label=..."""

    url = "/api/wopet_local/clip"
    name = "api:wopet_local:clip"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        try:
            slot = int(request.query.get("clip", ""))
        except ValueError:
            slot = 0
        if not 1 <= slot <= 5:
            return self.json_message("clip must be 1-5", 400)
        label = request.query.get("label", "").strip() or f"Clip {slot}"
        if len(label.encode()) > 31:
            return self.json_message("label is longer than 31 bytes", 400)
        try:
            entry = pick_entry(hass, request.query.get("entry_id"))
        except ServiceValidationError as exc:
            return self.json_message(str(exc), 400)
        if request.content_length is not None and request.content_length > MAX_UPLOAD:
            return self.json_message("recording too large", 413)
        data = await request.read()
        if not data or len(data) > MAX_UPLOAD:
            return self.json_message("empty or too large recording", 400)
        ctype = (request.content_type or "").split(";")[0].strip().lower()
        path = await hass.async_add_executor_job(_write_temp, data, SUFFIX.get(ctype, ".bin"))
        try:
            wav = await convert_to_clip(hass, path)
            clips = await store_clip(hass, entry, slot - 1, label, wav)
        except HomeAssistantError as exc:
            return self.json_message(str(exc), 502)
        finally:
            await hass.async_add_executor_job(path.unlink, True)
        return self.json({"clip": slot, "seconds": round((len(wav) - 44) / 16000, 1),
                          "clips": clips})


async def async_setup_recorder(hass: HomeAssistant) -> None:
    """Register the API and load the cards on every dashboard (no resource to add by hand)."""
    if hass.data.get(_READY):
        return
    hass.data[_READY] = True
    hass.http.register_view(WopetClipsView())
    hass.http.register_view(WopetClipUploadView())
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"/{DOMAIN}/{name}", str(FRONTEND / name), cache_headers=False)
         for name in CARDS]
    )
    versions = await hass.async_add_executor_job(
        lambda: [(FRONTEND / name).stat().st_mtime_ns for name in CARDS])
    for name, version in zip(CARDS, versions, strict=True):
        add_extra_js_url(hass, f"/{DOMAIN}/{name}?v={version}")   # cache-busts after an update
