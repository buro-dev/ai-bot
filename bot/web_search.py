"""DuckDuckGo ile ücretsiz internet araması (API anahtarı gerektirmez).

``ddg_search`` senkron bir işlevdir (ddgs 9.x senkron API sunar); çağıran
taraf ``asyncio.to_thread`` ile event loop'u bloklamadan çalıştırır.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ai_bot")


def ddg_search(
    query: str,
    max_results: int = 5,
    region: str = "tr-tr",
    timeout: float = 30.0,
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Arama yapar; ``(sonuçlar, hata_mesajı)`` döndürür (hata yoksa None)."""
    query = (query or "").strip()
    if not query:
        return [], "Arama sorgusu boş."
    try:
        from ddgs import DDGS
    except ImportError:
        return [], "Arama paketi (ddgs) kurulu değil."

    try:
        ddgs = DDGS(timeout=timeout)
        rows = ddgs.text(query, max_results=max_results, region=region) or []
    except Exception as exc:  # noqa: BLE001 - ağ/blok hatalarını kullanıcıya ilet
        LOGGER.warning("DuckDuckGo araması başarısız: %s", exc)
        return [], "Arama şu an başarısız (DuckDuckGo bazen geçici olarak kısıtlar). Birazdan tekrar dene."

    results: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        results.append(
            {
                "title": str(row.get("title", "")).strip(),
                "url": str(row.get("href") or row.get("url", "")).strip(),
                "snippet": str(row.get("body") or row.get("snippet", "")).strip(),
            }
        )
        if len(results) >= max_results:
            break
    return results, None


def format_results(results: List[Dict[str, str]]) -> str:
    """Arama sonuçlarını araç/gösterim metnine çevirir."""
    lines: List[str] = []
    for index, row in enumerate(results, 1):
        parts = ["%d. %s" % (index, row.get("title") or "(başlık yok)")]
        if row.get("snippet"):
            parts.append(row["snippet"])
        if row.get("url"):
            parts.append(row["url"])
        lines.append("\n".join(parts))
    return "\n".join(lines)
