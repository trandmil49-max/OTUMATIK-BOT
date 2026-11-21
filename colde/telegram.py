from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id
        self._last_alert = 0.0

    async def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            log.info("TELEGRAM (ayarsız) >>> %s", text.replace("\n", " | "))
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15) as cli:
                    r = await cli.post(url, json={"chat_id": self.chat_id, "text": text})
                if r.status_code == 200:
                    return True
                log.warning("Telegram %s: %s", r.status_code, r.text[:200])
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram hata: %s", e)
            await asyncio.sleep(2 * (attempt + 1))
        return False

    async def alert(self, text: str) -> None:
        """Kritik sistem uyarısı; saatte en fazla 1 kez."""
        if time.time() - self._last_alert < 3600:
            return
        self._last_alert = time.time()
        await self.send(f"⚠️ {text}")
