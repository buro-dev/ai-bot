"""Telegram medya indirme ve dosya içerik çıkarma yardımcıları."""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Dict, Set

from telegram import Bot
from telegram.error import TelegramError

LOGGER = logging.getLogger("ai_bot")

IMAGE_EXTS: Set[str] = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MIME_BY_EXT: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
TEXT_EXTS: Set[str] = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".log", ".sql",
    ".py", ".ipynb", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".html", ".htm",
    ".sh", ".bash", ".zsh", ".bat", ".ps1",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".go", ".rs", ".java", ".kt", ".scala",
    ".php", ".rb", ".swift", ".m", ".lua", ".r", ".pl", ".dart", ".vue", ".svelte",
}


async def download_bytes(bot: Bot, file_id: str) -> bytes | None:
    """Telegram dosyasını belleğe indirir; hata durumunda None döner."""
    try:
        tg_file = await bot.get_file(file_id)
        await tg_file.download_as_bytearray()
        return bytes(tg_file.file_bytes)
    except (TelegramError, ValueError) as exc:
        LOGGER.error("Telegram dosyası indirilemedi: %s", exc)
        return None


def data_url(data: bytes, mime: str) -> str:
    """Base64 data URL üretir (Groq görsel girdisi için)."""
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def pdf_to_text(data: bytes, max_chars: int = 16000) -> str:
    """PDF metnini çıkarır (ilk ~60 sayfa / max_chars kadar)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - bozuk PDF
        LOGGER.warning("PDF okunamadı: %s", exc)
        return ""
    pages: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        if index >= 60:
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - sayfa bazlı hata
            continue
        pages.append(text)
        total += len(text)
        if total >= max_chars:
            break
    return "\n".join(pages)[:max_chars]


def decode_text(data: bytes, name: str = "") -> str:
    """Metin dosyasını olası kodlamalarla çözümlemeye çalışır (TR destekli)."""
    for encoding in ("utf-8", "latin5", "cp1254"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def ext_of(name: str | None) -> str:
    return Path(name or "").suffix.lower()
