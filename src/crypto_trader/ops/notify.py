"""Operator notifications (Telegram) and dead-man's-switch pings (healthchecks.io style URLs).

Secrets come only from the environment (.env); they are never logged. Missing config disables the channel quietly,
so PAPER/BACKTEST keep working without any account.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx

log = logging.getLogger(__name__)

TOKEN_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TG_BOT_TOKEN", "TG_TOKEN", "BOT_TOKEN")
CHAT_KEYS = ("TELEGRAM_CHAT_ID", "TG_CHAT_ID", "CHAT_ID", "TELEGRAM_CHATID")


def _first_env(keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return k, v.strip()
    return None, None


class Notifier:
    """Telegram sender with per-key de-duplication so a flapping condition does not spam the operator."""

    def __init__(self, token: str | None = None, chat_id: str | None = None, prefix: str = "",
                 transport: httpx.BaseTransport | None = None):
        if token is None and chat_id is None:
            _, token = _first_env(TOKEN_KEYS)
            _, chat_id = _first_env(CHAT_KEYS)
        self.token, self.chat_id = token, chat_id
        self.prefix = prefix or os.environ.get("CAT_NOTIFY_PREFIX", "[crypto-auto-trader]")
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._transport = transport
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, key: str | None = None, min_interval_s: float = 1800) -> bool:
        """Send unless the same ``key`` was sent within ``min_interval_s``. Never raises."""
        if key is not None:
            with self._lock:
                now = time.time()
                if now - self._last.get(key, 0) < min_interval_s:
                    return False
                self._last[key] = now
        if not self.enabled:
            log.info("notify (disabled): %s", text[:200])
            return False
        try:
            with httpx.Client(timeout=10, transport=self._transport) as c:
                r = c.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                           json={"chat_id": self.chat_id, "text": f"{self.prefix} {text}"[:4000],
                                 "disable_web_page_preview": True})
            ok = r.status_code == 200
        except httpx.HTTPError as e:
            log.warning("telegram send failed: %s", type(e).__name__)
            ok = False
        if ok:
            self.sent += 1
            log.info("notify sent: %s", text.splitlines()[0][:120])
        else:
            self.failed += 1
        return ok


def ping(url_env: str, fail: bool = False, msg: str = "", transport: httpx.BaseTransport | None = None) -> bool:
    """Ping a dead-man's-switch URL (e.g. https://hc-ping.com/<uuid>) taken from env var ``url_env``.

    If pings stop (process dead, machine asleep, network down) the external service alerts the operator.
    """
    url = os.environ.get(url_env)
    if not url:
        return False
    try:
        with httpx.Client(timeout=10, transport=transport) as c:
            r = c.post(url.rstrip("/") + ("/fail" if fail else ""), content=msg[:10_000].encode())
        return r.status_code < 400
    except httpx.HTTPError:
        return False


_NOTIFIER: Notifier | None = None


def get_notifier() -> Notifier:
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = Notifier()
    return _NOTIFIER


def describe_config() -> dict:
    """Which env keys were found (names only, never values)."""
    tk, _ = _first_env(TOKEN_KEYS)
    ck, _ = _first_env(CHAT_KEYS)
    return {"telegram_token_key": tk, "telegram_chat_key": ck,
            "healthcheck_collector": bool(os.environ.get("HEALTHCHECK_URL_COLLECTOR")),
            "healthcheck_paper": bool(os.environ.get("HEALTHCHECK_URL_PAPER"))}
