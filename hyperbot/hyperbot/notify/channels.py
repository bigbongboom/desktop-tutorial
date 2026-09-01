"""Notification transports. Each one owns its own failure handling."""
from __future__ import annotations

import httpx

from ..log import get_logger
from .dispatcher import Channel, Event, Severity

log = get_logger("notify.channels")

DISCORD_COLOURS = {
    Severity.INFO: 0x5865F2,
    Severity.TRADE: 0x2ECC71,
    Severity.WARN: 0xE67E22,
    Severity.CRITICAL: 0xE74C3C,
}


class ConsoleChannel(Channel):
    name = "console"

    async def send(self, event: Event) -> None:
        level = {
            Severity.INFO: log.info,
            Severity.TRADE: log.info,
            Severity.WARN: log.warning,
            Severity.CRITICAL: log.critical,
        }[event.severity]
        detail = f" | {event.body}" if event.body else ""
        extras = " ".join(f"{k}={v}" for k, v in event.fields.items())
        level("%s%s%s", event.title, detail, f" | {extras}" if extras else "")


class _HttpChannel(Channel):
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()


class TelegramChannel(_HttpChannel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        super().__init__()
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, event: Event) -> None:
        lines = [f"*{_escape(event.title)}*"]
        if event.body:
            lines.append(_escape(event.body))
        for name, value in event.fields.items():
            lines.append(f"`{_escape(name)}`: {_escape(value)}")
        response = await self._client.post(
            self._url,
            json={
                "chat_id": self._chat_id,
                "text": "\n".join(lines),
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()


class DiscordChannel(_HttpChannel):
    name = "discord"

    def __init__(self, webhook_url: str):
        super().__init__()
        self._url = webhook_url

    async def send(self, event: Event) -> None:
        embed = {
            "title": event.title[:250],
            "description": event.body[:4000] or None,
            "color": DISCORD_COLOURS[event.severity],
            "fields": [
                {"name": name[:250], "value": str(value)[:1000], "inline": True}
                for name, value in list(event.fields.items())[:25]
            ],
        }
        response = await self._client.post(self._url, json={"embeds": [embed]})
        response.raise_for_status()


class WebhookChannel(_HttpChannel):
    name = "webhook"

    def __init__(self, url: str):
        super().__init__()
        self._url = url

    async def send(self, event: Event) -> None:
        response = await self._client.post(
            self._url,
            json={
                "title": event.title,
                "body": event.body,
                "severity": event.severity.name,
                "fields": event.fields,
            },
        )
        response.raise_for_status()


def _escape(text: str) -> str:
    """Telegram Markdown chokes on unescaped specials, and trader labels are
    arbitrary user-set strings from the leaderboard."""
    for char in ("_", "*", "[", "]", "`"):
        text = text.replace(char, f"\\{char}")
    return text
