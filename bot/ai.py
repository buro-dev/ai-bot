"""Groq API istemcisi: sohbet (metin + görsel + araçlar) ve ses tanıma.

Model birincil + yedek listesi üzerinden denenir; birincil kullanımdan
kaldırılmışsa yedeğe otomatik geçilir.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config, redact

try:
    from groq import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        Groq,
        NotFoundError,
        RateLimitError,
    )
except ImportError as exc:  # pragma: no cover
    print("HATA: 'groq' paketi bulunamadı ({0}).\nKurulum: pip install -r requirements.txt".format(exc))
    raise SystemExit(2) from exc

LOGGER = logging.getLogger("ai_bot")

_MODEL_ERROR_HINTS = (
    "decommission",
    "deprecat",
    "not found",
    "does not exist",
    "no longer",
    "invalid model",
    "unsupported model",
    "model_not_found",
    "unknown model",
    "terminated",
)


def is_model_unavailable_error(exc: BaseException) -> bool:
    """Hata 'bu model artık yok / desteklenmiyor' türünde mi?"""
    if isinstance(exc, NotFoundError):
        return True
    status = getattr(exc, "status_code", None)
    if status not in (400, 404, 410):
        return False
    text = str(exc).lower()
    return ("model" in text) and any(hint in text for hint in _MODEL_ERROR_HINTS)


def _is_tools_rejection(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status != 400:
        return False
    text = str(exc).lower()
    return any(keyword in text for keyword in ("tool", "function"))


#: Modelin çağırabileceği araçlar (OpenAI function-calling formatı).
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "İnternetten güncel bilgi arama. Güncel olaylar, haberler, hava durumu, fiyatlar, "
                "spor sonuçları, 'son dakika' veya kullanıcının internetten öğrenmen gereken "
                "konularda kullan. Sorgu kısa ve hedefli olmalı (3-8 kelime)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Arama motoru sorgusu (kullanıcının diliyle, 3-8 kelime)",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Kullanıcının kalıcı olarak hatırlanmasını istediği bilgiyi not olarak kaydet. "
                "Kullanıcı 'hatırla', 'not al', 'aklında tut' dediğinde veya önemli kişisel bir "
                "bilgi (isim, tercih, plan, adres) verdiğinde kullan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "Tek cümlelik, öz ve nesnel not",
                    }
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "Zamanlanmış hatırlatıcı kur. Kullanıcı bir zaman verip bir şeyi hatırlatmanı "
                "istediğinde kullan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fire_at": {
                        "type": "string",
                        "description": (
                            "Tetiklenme zamanı, 'YYYY-MM-DD HH:MM' biçiminde, kullanıcı saat "
                            "diliminde (Europe/Istanbul). 'yarın', 'akşam 8' gibi göreceli "
                            "ifadeleri GÜNCEL BİLGİ'deki saatten hesaplayarak mutlak zamana çevir."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": "Kullanıcıya iletilecek hatırlatma metni",
                    },
                },
                "required": ["fire_at", "message"],
            },
        },
    },
]


@dataclass
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any]


@dataclass
class ChatResult:
    text: str
    model: str
    tool_calls: List[ToolCall] = field(default_factory=list)


class GroqChat:
    """Groq sohbet tamamlama sarmalayıcısı (model yedekleme ile)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = Groq(
            api_key=cfg.groq_api_key,
            timeout=cfg.request_timeout,
            max_retries=max(0, cfg.max_retries),
        )
        self._disabled: set = set()
        self._tools_disabled = False
        self._active_model: Optional[str] = None

    @property
    def active_model(self) -> str:
        return self._active_model or self.cfg.models[0]

    def tools_supported(self) -> bool:
        return not self._tools_disabled

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatResult:
        """Yanıt metnini (+ olası araç çağrılarını) ve kullanılan modeli döndürür.

        ``messages`` OpenAI chat formatındadır; son kullanıcı mesajının content'ı
        metin veya ``[{"type": "image_url", ...}, {"type": "text", ...}]`` parça
        listesi olabilir (görsel analizi).
        """
        use_tools = bool(tools) and self.tools_supported()
        errors: List[str] = []
        for model in self.cfg.models:
            if model in self._disabled:
                continue
            try:
                kwargs: Dict[str, Any] = dict(
                    model=model,
                    messages=messages,
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                    top_p=self.cfg.top_p,
                )
                if use_tools:
                    kwargs["tools"] = tools
                response = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - tiplere göre ayrıştırıyoruz
                if is_model_unavailable_error(exc):
                    self._disabled.add(model)
                    LOGGER.warning(
                        "Groq modeli kullanılamıyor: %s (%s)",
                        model,
                        redact(str(exc), self.cfg.redaction_secrets())[:300],
                    )
                    errors.append("%s -> %s" % (model, exc))
                    continue
                if use_tools and _is_tools_rejection(exc):
                    self._tools_disabled = True
                    use_tools = False
                    LOGGER.warning(
                        "Model 'tools' parametresini reddetti (%s); araçsız devam edilecek.",
                        type(exc).__name__,
                    )
                    continue
                raise

            text = ""
            tool_calls: List[ToolCall] = []
            try:
                message = response.choices[0].message
            except (AttributeError, IndexError):
                message = None
            if message is not None:
                text = (message.content or "").strip()
                for tc in message.tool_calls or []:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
            if not text and not tool_calls:
                text = "(Model boş yanıt döndürdü.)"
            self._active_model = model
            usage = getattr(response, "usage", None)
            if usage is not None:
                LOGGER.debug(
                    "Groq yanıtı: model=%s, prompt_tokens=%s, completion_tokens=%s",
                    model,
                    getattr(usage, "prompt_tokens", "?"),
                    getattr(usage, "completion_tokens", "?"),
                )
            return ChatResult(text=text, model=model, tool_calls=tool_calls)

        raise RuntimeError("Hiçbir Groq modeli kullanılamadı. Denenenler: %s" % ("; ".join(errors) or "yok"))

    def transcribe(self, data: bytes) -> str:
        """Sesli mesajı (OGG/Opus vb.) Whisper ile metne çevirir."""
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            with open(tmp_path, "rb") as fh:
                result = self._client.audio.transcriptions.create(
                    model=self.cfg.whisper_model,
                    file=fh,
                )
            return (getattr(result, "text", "") or "").strip()
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def friendly_error(exc: BaseException) -> str:
        """Kullanıcıya gösterilecek kısa hata metni."""
        if isinstance(exc, AuthenticationError):
            return "Groq API anahtarı geçersiz görünüyor (GROQ_API_KEY)."
        if isinstance(exc, RateLimitError):
            return "Groq API hız sınırı aşıldı; birkaç saniye sonra tekrar deneyin."
        if isinstance(exc, APITimeoutError):
            return "Groq API zaman aşımına uğradı."
        if isinstance(exc, APIConnectionError):
            return "Groq API'ye bağlanılamadı (ağ erişimi?)."
        if isinstance(exc, APIStatusError) and getattr(exc, "status_code", None) == 400:
            return "Groq isteği reddedildi (400). Model adı veya bağlam uzunluğu sorunlu olabilir."
        if isinstance(exc, RuntimeError) and "Groq modeli" in str(exc):
            return (
                "Listedeki hiçbir Groq modeli kullanılamadı. Modeller kullanımdan kaldırılmış "
                "olabilir; GROQ_MODEL / GROQ_FALLBACK_MODELS ayarlarını güncelleyin."
            )
        return "Beklenmeyen hata: %s" % type(exc).__name__
