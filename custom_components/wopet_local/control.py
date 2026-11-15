"""Client for the authenticated Wopet feeder bridge control API."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

PLAN_TIMEOUT = 20
FEED_TIMEOUT = 35


class WopetControlClient:
    """Talk to the bridge, keeping the lazy go2rtc stream alive while a command runs."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        token: str,
        stream_name: str,
    ) -> None:
        self._session = async_get_clientsession(hass)
        self._base = f"http://{host}:{port}"
        src = quote(stream_name, safe="")
        self._hold_url = f"http://{host}:1985/api/stream.mp4?src={src}"
        self._prime_url = f"http://{host}:1985/api/frame.jpeg?src={src}"
        self._streams_url = f"http://{host}:1985/api/streams"
        self._stream_name = stream_name
        self._headers = {"Authorization": f"Bearer {token}"}
        self._lock = asyncio.Lock()

    async def _is_ready(self) -> bool:
        try:
            async with self._session.get(
                f"{self._base}/health",
                headers=self._headers,
                timeout=ClientTimeout(total=2),
            ) as response:
                return response.status == 200
        except (ClientError, TimeoutError):
            return False

    async def _video_flowing(self) -> bool:
        """True once go2rtc's current producer is delivering video.

        go2rtc can abandon a producer process without killing it; the next producer
        then replaces it. Waiting for live video from go2rtc's own producer means the
        control port answering is the current session, not the one being replaced.
        """
        try:
            async with self._session.get(
                self._streams_url, timeout=ClientTimeout(total=3)
            ) as response:
                data = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError):
            return False
        stream = data.get(self._stream_name) if isinstance(data, dict) else None
        producers = (stream or {}).get("producers") or []
        return any((p or {}).get("bytes_recv", 0) > 0 for p in producers)

    async def _hold_stream(self) -> None:
        """Be a go2rtc consumer so its exec producer (the camera session) stays up."""
        try:
            async with self._session.get(
                self._hold_url, timeout=ClientTimeout(total=None, sock_read=30)
            ) as response:
                async for _chunk in response.content.iter_chunked(65536):
                    pass
        except (ClientError, TimeoutError):
            pass
        # Fall back to repeated snapshots if MP4 streaming is unavailable.
        while True:
            with suppress(ClientError, TimeoutError):
                async with self._session.get(
                    self._prime_url, timeout=ClientTimeout(total=20)
                ) as response:
                    await response.read()
            await asyncio.sleep(1)

    async def _call(
        self, method: str, path: str, body: dict[str, Any] | None, timeout: int
    ) -> dict[str, Any]:
        async with self._lock:
            holder = asyncio.create_task(self._hold_stream())
            try:
                for _attempt in range(60):
                    if await self._video_flowing() and await self._is_ready():
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise HomeAssistantError(
                        "Wopet camera session did not start (is the bridge app running?)"
                    )
                try:
                    async with self._session.request(
                        method,
                        f"{self._base}{path}",
                        json=body,
                        headers=self._headers,
                        timeout=ClientTimeout(total=timeout),
                    ) as response:
                        payload = await response.json(content_type=None)
                except (ClientError, TimeoutError, ValueError) as exc:
                    # Never retry after sending: a feed may already have happened.
                    raise HomeAssistantError(
                        "No answer from the Wopet bridge; the command may or may not "
                        "have reached the feeder"
                    ) from exc
            finally:
                holder.cancel()
                with suppress(asyncio.CancelledError):
                    await holder

        if not isinstance(payload, dict) or not payload.get("ok"):
            error = (
                payload.get("error", "camera command failed")
                if isinstance(payload, dict)
                else "bad reply"
            )
            raise HomeAssistantError(f"Wopet: {error}")
        return payload

    async def async_get_plan(self) -> list[dict[str, Any]]:
        payload = await self._call("GET", "/plan", None, PLAN_TIMEOUT)
        return payload["meals"]

    async def async_set_meal(self, slot: int, **changes: Any) -> list[dict[str, Any]]:
        payload = await self._call("POST", "/plan", {"slot": slot, **changes}, PLAN_TIMEOUT)
        return payload["meals"]

    async def async_feed(self, portions: int) -> None:
        await self._call("POST", "/feed", {"portions": portions}, FEED_TIMEOUT)
