"""Signal channel implementation using signal-cli-rest-api."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import httpx
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base
from nanobot.utils.helpers import split_message

_MAX_CHUNK = 2000


def _quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


class SignalConfig(Base):
    """Signal channel configuration using signal-cli-rest-api."""

    enabled: bool = False
    api_url: str = "http://localhost:8080"
    phone_number: str = ""
    allow_from: list[str] = Field(default_factory=list)
    mode: Literal["auto", "websocket", "polling"] = "auto"
    poll_interval: float = 2.0
    typing_indicator: bool = True
    data_path: str = ""
    streaming: bool = False


class SignalChannel(BaseChannel):
    """
    Signal channel using signal-cli-rest-api.

    Connects via WebSocket (json-rpc mode) or falls back to polling
    (normal/native mode). Sends messages via the REST API.

    See: https://github.com/bbernhard/signal-cli-rest-api
    """

    name = "signal"
    display_name = "Signal"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return SignalConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = SignalConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: SignalConfig = config
        self._http: httpx.AsyncClient | None = None
        self._ws = None
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task

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
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)
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
        url = f"{self.config.api_url}/v1/typing-indicator/{_quote(self.config.phone_number)}"
        try:
            await self._http.put(url, json={"recipient": recipient})
        except Exception as e:
            logger.debug("Failed to send Signal typing indicator: {}", e)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message via the Signal REST API."""
        # Only stop typing indicator for final responses
        if not msg.metadata.get("_progress", False):
            self._stop_typing(msg.chat_id)

        if not self._http:
            logger.warning("Signal channel not running")
            return
        if not msg.content or msg.content == "[empty message]":
            return

        url = f"{self.config.api_url}/v2/send"
        for chunk in split_message(msg.content, _MAX_CHUNK):
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

    def _start_typing(self, chat_id: str) -> None:
        """Start sending typing indicator for a chat."""
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send typing indicator until cancelled."""
        try:
            while self._running:
                await self._send_typing_indicator(chat_id)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

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
        attachments directory.
        """
        raw = att.get("id") or att.get("filename") or ""
        if not raw:
            return None
        p = Path(str(raw))
        if p.is_absolute() and p.exists():
            return p
        return self._signal_data_root / "attachments" / p.name

    def _receive_path(self) -> str:
        return _quote(self.config.phone_number)

    def _ws_url(self) -> str:
        base = (
            self.config.api_url.replace("https://", "wss://").replace("http://", "ws://")
        )
        return f"{base}/v1/receive/{self._receive_path()}"

    def _poll_url(self) -> str:
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
            return

        body: str = data_msg.get("message") or ""

        # Signal stores messages >~2000 chars as a text/x-signal-plain attachment.
        attachments = data_msg.get("attachments") or []
        if attachments:
            logger.debug("Signal: message has {} attachment(s): {}", len(attachments), attachments)
        media_paths: list[str] = []
        for att in attachments:
            content_type: str = att.get("contentType") or ""
            if content_type == "text/x-signal-plain":
                logger.info(
                    "Signal: long message detected, text attachment id={} filename={}",
                    att.get("id"),
                    att.get("filename"),
                )
                p = self._resolve_att_path(att)
                if p:
                    logger.info("Signal: reading full message text from {}", p)
                    try:
                        full_text = p.read_text(encoding="utf-8").strip()
                        if full_text:
                            logger.info(
                                "Signal: replaced truncated body ({} chars) with full text ({} chars)",
                                len(body),
                                len(full_text),
                            )
                            body = full_text
                    except Exception as e:
                        logger.warning("Signal: failed to read text attachment {}: {}", p, e)
            else:
                p = self._resolve_att_path(att)
                raw_name = att.get("filename") or att.get("id") or "attachment"
                name = Path(str(raw_name)).name
                logger.info("Signal: user attachment {} ({}) -> {}", name, content_type, p)
                if p and p.exists():
                    media_paths.append(str(p))
                    body = (body + "\n" if body else "") + (
                        f"[attachment: {name} ({content_type}) at {p}]"
                    )
                else:
                    logger.warning("Signal: attachment file not found: {}", p)
                    body = (body + "\n" if body else "") + (
                        f"[attachment: {name} ({content_type}) — file not accessible]"
                    )

        if not body and media_paths:
            body = "[attachment received]"

        if not body:
            return

        group_info = data_msg.get("groupInfo") or data_msg.get("groupMessage")
        if group_info:
            chat_id = str(group_info.get("groupId", source))
        else:
            chat_id = source

        if not source:
            logger.warning("Signal: received message with no source, skipping")
            return

        logger.debug("Signal message from {}: {}...", source, body[:50])

        self._start_typing(chat_id)
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
