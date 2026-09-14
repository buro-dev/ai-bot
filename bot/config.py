"""Yapılandırma, loglama ve ortam yardımcıları.

Tüm gizli değerler (token, API anahtarı) yalnızca ortam değişkenlerinden
(veya yerel ``.env`` dosyasından) okunur; kod içinde sabitlenmez.
Render'da bunlar servisin "Environment Variables" bölümünden tanımlanır.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from bot import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY_FILE = REPO_ROOT / "chat_history.json"
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_FALLBACK_MODELS = ("openai/gpt-oss-20b",)
DEFAULT_WHISPER_MODEL = "whisper-large-v3-turbo"
DEFAULT_TIMEZONE = "Europe/Istanbul"
DEFAULT_PORT = 8000
WEBHOOK_PATH = "/webhook"

DEFAULT_SYSTEM_PROMPT = (
    "Sen Telegram üzerinden çalışan kişisel yapay zeka asistanısın. "
    "Yalnızca tek bir kullanıcıya hizmet edersin ve onun kişisel asistanısın.\n"
    "\n"
    "KURALLAR\n"
    "- Her zaman Türkçe yanıt ver.\n"
    "- Kısa, net ve doğrudan cevap ver; uzun giriş-çıkmaz, klişe övgü ve gereksiz "
    "özetlerden kaçın.\n"
    "- Bilmediğin şeyi uydurma; emin değilsen 'emin değilim' de.\n"
    "- Güncel tarihi ve saati, bu mesajın sonunda eklenen GÜNCEL BİLGİ bölümünden öğren.\n"
    "- Madde işaretleri ve kısa paragraflar kullanabilirsin; düz metin veya basit markdown uygundur.\n"
    "\n"
    "ARAÇLARIN\n"
    "- web_search: Güncel olay, haber, hava durumu, fiyat, spor sonucu, 'son dakika' "
    "gibi bilginin güncelliğini güvenle bilemeyeceğin konularda VE kullanıcı internetten "
    "aramanı istediğinde ara. Sorgu kısa ve hedefli olmalı (3-8 kelime).\n"
    "- save_memory: Kullanıcı 'hatırla', 'not al', 'aklında tut' dediğinde ya da önemli "
    "kişisel bir bilgi (isim, tercih, plan, adres, detay) verdiğinde kalıcı not olarak kaydet. "
    "Notu tek cümle, öz ve nesnel yaz.\n"
    "- set_reminder: Kullanıcı bir zaman verip bir şeyi hatırlatmanı istediğinde kullan. "
    "fire_at alanına kullanıcı saat dilimindeki (Europe/Istanbul) 'YYYY-MM-DD HH:MM' biçiminde "
    "MUTLAK zaman yaz; 'yarın', 'akşam 8' gibi ifadeleri GÜNCEL BİLGİ'deki saatten hesaplayarak çevir.\n"
    "Araç sonucu sana verilir; sonucu kendi cümlenle, doğal dille kullanıcıya aktar. "
    "İnternet aramasında kaynak linklerini mutlaka paylaş."
)


class ConfigError(RuntimeError):
    """Yapılandırma hataları için."""


# ===========================================================================
# Ortam yardımcıları
# ===========================================================================
def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "evet", "on", "acik", "açık")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_dotenv(path: Optional[Path] = None, override: bool = False) -> Optional[Path]:
    """Yerel ``.env`` dosyası varsa oku (Render'da kullanılmaz)."""
    try:
        from dotenv import load_dotenv as _load
    except ImportError:
        return None
    env_path = Path(path) if path else REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    _load(dotenv_path=env_path, override=override)
    return env_path


# ===========================================================================
# Config
# ===========================================================================
@dataclass
class Config:
    """Çalışma zamanı yapılandırması."""

    telegram_token: str
    allowed_user_ids: Tuple[int, ...]
    groq_api_key: str

    gh_pat_token: Optional[str] = None
    github_repository: Optional[str] = None

    # Birincil model + yedekler (birincil kullanılamazsa sırayla denenir).
    models: Tuple[str, ...] = (DEFAULT_MODEL, *DEFAULT_FALLBACK_MODELS)
    temperature: float = 0.7
    max_tokens: int = 1024
    top_p: float = 1.0
    request_timeout: float = 60.0
    max_retries: int = 2

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    history_file: Path = DEFAULT_HISTORY_FILE
    max_history_messages: int = 40
    max_prompt_chars: int = 24000
    max_memories: int = 50

    # Git ile kalıcı hafıza senkronu (Render'ın silinen dosya sistemi için).
    git_push_enabled: bool = True
    git_push_min_interval: float = 15.0
    git_memory_branch: str = "memory"
    git_remote: str = "origin"
    git_user_name: str = "telegram-ai-bot"
    git_user_email: str = "telegram-ai-bot@users.noreply.github.com"
    commit_message_prefix: str = "hafiza"

    # Çalışma modu: auto | poll | webhook
    # auto: PUBLIC_URL tanımlıysa webhook (Render), değilse poll (yerel).
    run_mode: str = "auto"
    public_url: Optional[str] = None
    webhook_path: str = WEBHOOK_PATH
    webhook_secret: Optional[str] = None  # boşsa webhook modunda otomatik üretilir
    port: int = DEFAULT_PORT

    # Özellik anahtarları
    search_enabled: bool = True
    search_region: str = "tr-tr"
    search_max_results: int = 5
    voice_enabled: bool = True
    whisper_model: str = DEFAULT_WHISPER_MODEL
    reminders_enabled: bool = True
    files_enabled: bool = True

    timezone_name: str = DEFAULT_TIMEZONE

    max_photo_bytes: int = 10 * 1024 * 1024
    max_voice_bytes: int = 10 * 1024 * 1024
    max_document_bytes: int = 5 * 1024 * 1024
    max_file_text_chars: int = 16000

    dry_run: bool = False
    telegram_api_base_url: Optional[str] = None

    def redaction_secrets(self) -> List[str]:
        return [
            s
            for s in (self.telegram_token, self.groq_api_key, self.gh_pat_token, self.webhook_secret)
            if s
        ]

    def masked(self) -> str:
        """Loglarda güvenle paylaşılabilen özet."""
        return (
            "v{v} | izinli_kullanıcı={users} | model={model} | mod={mode} | git_hafiza={git} | "
            "arama={search} | ses={voice} | hatirlatici={reminder} | dosya={files}".format(
                v=__version__,
                users=list(self.allowed_user_ids) or "AYARLANMADI",
                model=self.models[0],
                mode=self.run_mode,
                git="açık" if self.git_push_enabled else "kapalı",
                search="açık" if self.search_enabled else "kapalı",
                voice="açık" if self.voice_enabled else "kapalı",
                reminder="açık" if self.reminders_enabled else "kapalı",
                files="açık" if self.files_enabled else "kapalı",
            )
        )


def load_config() -> Config:
    """Ortam değişkenlerinden ``Config`` üretir; eksik zorunlu değerlerde ConfigError atar."""
    token = _env("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")
    if not token:
        raise ConfigError(
            "TELEGRAM_TOKEN tanımlı değil. BotFather'dan aldığınız token'ı ortam değişkeni "
            "(yerelde .env) olarak tanımlayın. Render'da: servisin Environment Variables bölümü."
        )

    raw_allowed = _env("ALLOWED_USER_ID", "ALLOWED_USER_IDS")
    allowed: List[int] = []
    if raw_allowed:
        for chunk in re.split(r"[,;\s]+", raw_allowed):
            chunk = chunk.strip()
            if not chunk:
                continue
            if not chunk.lstrip("-").isdigit():
                raise ConfigError(
                    "ALLOWED_USER_ID sayısal olmalı (örn: 123456789). Geçersiz değer: %r. "
                    "Kendi ID'niz için Telegram'da @userinfobot'a yazın." % chunk
                )
            allowed.append(int(chunk))
    if not allowed:
        raise ConfigError(
            "ALLOWED_USER_ID tanımlı değil. Bot yalnızca tek bir kullanıcıya yanıt verdiği için "
            "bu değer zorunludur. Telegram'da @userinfobot'a yazıp sayısal ID'nizi öğrenin."
        )

    api_key = _env("GROQ_API_KEY", "GROQ_KEY")
    if not api_key:
        raise ConfigError(
            "GROQ_API_KEY tanımlı değil. https://console.groq.com/keys adresinden anahtar "
            "oluşturup ortam değişkeni olarak tanımlayın."
        )

    # Model listesi: birincil + yedekler (virgülle ayrılabilir).
    primary_model = _env("GROQ_MODEL", "GROQ_MODEL_NAME", default=DEFAULT_MODEL) or DEFAULT_MODEL
    models: List[str] = [m.strip() for m in str(primary_model).split(",") if m.strip()]
    if _env_bool("GROQ_DISABLE_FALLBACK_MODELS", False):
        fallbacks: List[str] = []
    else:
        fallback_raw = _env("GROQ_FALLBACK_MODELS")
        if fallback_raw is not None:
            fallbacks = [m.strip() for m in fallback_raw.split(",") if m.strip()]
        else:
            fallbacks = list(DEFAULT_FALLBACK_MODELS)
    for model in fallbacks:
        if model not in models:
            models.append(model)

    run_mode = (_env("RUN_MODE", default="auto") or "auto").strip().lower()
    if run_mode not in {"auto", "poll", "webhook"}:
        raise ConfigError("Geçersiz RUN_MODE: %r (auto|poll|webhook)" % run_mode)

    public_url = _env("PUBLIC_URL")
    if public_url:
        public_url = public_url.rstrip("/")
    if run_mode == "webhook" and not public_url:
        raise ConfigError(
            "Webhook modu için PUBLIC_URL zorunlu (örn: https://botunuz.onrender.com). "
            "Render bu değişkeni otomatik tanımlar; başka yerde elle verin."
        )

    secret = _env("WEBHOOK_SECRET")
    if not secret and (run_mode == "webhook" or (run_mode == "auto" and public_url)):
        secret = secrets.token_urlsafe(32)

    history_file = Path(_env("HISTORY_FILE", default=str(DEFAULT_HISTORY_FILE)) or str(DEFAULT_HISTORY_FILE)).expanduser()
    if not history_file.is_absolute():
        history_file = REPO_ROOT / history_file

    return Config(
        telegram_token=token,
        allowed_user_ids=tuple(allowed),
        groq_api_key=api_key,
        gh_pat_token=_env("GH_PAT_TOKEN", "GITHUB_PAT"),
        github_repository=_env("GITHUB_REPOSITORY"),
        models=tuple(models),
        temperature=_env_float("GROQ_TEMPERATURE", 0.7),
        max_tokens=_env_int("GROQ_MAX_TOKENS", 1024),
        top_p=_env_float("GROQ_TOP_P", 1.0),
        request_timeout=_env_float("GROQ_TIMEOUT", 60.0),
        max_retries=_env_int("GROQ_MAX_RETRIES", 2),
        system_prompt=_env("SYSTEM_PROMPT", default=DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT,
        history_file=history_file,
        max_history_messages=_env_int("MAX_HISTORY_MESSAGES", 40),
        max_prompt_chars=_env_int("MAX_PROMPT_CHARS", 24000),
        max_memories=_env_int("MAX_MEMORIES", 50),
        git_push_enabled=_env_bool("GIT_PUSH_ENABLED", True),
        git_push_min_interval=_env_float("GIT_PUSH_MIN_INTERVAL", 15.0),
        git_memory_branch=_env("GIT_MEMORY_BRANCH", default="memory") or "memory",
        git_remote=_env("GIT_REMOTE", default="origin") or "origin",
        git_user_name=_env("GIT_USER_NAME", default="telegram-ai-bot") or "telegram-ai-bot",
        git_user_email=_env("GIT_USER_EMAIL", default="telegram-ai-bot@users.noreply.github.com")
        or "telegram-ai-bot@users.noreply.github.com",
        commit_message_prefix=_env("COMMIT_MESSAGE_PREFIX", default="hafiza") or "hafiza",
        run_mode=run_mode,
        public_url=public_url,
        webhook_path=_env("WEBHOOK_PATH", default=WEBHOOK_PATH) or WEBHOOK_PATH,
        webhook_secret=secret,
        port=_env_int("PORT", DEFAULT_PORT),
        search_enabled=_env_bool("SEARCH_ENABLED", True),
        search_region=_env("SEARCH_REGION", default="tr-tr") or "tr-tr",
        search_max_results=_env_int("SEARCH_MAX_RESULTS", 5),
        voice_enabled=_env_bool("VOICE_ENABLED", True),
        whisper_model=_env("WHISPER_MODEL", default=DEFAULT_WHISPER_MODEL) or DEFAULT_WHISPER_MODEL,
        reminders_enabled=_env_bool("REMINDERS_ENABLED", True),
        files_enabled=_env_bool("FILES_ENABLED", True),
        timezone_name=_env("TIMEZONE", default=DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE,
        max_photo_bytes=_env_int("MAX_PHOTO_BYTES", 10 * 1024 * 1024),
        max_voice_bytes=_env_int("MAX_VOICE_BYTES", 10 * 1024 * 1024),
        max_document_bytes=_env_int("MAX_DOCUMENT_BYTES", 5 * 1024 * 1024),
        max_file_text_chars=_env_int("MAX_FILE_TEXT_CHARS", 16000),
        dry_run=_env_bool("DRY_RUN", False),
        telegram_api_base_url=_env("TELEGRAM_API_BASE_URL"),
    )


def resolve_mode(cfg: Config) -> str:
    """``auto`` modunu ortama göre çözer: PUBLIC_URL varsa webhook, değilse poll."""
    if cfg.run_mode != "auto":
        return cfg.run_mode
    return "webhook" if cfg.public_url else "poll"


# ===========================================================================
# Loglama (anahtar maskeleme ile)
# ===========================================================================
def redact(text: str, secrets: Sequence[str]) -> str:
    """Metin içindeki gizli değerleri ``***`` ile değiştirir."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "***")
    return text


class RedactingFilter(logging.Filter):
    """Log kayıtlarında geçen gizli değerleri maskeler."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets: List[str] = []
        for secret in secrets:
            self.add_secret(secret)

    def add_secret(self, secret: Optional[str]) -> None:
        if secret and len(secret) >= 8 and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if any(s in message for s in self._secrets):
            record.msg = redact(str(record.msg), self._secrets)
            if record.args:
                args = record.args if isinstance(record.args, tuple) else (record.args,)
                record.args = tuple(redact(str(a), self._secrets) for a in args)
        return True


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    level_name = (_env("LOG_LEVEL") or ("DEBUG" if verbose else "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    root.addFilter(RedactingFilter(cfg.redaction_secrets()))
    for noisy in ("httpx", "httpcore", "aiohttp", "apscheduler"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
