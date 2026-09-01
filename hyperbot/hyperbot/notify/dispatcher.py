"""Notification fan-out: severity routing, deduplication, and channel isolation."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from ..config import NotifyConfig
from ..log import get_logger

log = get_logger("notify")


class Severity(IntEnum):
    INFO = 10
    TRADE = 20
    WARN = 30
    CRITICAL = 40


ICONS = {
    Severity.INFO: "*",
    Severity.TRADE: ">",
    Severity.WARN: "!",
    Severity.CRITICAL: "***",
}


@dataclass
class Event:
    title: str
    body: str = ""
    severity: Severity = Severity.INFO
    fields: dict[str, str] = field(default_factory=dict)
    dedupe_key: str = ""

    def as_text(self) -> str:
        lines = [f"{ICONS[self.severity]} {self.title}"]
        if self.body:
            lines.append(self.body)
        for name, value in self.fields.items():
            lines.append(f"  {name}: {value}")
        return "\n".join(lines)


class Channel:
    name = "channel"

    async def send(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:
        return None


class Dispatcher:
    def __init__(self, config: NotifyConfig):
        self.config = config
        self.channels: list[Channel] = []
        self._last_sent: dict[str, float] = {}
        self._threshold = getattr(Severity, config.min_severity.upper(), Severity.INFO)

    def add(self, channel: Channel) -> None:
        self.channels.append(channel)
        log.info("notifications: %s enabled", channel.name)

    def _suppressed(self, event: Event) -> bool:
        """Same key inside the cooldown fires once. A CRITICAL always fires."""
        if not event.dedupe_key or event.severity >= Severity.CRITICAL:
            return False
        now = time.time()
        last = self._last_sent.get(event.dedupe_key, 0.0)
        if now - last < self.config.cooldown_seconds:
            return True
        self._last_sent[event.dedupe_key] = now
        return False

    async def send(self, event: Event) -> None:
        if event.severity < self._threshold or self._suppressed(event):
            return
        # One dead channel must never block the others or the engine.
        results = await asyncio.gather(
            *(channel.send(event) for channel in self.channels), return_exceptions=True
        )
        for channel, result in zip(self.channels, results):
            if isinstance(result, Exception):
                log.warning("notify via %s failed: %s", channel.name, result)

    # ---- convenience ------------------------------------------------------ #

    async def info(self, title: str, body: str = "", **kwargs: Any) -> None:
        await self.send(Event(title, body, Severity.INFO, **kwargs))

    async def trade(self, title: str, body: str = "", **kwargs: Any) -> None:
        await self.send(Event(title, body, Severity.TRADE, **kwargs))

    async def warn(self, title: str, body: str = "", **kwargs: Any) -> None:
        await self.send(Event(title, body, Severity.WARN, **kwargs))

    async def critical(self, title: str, body: str = "", **kwargs: Any) -> None:
        await self.send(Event(title, body, Severity.CRITICAL, **kwargs))

    async def close(self) -> None:
        for channel in self.channels:
            await channel.close()


def build_dispatcher(config: NotifyConfig) -> Dispatcher:
    from .channels import ConsoleChannel, DiscordChannel, TelegramChannel, WebhookChannel

    dispatcher = Dispatcher(config)
    if config.console:
        dispatcher.add(ConsoleChannel())
    if config.telegram_enabled and config.telegram_token and config.telegram_chat_id:
        dispatcher.add(TelegramChannel(config.telegram_token, config.telegram_chat_id))
    if config.discord_enabled and config.discord_webhook:
        dispatcher.add(DiscordChannel(config.discord_webhook))
    if config.webhook_enabled and config.webhook_url:
        dispatcher.add(WebhookChannel(config.webhook_url))
    return dispatcher
