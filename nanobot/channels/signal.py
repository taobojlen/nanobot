"""Signal channel implementation using signal-cli-rest-api."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import SignalConfig


_MAX_CHUNK = 2000


def _split_message(content: str, max_len: int = _MAX_CHUNK) -> list[str]:
    """Split content into chunks within max_len, preferring line breaks."""
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        pos = cut.rfind("\n")
        if pos == -1:
            pos = cut.rfind(" ")
        if pos == -1:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


class SignalChannel(BaseChannel):
    """
    Signal channel using signal-cli-rest-api.

    Connects via WebSocket (json-rpc mode) or falls back to polling
    (normal/native mode). Sends messages via the REST API.

    See: https://github.com/bbernhard/signal-cli-rest-api
    """

    name = "signal"

    def __init__(self, config: SignalConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: SignalConfig = config
        self._http: httpx.AsyncClient | None = None
        self._ws = None

    async def start(self) -> None:
        """Start the Signal channel."""
        if not self.config.phone_number:
            logger.error("Signal phone_number not configured")
            return
        if not self.config.api_url:
            logger.error("Signal api_url not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)

        logger.info(
            "Starting Signal channel for {} via {}",
            self.config.phone_number,
            self.config.api_url,
        )

        mode = self.config.mode
        if mode == "websocket":
            await self._run_websocket()
        elif mode == "polling":
            await self._run_polling()
        else:
            # auto: try WebSocket first, fall back to polling
            if await self._probe_websocket():
                await self._run_websocket()
            else:
                logger.info("Signal: WebSocket not available, using HTTP polling")
                await self._run_polling()

    async def stop(self) -> None:
        """Stop the Signal channel."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _send_typing_indicator(self, recipient: str) -> None:
        """Send a typing indicator to the given recipient or group."""
        if not self._http or not self.config.typing_indicator:
            return
        url = f"{self.config.api_url}/v1/typing-indicator/{quote(self.config.phone_number, safe='')}"
        try:
            await self._http.put(url, json={"recipient": recipient})
        except Exception as e:
            logger.debug("Failed to send Signal typing indicator: {}", e)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message via the Signal REST API."""
        if msg.metadata.get("_typing"):
            await self._send_typing_indicator(msg.chat_id)
            return

        if not self._http:
            logger.warning("Signal channel not running")
            return
        if not msg.content or msg.content == "[empty message]":
            return

        url = f"{self.config.api_url}/v2/send"
        for chunk in _split_message(msg.content):
            try:
                payload: dict[str, Any] = {
                    "number": self.config.phone_number,
                    "message": chunk,
                    "recipients": [msg.chat_id],
                }
                resp = await self._http.post(url, json=payload)
                resp.raise_for_status()
            except Exception as e:
                logger.error("Failed to send Signal message to {}: {}", msg.chat_id, e)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @property
    def _signal_data_root(self) -> Path:
        """Return the signal-cli data directory visible to nanobot."""
        if self.config.data_path:
            return Path(self.config.data_path).expanduser()
        return Path.home() / ".nanobot" / "signal-data"

    def _resolve_att_path(self, att: dict) -> Path | None:
        """Resolve the on-disk path of a signal-cli attachment dict.

        signal-cli may supply an absolute path (native/polling mode) or just an
        id string (json-rpc/WebSocket mode).  If the absolute path already exists
        we use it directly; otherwise we look for the basename in our signal-data
        attachments directory.  This avoids hardcoding any signal-cli internal paths.
        """
        # id is the actual storage name on disk; filename is user-facing only.
        raw = att.get("id") or att.get("filename") or ""
        if not raw:
            return None
        p = Path(str(raw))
        if p.is_absolute() and p.exists():
            return p
        # Fall back: find by basename (id) in our signal-data attachments dir.
        return self._signal_data_root / "attachments" / p.name

    def _receive_path(self) -> str:
        """URL-encoded path segment for the registered phone number."""
        return quote(self.config.phone_number, safe="")

    def _ws_url(self) -> str:
        """Build the WebSocket URL for the receive endpoint."""
        base = (
            self.config.api_url
            .replace("https://", "wss://")
            .replace("http://", "ws://")
        )
        return f"{base}/v1/receive/{self._receive_path()}"

    def _poll_url(self) -> str:
        """Build the HTTP polling URL for the receive endpoint."""
        return f"{self.config.api_url}/v1/receive/{self._receive_path()}"

    async def _probe_websocket(self) -> bool:
        """Return True if a WebSocket upgrade succeeds (server is in json-rpc mode)."""
        import websockets
        try:
            async with asyncio.timeout(5):
                async with websockets.connect(self._ws_url()):
                    pass
            return True
        except Exception:
            return False

    async def _run_websocket(self) -> None:
        """Receive messages via WebSocket (json-rpc mode)."""
        import websockets

        ws_url = self._ws_url()
        logger.info("Signal: using WebSocket at {}", ws_url)

        while self._running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self._ws = ws
                    logger.info("Signal WebSocket connected")
                    async for raw in ws:
                        try:
                            await self._process_raw(raw)
                        except Exception as e:
                            logger.error("Error processing Signal message: {}", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._ws = None
                logger.warning("Signal WebSocket error: {}", e)
                if self._running:
                    logger.info("Signal: reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def _run_polling(self) -> None:
        """Receive messages via HTTP polling (normal/native mode)."""
        poll_url = self._poll_url()
        interval = self.config.poll_interval

        logger.info("Signal: using HTTP polling at {} (interval={}s)", poll_url, interval)

        while self._running:
            try:
                assert self._http is not None
                resp = await self._http.get(poll_url, params={"timeout": 1})
                resp.raise_for_status()
                messages = resp.json()
                if isinstance(messages, list):
                    for item in messages:
                        try:
                            await self._process_envelope(item)
                        except Exception as e:
                            logger.error("Error processing Signal envelope: {}", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Signal polling error: {}", e)

            await asyncio.sleep(interval)

    async def _process_raw(self, raw: str | bytes) -> None:
        """Parse a raw WebSocket frame and hand off to envelope processing."""
        if isinstance(raw, bytes):
            raw = raw.decode()
        data = json.loads(raw)
        await self._process_envelope(data)

    async def _process_envelope(self, data: dict[str, Any]) -> None:
        """Extract a user message from a signal-cli envelope and forward to bus."""
        envelope = data.get("envelope", {})
        if not envelope:
            return

        source = envelope.get("sourceNumber") or envelope.get("source", "")
        source_name = envelope.get("sourceName", "")

        data_msg = envelope.get("dataMessage")
        if not data_msg:
            # Ignore receipts, typing notifications, sync messages, etc.
            return

        body: str = data_msg.get("message") or ""

        # Signal protocol stores messages >~2000 chars as a text/x-signal-plain attachment.
        # The body field is truncated; read the attachment file for the full text.
        # Other attachments (images, files) are collected and forwarded as media paths.
        attachments = data_msg.get("attachments") or []
        if attachments:
            logger.debug("Signal: message has {} attachment(s): {}", len(attachments), attachments)
        media_paths: list[str] = []
        for att in attachments:
            content_type: str = att.get("contentType") or ""
            if content_type == "text/x-signal-plain":
                # Long message stored as a plain-text attachment; replace truncated body.
                logger.info(
                    "Signal: long message detected, text attachment id={} filename={}",
                    att.get("id"), att.get("filename"),
                )
                p = self._resolve_att_path(att)
                if p:
                    logger.info("Signal: reading full message text from {}", p)
                    try:
                        full_text = p.read_text(encoding="utf-8").strip()
                        if full_text:
                            logger.info(
                                "Signal: replaced truncated body ({} chars) with full text ({} chars)",
                                len(body), len(full_text),
                            )
                            body = full_text
                    except Exception as e:
                        logger.warning("Signal: failed to read text attachment {}: {}", p, e)
            else:
                # User-sent file or image.
                p = self._resolve_att_path(att)
                raw_name = att.get("filename") or att.get("id") or "attachment"
                name = Path(str(raw_name)).name
                logger.info("Signal: user attachment {} ({}) -> {}", name, content_type, p)
                if p and p.exists():
                    media_paths.append(str(p))
                    body = (body + "\n" if body else "") + f"[attachment: {name} ({content_type}) at {p}]"
                else:
                    logger.warning("Signal: attachment file not found: {}", p)
                    body = (body + "\n" if body else "") + f"[attachment: {name} ({content_type}) — file not accessible]"

        if not body and media_paths:
            body = "[attachment received]"

        if not body:
            return

        # chat_id: group ID for group messages, sender number for DMs
        group_info = data_msg.get("groupInfo") or data_msg.get("groupMessage")
        if group_info:
            chat_id = str(group_info.get("groupId", source))
        else:
            chat_id = source

        if not source:
            logger.warning("Signal: received message with no source, skipping")
            return

        logger.debug("Signal message from {}: {}...", source, body[:50])

        await self._send_typing_indicator(chat_id)
        await self._handle_message(
            sender_id=source,
            chat_id=chat_id,
            content=body,
            media=media_paths or None,
            metadata={
                "source_name": source_name,
                "timestamp": envelope.get("timestamp"),
                "account": data.get("account", ""),
            },
        )
