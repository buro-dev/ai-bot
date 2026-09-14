"""Zamanlanmış hatırlatıcılar (APScheduler)."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

LOGGER = logging.getLogger("ai_bot")

_FIRE_AT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})$")


def parse_fire_at(raw: str, tz) -> Optional[datetime]:
    """'YYYY-MM-DD HH:MM' metnini saat diliminde datetime'a çevirir; geçersizse None."""
    raw = (raw or "").strip()
    match = _FIRE_AT_RE.match(raw)
    if not match:
        return None
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return None


class ReminderScheduler:
    """Süreç içi (asyncio) zamanlayıcı; her hatırlatıcı için tek seferlik job."""

    def __init__(self, tz) -> None:
        self._tz = tz
        self._scheduler: Optional[AsyncIOScheduler] = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(timezone=self._tz)
            self._scheduler.start()
            LOGGER.debug("Hatırlatıcı zamanlayıcı başlatıldı (tz=%s)", self._tz)

    def schedule(
        self,
        job_id: str,
        fire_at: datetime,
        callback: Callable[..., Any],
        *args: Any,
    ) -> bool:
        """Gelecekteki ``fire_at`` için job kurar; geçersiz/geçmişte ise False."""
        if self._scheduler is None:
            return False
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=self._tz)
        if fire_at <= datetime.now(self._tz):
            return False
        self._scheduler.add_job(
            callback,
            trigger="date",
            run_date=fire_at,
            id=job_id,
            args=list(args),
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return True

    def pending_count(self) -> int:
        if self._scheduler is None:
            return 0
        return len(self._scheduler.get_jobs())

    def shutdown(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._scheduler = None
