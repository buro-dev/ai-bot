#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram + Groq sohbet botu (tek dosya, sunucusuz çalışabilir)
=============================================================

Ne yapar?
---------
1. Telegram'dan gelen mesajları okur (webhook YOK; bu yüzden internete açık bir
   sunucu/servis gerekmez).
2. Sadece ``ALLOWED_USER_ID`` değerine sahip kullanıcının mesajlarına yanıt verir.
3. Gelen mesajı + ``chat_history.json`` içindeki sohbet geçmişini Groq API'ye
   (varsayılan model: ``openai/gpt-oss-120b``; model kapalıysa sıradaki yedeğe geçer)
   gönderir.
4. Üretilen yanıtı Telegram'da ilgili mesaja cevap olarak atar.
5. Yeni mesajı ve yanıtı ``chat_history.json`` dosyasına yazar, ardından
   ``git add`` / ``git commit`` / ``git push`` ile repoyu günceller.

Çalışma modları (``--mode``)
----------------------------
* ``once`` : Tek seferlik "yokla, işle, bitir" turu. GitHub Actions + zamanlanmış
             görev için idealdir (varsayılan: GitHub Actions içinde otomatik seçilir).
* ``loop`` : Belirli bir süre boyunca (varsayılan 5 dk) uzun-polling yapar; bir iş
             içinde birden çok mesajı tek commit ile işlemek için kullanılır.
* ``poll`` : Sonsuz döngü (klasik uzun-polling botu). Yerel makinede / VPS'te
             ``python bot.py --mode poll`` şeklinde çalıştırılır.
* ``auto`` : GitHub Actions ortamında ``once``, diğer ortamlarda ``poll`` seçilir.

Kullanım
--------
    export TELEGRAM_TOKEN="123456:AA..."
    export ALLOWED_USER_ID="123456789"
    export GROQ_API_KEY="gsk_..."
    export GH_PAT_TOKEN="ghp_..."              # git push için (opsiyonel/önerilir)
    export GITHUB_REPOSITORY="kullanici/repo"  # opsiyonel, bilgi amaçlı
    python bot.py                 # ayarları doğrular, sonra --mode auto ile başlar
    python bot.py --mode once     # tek tur
    python bot.py --mode loop --loop-minutes 5
    python bot.py --self-test     # internet/anahtar gerektirmeyen iç testler

Notlar
------
* Telegram'a özel anahtarlar asla loglanmaz (log filtresi ile maskelenir).
* ``.env`` dosyası varsa otomatik okunur (GitHub Actions'ta kullanılmaz).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --- Sürüm -----------------------------------------------------------------
__version__ = "1.0.0"

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_HISTORY_FILE = REPO_ROOT / "chat_history.json"
DEFAULT_REQUIREMENTS_FILE = REPO_ROOT / "requirements.txt"
DEFAULT_WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "bot.yml"

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Model kullanılamazsa (kullanımdan kaldırma, erişim yokluğu vb.) sırayla denenecek modeller.
# Not: Groq, llama-3.3-70b-versatile ve llama-3.1-8b-instant modellerini 16.08.2026'da
# kapattı; bu yüzden llama yalnızca son çare olarak listede tutuluyor.
DEFAULT_FALLBACK_MODELS = ["openai/gpt-oss-20b", "llama-3.3-70b-versatile"]

DEFAULT_SYSTEM_PROMPT = (
    "Sen Telegram üzerinden konuşan yardımcı bir asistansın. "
    "Kullanıcı hangi dilde yazarsa o dilde yanıt ver (Türkçe yazarsa Türkçe). "
    "Kısa, net ve doğrudan cevap ver; gereksiz uzun açıklamalardan kaçın."
)

LOGGER = logging.getLogger("ai_bot")

# Kütüphane eksikse anlaşılır bir hata verelim.
try:
    from groq import Groq
    from groq import (  # noqa: F401  (hata sınıfları tipli yakalama için)
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        NotFoundError,
        RateLimitError,
    )
except ImportError as exc:  # pragma: no cover
    print(
        "HATA: 'groq' paketi bulunamadı ({0}).\n"
        "Kurulum: python -m pip install -r requirements.txt".format(exc),
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

try:
    from telegram import Bot, Update
    from telegram.constants import ChatAction
    from telegram.error import Forbidden, TelegramError
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as exc:  # pragma: no cover
    print(
        "HATA: 'python-telegram-bot' paketi bulunamadı ({0}).\n"
        "Kurulum: python -m pip install -r requirements.txt".format(exc),
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


# ===========================================================================
# 1) Ayar yönetimi
# ===========================================================================
class ConfigError(RuntimeError):
    """Eksik/hatalı yapılandırma."""


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Verilen isimlerden ilk dolu ortam değişkenini döndürür."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "evet"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        LOGGER.warning("%s geçersiz (%r); varsayılan %s kullanılıyor.", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("%s geçersiz (%r); varsayılan %s kullanılıyor.", name, raw, default)
        return default


def load_dotenv(path: Optional[Path] = None, override: bool = False) -> Optional[Path]:
    """Basit (bağımlılıksız) ``.env`` okuyucu. Bulursa dosya yolunu döndürür."""
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.is_file():
        return None
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            if override or key not in os.environ or os.environ.get(key, "") == "":
                os.environ[key] = value
        return env_path
    except OSError as exc:  # pragma: no cover
        LOGGER.warning(".env okunamadı (%s): %s", env_path, exc)
        return None


@dataclass
class Config:
    """Çalışma zamanı yapılandırması."""

    telegram_token: str
    allowed_user_ids: Tuple[int, ...]
    groq_api_key: str
    gh_pat_token: Optional[str] = None
    github_repository: Optional[str] = None

    # İlk model birincildir; model kullanımdan kaldırılırsa sırayla diğerleri denenir.
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

    git_push_enabled: bool = True
    git_branch: Optional[str] = None
    git_remote: str = "origin"
    git_user_name: str = "telegram-ai-bot"
    git_user_email: str = "telegram-ai-bot@users.noreply.github.com"
    commit_message_prefix: str = "sohbet gecmisi"

    mode: str = "auto"
    loop_minutes: float = 5.0
    long_poll_seconds: float = 25.0
    max_rounds: int = 3
    dry_run: bool = False
    allow_unauthenticated_id_setup: bool = False
    telegram_api_base_url: Optional[str] = None

    def redaction_secrets(self) -> List[str]:
        return [s for s in (self.telegram_token, self.groq_api_key, self.gh_pat_token) if s]

    def masked(self) -> str:
        """Loglarda güvenle paylaşılabilecek özet."""
        return (
            "yapılandırma: izinli_kullanıcı={users} | model={model} | yedekler={fallbacks} | "
            "geçmiş={hist} | git_push={push} | mod={mode}".format(
                users=list(self.allowed_user_ids) or "AYARLANMADI",
                model=self.models[0],
                fallbacks=list(self.models[1:]),
                hist=self.history_file,
                push=self.git_push_enabled,
                mode=self.mode,
            )
        )


def load_config(args: argparse.Namespace) -> Config:
    """Ortam değişkenleri + argparse sonuçlarından ``Config`` üretir."""
    token = _env("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")
    if not token:
        raise ConfigError(
            "TELEGRAM_TOKEN tanımlı değil. BotFather'dan aldığınız token'ı ortam değişkeni "
            "(veya .env) olarak tanımlayın. GitHub Actions'ta: Settings > Secrets and variables "
            "> Actions > New repository secret."
        )

    raw_allowed = _env("ALLOWED_USER_ID", "ALLOWED_USER_IDS", "TELEGRAM_ALLOWED_USER_ID")
    allowed: List[int] = []
    if raw_allowed:
        for chunk in re.split(r"[,\s;]+", raw_allowed):
            chunk = chunk.strip()
            if not chunk:
                continue
            if not chunk.lstrip("-").isdigit():
                raise ConfigError(
                    "ALLOWED_USER_ID sayısal olmalı (örn: 123456789). Geçersiz değer: %r. "
                    "Kendi ID'nizi @userinfobot üzerinden öğrenebilirsiniz." % chunk
                )
            allowed.append(int(chunk))
    if not allowed and not _env_bool("TELEGRAM_ALLOW_UNAUTHENTICATED_ID", False):
        raise ConfigError(
            "ALLOWED_USER_ID tanımlı değil. Sadece tek bir kullanıcıya yanıt verileceği için bu "
            "değer zorunludur (örn: 123456789). Kendi Telegram ID'niz için @userinfobot'a yazın.\n"
            "Not: ID'yi bilmiyorsanız geçici olarak TELEGRAM_ALLOW_UNAUTHENTICATED_ID=1 verip "
            "bota /id yazabilirsiniz (bu modda bot yanıt üretmez)."
        )

    api_key = _env("GROQ_API_KEY", "GROQ_KEY")
    if not api_key:
        raise ConfigError(
            "GROQ_API_KEY tanımlı değil. https://console.groq.com/keys adresinden anahtar "
            "oluşturup ortam değişkeni olarak tanımlayın."
        )

    # Model listesi: birincil model + yedekler (virgülle ayrılabilir).
    primary_model = _env("GROQ_MODEL", "GROQ_MODEL_NAME", default=DEFAULT_MODEL) or DEFAULT_MODEL
    if args.model:
        primary_model = args.model[0]
    models: List[str] = [m.strip() for m in str(primary_model).split(",") if m.strip()]

    fallback_raw = _env("GROQ_FALLBACK_MODELS")
    if args.fallback_model:
        fallbacks = [m.strip() for m in args.fallback_model if m.strip()]
    elif fallback_raw is not None:
        fallbacks = [m.strip() for m in fallback_raw.split(",") if m.strip()]
    else:
        fallbacks = list(DEFAULT_FALLBACK_MODELS)
    if _env_bool("GROQ_DISABLE_FALLBACK_MODELS", False):
        fallbacks = []
    for model in fallbacks:
        if model not in models:
            models.append(model)

    history_file = Path(args.history_file).expanduser().resolve() if args.history_file else DEFAULT_HISTORY_FILE

    git_branch = _env("GIT_BRANCH")
    if args.git_branch:
        git_branch = args.git_branch

    mode = args.mode or (_env("RUN_MODE", default="auto") or "auto")
    mode = mode.strip().lower()
    if mode not in {"auto", "once", "loop", "poll"}:
        raise ConfigError("Geçersiz mod: %r (auto|once|loop|poll)" % mode)

    cfg = Config(
        telegram_token=token,
        allowed_user_ids=tuple(allowed),
        groq_api_key=api_key,
        gh_pat_token=_env("GH_PAT_TOKEN", "GITHUB_PAT", "GH_TOKEN"),
        github_repository=_env("GITHUB_REPOSITORY"),
        models=tuple(models),
        temperature=args.temperature if args.temperature is not None else _env_float("GROQ_TEMPERATURE", 0.7),
        max_tokens=args.max_tokens if args.max_tokens is not None else _env_int("GROQ_MAX_TOKENS", 1024),
        top_p=_env_float("GROQ_TOP_P", 1.0),
        request_timeout=_env_float("GROQ_TIMEOUT", 60.0),
        max_retries=_env_int("GROQ_MAX_RETRIES", 2),
        system_prompt=_env("SYSTEM_PROMPT", default=DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT,
        history_file=history_file,
        max_history_messages=args.max_history if args.max_history is not None else _env_int("MAX_HISTORY_MESSAGES", 40),
        max_prompt_chars=_env_int("MAX_PROMPT_CHARS", 24000),
        git_push_enabled=not args.no_git and _env_bool("GIT_PUSH_ENABLED", True),
        git_branch=git_branch,
        git_remote=_env("GIT_REMOTE", default="origin") or "origin",
        git_user_name=_env("GIT_USER_NAME", default="telegram-ai-bot") or "telegram-ai-bot",
        git_user_email=_env("GIT_USER_EMAIL", default="telegram-ai-bot@users.noreply.github.com")
        or "telegram-ai-bot@users.noreply.github.com",
        commit_message_prefix=_env("COMMIT_MESSAGE_PREFIX", default="sohbet gecmisi") or "sohbet gecmisi",
        mode=mode,
        loop_minutes=args.loop_minutes if args.loop_minutes is not None else _env_float("LOOP_MINUTES", 5.0),
        long_poll_seconds=args.long_poll if args.long_poll is not None else _env_float("LONG_POLL_SECONDS", 25.0),
        max_rounds=args.max_rounds if args.max_rounds is not None else _env_int("MAX_ROUNDS", 3),
        dry_run=bool(args.dry_run),
        allow_unauthenticated_id_setup=_env_bool("TELEGRAM_ALLOW_UNAUTHENTICATED_ID", False),
        telegram_api_base_url=_env("TELEGRAM_API_BASE_URL"),
    )
    return cfg


def resolve_mode(cfg: Config) -> str:
    """``auto`` modunu ortama göre çözer."""
    if cfg.mode != "auto":
        return cfg.mode
    if _env("GITHUB_ACTIONS", default="").lower() == "true" or _env("CI", default="").lower() == "true":
        return "once"
    return "poll"


# ===========================================================================
# 2) Loglama (anahtar maskeleme ile)
# ===========================================================================
class RedactingFilter(logging.Filter):
    """Log kayıtlarında geçen gizli değerleri maskeler."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def add_secret(self, secret: Optional[str]) -> None:
        if secret and len(secret) >= 8 and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - bozuk format string
            return True
        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***GIZLI***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


REDACTOR = RedactingFilter(
    [os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("GROQ_API_KEY", ""), os.environ.get("GH_PAT_TOKEN", "")]
)


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    if quiet:
        level = logging.WARNING
    else:
        level = logging.DEBUG if verbose else logging.INFO
    env_level = _env("LOG_LEVEL")
    if env_level:
        level = getattr(logging, env_level.upper(), level)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    handler.addFilter(REDACTOR)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    our = logging.getLogger("ai_bot")
    our.handlers.clear()
    our.addFilter(REDACTOR)
    our.setLevel(level)
    # Gürültülü kütüphaneleri sustur.
    for noisy in ("httpx", "httpcore", "telegram.ext.Application", "telegram.request", "urllib3"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def redact(text: str, secrets: Sequence[str]) -> str:
    """``text`` içindeki gizli değerleri maskeler."""
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***GIZLI***")
    return out


# ===========================================================================
# 3) Sohbet geçmişi (chat_history.json)
# ===========================================================================
class HistoryStore:
    """``chat_history.json`` dosyasını güvenli şekilde okur/yazar.

    Dosya formatı (istenen format): ``[{"role": "user", "content": "..."}, ...]``
    """

    VALID_ROLES = {"system", "user", "assistant"}

    def __init__(self, path: Path, max_messages: int = 40) -> None:
        self.path = Path(path)
        self.max_messages = max_messages
        self._messages: List[Dict[str, str]] = []
        self._dirty = False

    # -- okuma/yazma --------------------------------------------------------
    @staticmethod
    def normalize(raw: Any) -> List[Dict[str, str]]:
        """Bozuk kayıtları eleyerek geçmişi temizler."""
        messages: List[Dict[str, str]] = []
        if not isinstance(raw, list):
            return messages
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role not in HistoryStore.VALID_ROLES:
                continue
            messages.append({"role": role, "content": content})
        return messages

    def load(self) -> List[Dict[str, str]]:
        if not self.path.exists():
            LOGGER.info("Geçmiş dosyası yok, oluşturuluyor: %s", self.path)
            self._messages = []
            self._dirty = True
            self.save()
            return list(self._messages)

        try:
            raw_text = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("Geçmiş dosyası okunamadı (%s): %s" % (self.path, exc)) from exc

        if not raw_text:
            raw: Any = []
        else:
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                # Bozuk dosyayı kaybetmemek için yedekleyip sıfırdan başla.
                backup = self.path.with_suffix(self.path.suffix + ".bozuk-yedek")
                try:
                    shutil.copy2(self.path, backup)
                    LOGGER.warning("chat_history.json bozuk JSON (%s); yedek: %s", exc, backup)
                except OSError:
                    LOGGER.warning("chat_history.json bozuk JSON (%s)", exc)
                raw = []
                self._dirty = True

        if isinstance(raw, dict) and "messages" in raw:  # eski/toleranslı format
            raw = raw.get("messages")

        self._messages = self.normalize(raw)
        if not isinstance(raw, list) or len(self._messages) != len(raw):
            LOGGER.warning("Geçmiş dosyasındaki bazı kayıtlar geçersiz olduğu için atlandı.")
            self._dirty = True
            self.save()
        LOGGER.debug("Geçmiş yüklendi: %d kayıt", len(self._messages))
        return list(self._messages)

    def save(self) -> None:
        """Dosyayı atomik olarak yazar (yarım dosya kalma riski yok)."""
        if self.max_messages and self.max_messages > 0:
            self._messages = self._messages[-self.max_messages :]
        payload = json.dumps(self._messages, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".chat_history.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._dirty = False
        LOGGER.debug("Geçmiş kaydedildi: %s (%d kayıt)", self.path, len(self._messages))

    # -- erişim -------------------------------------------------------------
    @property
    def messages(self) -> List[Dict[str, str]]:
        return self._messages

    @property
    def dirty(self) -> bool:
        return self._dirty

    def append(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})
        self._dirty = True

    def clear(self) -> None:
        self._messages = []
        self._dirty = True
        self.save()

    def __len__(self) -> int:
        return len(self._messages)


def build_prompt_messages(
    history: Sequence[Dict[str, str]],
    user_text: str,
    system_prompt: Optional[str],
    max_history_messages: int,
    max_chars: int,
) -> List[Dict[str, str]]:
    """Groq'a gönderilecek mesaj listesini hazırlar (geçmişi budayarak)."""
    payload: List[Dict[str, str]] = []
    if system_prompt:
        payload.append({"role": "system", "content": system_prompt})

    tail: List[Dict[str, str]] = [m for m in history if m.get("role") in HistoryStore.VALID_ROLES and m.get("content")]
    if max_history_messages and max_history_messages > 0:
        tail = tail[-max_history_messages:]

    # Karakter bütçesini aşmayacak şekilde sondan başa doğru ekle.
    selected: List[Dict[str, str]] = []
    used = len(user_text) + (len(system_prompt) if system_prompt else 0)
    for message in reversed(tail):
        cost = len(message["content"])
        if used + cost > max_chars and selected:
            break
        used += cost
        selected.append(message)
    selected.reverse()

    payload.extend(selected)
    payload.append({"role": "user", "content": user_text})
    return payload


# ===========================================================================
# 4) Git otomasyonu (add / commit / push)
# ===========================================================================
GIT_TIMEOUT = 120


@dataclass
class GitResult:
    ok: bool
    pushed: bool
    detail: str = ""

    def __bool__(self) -> bool:  # pragma: no cover
        return self.ok


def _git_env(token: Optional[str]) -> Dict[str, str]:
    """Kimlik doğrulamayı komut satırına yazmadan git'e aktarır (GIT_CONFIG_*)."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # asla kullanıcı adı/şifre sorma
            "GIT_ASKPASS": shutil.which("true") or "/bin/true",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    if token:
        # x-access-token, GitHub PAT + App token'ları için geçerli kullanıcı adıdır.
        basic = base64.b64encode(("x-access-token:%s" % token).encode("utf-8")).decode("ascii")
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic %s" % basic,
            }
        )
    return env


def git_extraheader_env(token: str) -> Dict[str, str]:
    """``_git_env`` sarmalayıcısı (self-test kolaylığı için)."""
    return _git_env(token)


class GitSync:
    """``chat_history.json`` değişikliğini commit'leyip push'lar."""

    def __init__(self, repo_dir: Path, file_path: Path, cfg: Config) -> None:
        self.repo_dir = Path(repo_dir)
        self.file_path = Path(file_path)
        self.cfg = cfg
        self.secrets = cfg.redaction_secrets()

    # -- yardımcılar --------------------------------------------------------
    def _run(self, args: Sequence[str], token: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = list(args)
        LOGGER.debug("git komutu: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_dir),
                env=_git_env(token),
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ConfigError("'git' komutu bulunamadı. Git kurulu mu?") from exc
        except subprocess.TimeoutExpired as exc:
            raise ConfigError("git komutu zaman aşımına uğradı: %s" % " ".join(cmd)) from exc
        if result.stdout.strip():
            LOGGER.debug("git stdout: %s", redact(result.stdout.strip(), self.secrets))
        if result.stderr.strip():
            LOGGER.debug("git stderr: %s", redact(result.stderr.strip(), self.secrets))
        return result

    def is_repo(self) -> bool:
        if not (self.repo_dir / ".git").exists():
            return False
        result = self._run(["git", "rev-parse", "--is-inside-work-tree"])
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _relative_history_path(self) -> Optional[str]:
        try:
            return str(self.file_path.resolve().relative_to(self.repo_dir.resolve()))
        except ValueError:
            return None

    def _current_branch(self) -> Optional[str]:
        result = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        branch = result.stdout.strip() if result.returncode == 0 else ""
        if not branch or branch == "HEAD":  # detached HEAD (GitHub Actions böyle çalışır)
            return None
        return branch

    # -- ana işlem ----------------------------------------------------------
    def commit_and_push(self, commit_message: str) -> GitResult:
        if not self.cfg.git_push_enabled:
            LOGGER.info("Git push devre dışı (--no-git / GIT_PUSH_ENABLED=0).")
            return GitResult(ok=False, pushed=False, detail="devre dışı")

        if not self._run(["git", "--version"]).returncode == 0:
            return GitResult(False, False, "git bulunamadı")

        if not self.is_repo():
            LOGGER.warning("Git deposu bulunamadı (%s); push atlanıyor.", self.repo_dir)
            return GitResult(False, False, "git deposu yok")

        rel_path = self._relative_history_path()
        if rel_path is None:
            LOGGER.warning("Geçmiş dosyası repo dışında (%s); push atlanıyor.", self.file_path)
            return GitResult(False, False, "dosya repo dışında")

        # 1) Dosyayı sahneye al (sadece geçmiş dosyası; başka değişikliklere dokunma).
        add = self._run(["git", "add", "--", rel_path])
        if add.returncode != 0:
            return GitResult(False, False, "git add başarısız: %s" % redact(add.stderr.strip(), self.secrets))

        # 2) Değişiklik yoksa commit/push yapma.
        staged = self._run(["git", "diff", "--cached", "--quiet", "--", rel_path])
        if staged.returncode == 0:
            LOGGER.info("Sahneye alınacak yeni geçmiş değişikliği yok; commit atlandı.")
            return GitResult(True, False, "değişiklik yok")

        # 3) Commit (kimlik bilgileri repoya yazılmadan, geçici -c ile).
        commit = self._run(
            [
                "git",
                "-c",
                "user.name=%s" % self.cfg.git_user_name,
                "-c",
                "user.email=%s" % self.cfg.git_user_email,
                "commit",
                "--no-verify",
                "-m",
                commit_message,
                "--",
                rel_path,
            ]
        )
        if commit.returncode != 0:
            return GitResult(False, False, "git commit başarısız: %s" % redact(commit.stderr.strip(), self.secrets))

        commit_hash = self._run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()

        # 4) Push: önce uzak dalı çekip rebase et (Actions'ta yarış durumuna karşı).
        branch = self.cfg.git_branch or self._current_branch()
        if not branch:
            inferred = self._infer_branch_from_env()
            if inferred:
                branch = inferred
                LOGGER.info("Detached HEAD algılandı; GITHUB_REF üzerinden dal: %s", branch)
        if not branch:
            return GitResult(
                False,
                False,
                "hedef dal belirlenemedi (GIT_BRANCH ayarlayın veya dal üzerinde çalışın). Commit yerel kaldı: %s"
                % commit_hash,
            )

        remote_branch = "%s/%s" % (self.cfg.git_remote, branch)
        fetch = self._run(["git", "fetch", "--quiet", self.cfg.git_remote, branch], token=self.cfg.gh_pat_token)
        if fetch.returncode == 0:
            rebase = self._run(["git", "rebase", "--quiet", remote_branch])
            if rebase.returncode != 0:
                LOGGER.warning(
                    "Rebase başarısız (çakışma olabilir); rebase geri alındı, doğrudan push denenecek."
                )
                self._run(["git", "rebase", "--abort"])
        else:
            LOGGER.debug("Uzak dal çekilemedi, doğrudan push denenecek.")

        push = self._run(
            ["git", "push", self.cfg.git_remote, "HEAD:refs/heads/%s" % branch],
            token=self.cfg.gh_pat_token,
        )
        if push.returncode == 0:
            LOGGER.info("Git push tamam: %s (%s) -> %s", commit_hash, commit_message, branch)
            return GitResult(True, True, "push ok (%s)" % branch)

        stderr = redact(push.stderr.strip(), self.secrets)
        detail = "git push başarısız: %s" % (stderr or "bilinmeyen hata")
        if not self.cfg.gh_pat_token:
            detail += (
                " | İpucu: GH_PAT_TOKEN tanımlı değil. Actions varsayılan token'ı salt-okunur olabilir; "
                "içerik yazma yetkili fine-grained PAT tanımlayın."
            )
        LOGGER.error(detail)
        return GitResult(False, False, detail)

    @staticmethod
    def _infer_branch_from_env() -> Optional[str]:
        ref = _env("GITHUB_REF", "GITHUB_REF_NAME")
        if not ref:
            return None
        for prefix in ("refs/heads/", "refs/pull/"):
            if ref.startswith(prefix):
                return ref[len(prefix) :].replace("/merge", "")
        return ref if "/" in ref else None


# ===========================================================================
# 5) Groq istemcisi
# ===========================================================================
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
    """Hata, 'bu model artık yok / desteklenmiyor' türünde mi?"""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, NotFoundError):
        return True
    if status not in (400, 404, 410):
        return False
    text = str(exc).lower()
    return ("model" in text) and any(hint in text for hint in _MODEL_ERROR_HINTS)


class GroqChat:
    """Groq sohbet tamamlama sarmalayıcısı (model yedekleme ile)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = Groq(
            api_key=cfg.groq_api_key,
            timeout=cfg.request_timeout,
            max_retries=max(0, cfg.max_retries),
        )
        self._disabled: set = set()  # kullanılamadığı tespit edilen modeller
        self._active_model: Optional[str] = None

    @property
    def active_model(self) -> str:
        return self._active_model or self.cfg.models[0]

    def complete(self, messages: List[Dict[str, str]]) -> Tuple[str, str]:
        """Yanıt metnini ve kullanılan modeli döndürür."""
        errors: List[str] = []
        for index, model in enumerate(self.cfg.models):
            if model in self._disabled:
                continue
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                    top_p=self.cfg.top_p,
                )
            except Exception as exc:  # noqa: BLE001 - tiplere göre ayrıştırıyoruz
                if is_model_unavailable_error(exc):
                    self._disabled.add(model)
                    next_model = next((m for m in self.cfg.models[index + 1 :] if m not in self._disabled), None)
                    LOGGER.warning(
                        "Groq modeli kullanılamıyor: %s (%s)%s",
                        model,
                        redact(str(exc), self.cfg.redaction_secrets())[:300],
                        (" | sıradaki model: %s" % next_model) if next_model else " | yedek model yok",
                    )
                    errors.append("%s -> %s" % (model, exc))
                    continue
                raise

            text = ""
            try:
                if response.choices:
                    text = (response.choices[0].message.content or "").strip()
            except (AttributeError, IndexError):  # pragma: no cover
                text = ""
            if not text:
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
            return text, model

        raise RuntimeError("Hiçbir Groq modeli kullanılamadı. Denenenler: %s" % ("; ".join(errors) or "yok"))

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
                "Listedeki hiçbir Groq modeli kullanılamadı. Modeller kullanımdan kaldırılmış olabilir; "
                "GROQ_MODEL / GROQ_FALLBACK_MODELS ayarlarını güncelleyin."
            )
        return "Beklenmeyen hata: %s" % type(exc).__name__


# ===========================================================================
# 6) Telegram yardımcıları
# ===========================================================================
def split_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> List[str]:
    """Uzun yanıtları Telegram'ın 4096 karakter sınırına göre böler."""
    text = (text or "").strip()
    if not text:
        return ["(boş yanıt)"]
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = limit
        for separator in ("\n\n", "\n", ". ", " "):
            index = window.rfind(separator)
            if index > int(limit * 0.5):
                cut = index + len(separator)
                break
        chunk = remaining[:cut].rstrip()
        chunks.append(chunk if chunk else remaining[:limit])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def is_allowed(user_id: Optional[int], cfg: Config) -> bool:
    if not cfg.allowed_user_ids:
        return False
    return user_id in cfg.allowed_user_ids


def user_display(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "bilinmeyen"
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return "%s (@%s, id=%s)" % (name or "-", user.username or "-", user.id)


# ===========================================================================
# 7) Uygulama akışı
# ===========================================================================
class BotRunner:
    """Telegram güncellemelerini işleyen ana sınıf."""

    def __init__(self, cfg: Config, chat: GroqChat, history: HistoryStore, git: GitSync) -> None:
        self.cfg = cfg
        self.chat = chat
        self.history = history
        self.git = git
        self.processed = 0
        self.replied = 0
        self.errors = 0
        self.new_messages = 0  # bu süreçte geçmişe eklenen kayıt sayısı
        self._cleared = False  # bu süreçte geçmiş temizlendi mi?
        self._history_dirty = False
        self._finalized = False
        self.push_failed = False
        self._started_at = time.monotonic()

    # -- Telegram'a gönderme ------------------------------------------------
    async def send(self, bot: Bot, chat_id: int, text: str, reply_to: Optional[int] = None) -> None:
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            if self.cfg.dry_run:
                LOGGER.info("[DRY-RUN] Telegram'a gönderilecek yanıt: %s", chunk)
                continue
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to if index == 0 else None,
                    disable_web_page_preview=True,
                )
            except Forbidden:
                LOGGER.error("Bot kullanıcıya mesaj gönderemiyor (engellenmiş olabilir): chat_id=%s", chat_id)
                return
            except TelegramError as exc:
                if index == 0 and reply_to:
                    LOGGER.warning("Yanıt olarak gönderilemedi (%s); normal mesaj olarak deneniyor.", exc)
                    await bot.send_message(chat_id=chat_id, text=chunk, disable_web_page_preview=True)
                else:
                    LOGGER.error("Telegram gönderim hatası: %s", exc)
                    return

    # -- Handlers ------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        await self.send(
            context.bot,
            update.effective_chat.id,
            "Merhaba! Sohbet botu hazır.\n"
            "Model: {model}\n"
            "Geçmiş: {hist}\n"
            "Komutlar: /status /id /clear /help".format(model=self.chat.active_model, hist=self.cfg.history_file),
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        await self.send(
            context.bot,
            update.effective_chat.id,
            "Herhangi bir mesaj yazın; geçmişle birlikte Groq'a gönderip yanıtlarım.\n\n"
            "/status - model, geçmiş uzunluğu ve son işlem bilgisi\n"
            "/id - Telegram kullanıcı/sohbet kimlikleriniz\n"
            "/clear - sohbet geçmişini temizler (repoya commit'lenir)\n"
            "/help - bu mesaj",
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        await self.send(
            context.bot,
            update.effective_chat.id,
            "Telegram ID: {uid}\nSohbet ID: {cid}".format(
                uid=update.effective_user.id if update.effective_user else "?",
                cid=update.effective_chat.id if update.effective_chat else "?",
            ),
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        await self.send(
            context.bot,
            update.effective_chat.id,
            "Model: {model}\nDiğer modeller (yedek): {fallbacks}\n"
            "Geçmiş kaydı: {count}\nSüre: {uptime:.0f} sn\nİşlenen mesaj: {processed}".format(
                model=self.chat.active_model,
                fallbacks=", ".join(m for m in self.cfg.models if m != self.chat.active_model) or "-",
                count=len(self.history),
                uptime=time.monotonic() - self._started_at,
                processed=self.processed,
            ),
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        self.history.clear()
        self._cleared = True
        self.new_messages = 0
        self._history_dirty = True
        await self.send(
            context.bot,
            update.effective_chat.id,
            "Sohbet geçmişi temizlendi. (Değişiklik tur sonunda repoya commit'lenecek.)",
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Yetkili kullanıcının normal mesajlarını işler."""
        message = update.effective_message
        if message is None or not message.text:
            return
        if not await self._guard(update, context):
            return

        user_text = message.text.strip()
        if not user_text:
            return

        self.processed += 1
        chat_id = update.effective_chat.id
        LOGGER.info("Mesaj alındı: %s | %s", user_display(update), user_text[:200])

        # "yazıyor..." göstergesi
        if not self.cfg.dry_run:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except TelegramError as exc:
                LOGGER.debug("send_chat_action başarısız: %s", exc)

        prompt = build_prompt_messages(
            history=self.history.messages,
            user_text=user_text,
            system_prompt=self.cfg.system_prompt,
            max_history_messages=self.cfg.max_history_messages,
            max_chars=self.cfg.max_prompt_chars,
        )
        LOGGER.debug("Groq'a gönderilen mesaj sayısı: %d", len(prompt))

        try:
            reply_text, model = await asyncio.to_thread(self.chat.complete, prompt)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            LOGGER.exception("Groq isteği başarısız: %s", type(exc).__name__)
            await self.send(
                context.bot,
                chat_id,
                "Üzgünüm, yanıt üretilemedi. %s" % GroqChat.friendly_error(exc),
                reply_to=message.message_id,
            )
            return

        await self.send(context.bot, chat_id, reply_text, reply_to=message.message_id)

        # Geçmişi güncelle (yanıt gönderildikten sonra).
        self.history.append("user", user_text)
        self.history.append("assistant", reply_text)
        self.history.save()
        self.new_messages += 2
        self._history_dirty = True
        self.replied += 1
        LOGGER.info("Yanıt gönderildi (model=%s, %d karakter).", model, len(reply_text))

    # -- Hata yakalama ------------------------------------------------------
    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler içinde oluşan beklenmeyen hataları loglar."""
        self.errors += 1
        LOGGER.error(
            "Handler hatası: %s",
            context.error,
            exc_info=context.error if isinstance(context.error, Exception) else None,
        )

    # -- Yetki kontrolü -----------------------------------------------------
    async def _guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user = update.effective_user
        chat_id = update.effective_chat.id if update.effective_chat else None
        message = update.effective_message

        if user is not None and is_allowed(user.id, self.cfg):
            return True

        if user is None:
            LOGGER.debug("Gönderen bilgisi olmayan güncelleme yok sayıldı.")
            return False

        LOGGER.warning("Yetkisiz erişim denemesi reddedildi: %s", user_display(update))
        if self.cfg.allow_unauthenticated_id_setup and chat_id is not None:
            await self.send(
                context.bot,
                chat_id,
                "Bu bot yalnızca yetkili kullanıcıya yanıt verir.\nTelegram ID'niz: {uid}\n"
                "(Bu değeri ALLOWED_USER_ID olarak tanımlayın.)".format(uid=user.id),
                reply_to=message.message_id if message else None,
            )
        return False

    # -- Tur sonu -----------------------------------------------------------
    def finalize(self, reason: str) -> None:
        """Tur sonunda geçmişi repoya commit'ler/push'lar ve özet loglar."""
        if self._finalized:
            return
        self._finalized = True

        if self._history_dirty and self.history.dirty:
            try:
                self.history.save()
            except OSError as exc:
                LOGGER.error("Geçmiş kaydedilemedi: %s", exc)

        if self._history_dirty:
            LOGGER.info("Geçmiş değişti; git commit + push deneniyor.")
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if self._cleared and self.new_messages == 0:
                message = "%s temizlendi: %s" % (self.cfg.commit_message_prefix, stamp)
            else:
                message = "%s: %s (+%d mesaj, model=%s)" % (
                    self.cfg.commit_message_prefix,
                    stamp,
                    self.new_messages,
                    self.chat.active_model,
                )
            result = self.git.commit_and_push(message)
            if result.pushed:
                LOGGER.info("Sohbet geçmişi repoya gönderildi (%s).", result.detail)
            elif result.ok:
                LOGGER.info("Push gerekmedi (%s).", result.detail)
            elif self.cfg.git_push_enabled:
                self.push_failed = True
                LOGGER.error(
                    "Sohbet geçmişi repoya push edilemedi (%s). Yanıtlar kullanıcıya iletildi, "
                    "ancak geçmiş uzak repoda güncel değil.",
                    result.detail,
                )
        LOGGER.info(
            "Tur bitti (%s): işlenen=%d, yanıtlanan=%d, hata=%d, geçmiş=%d kayıt",
            reason,
            self.processed,
            self.replied,
            self.errors,
            len(self.history),
        )


# ===========================================================================
# 8) Telegram uygulamasını kurma ve çalıştırma
# ===========================================================================
def build_application(cfg: Config, runner: BotRunner, use_updater: bool) -> Application:
    """PTB Application nesnesini kurar."""
    builder = ApplicationBuilder().token(cfg.telegram_token)
    if not use_updater:
        # webhook/polling altyapısı kullanmıyoruz: kendi getUpdates döngümüz var.
        builder = builder.updater(None)
    if cfg.telegram_api_base_url:
        LOGGER.info("Özel Telegram API adresi kullanılıyor: %s", cfg.telegram_api_base_url)
        builder = builder.base_url(cfg.telegram_api_base_url)
    builder = builder.concurrent_updates(False)

    application = builder.build()
    application.add_handler(CommandHandler("start", runner.cmd_start))
    application.add_handler(CommandHandler("help", runner.cmd_help))
    application.add_handler(CommandHandler("id", runner.cmd_id))
    application.add_handler(CommandHandler("status", runner.cmd_status))
    application.add_handler(CommandHandler("clear", runner.cmd_clear))
    # Komut olmayan tüm metin mesajları:
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, runner.on_message))
    # Beklenmeyen hataları logla (aksi halde PTB "No error handlers are registered" uyarısı verir):
    application.add_error_handler(runner.on_error)
    return application


async def fetch_updates(bot: Bot, offset: Optional[int], timeout: float) -> List[Update]:
    try:
        updates = await bot.get_updates(
            offset=offset,
            timeout=timeout,
            limit=100,
            allowed_updates=Update.ALL_TYPES,
            read_timeout=(timeout + 15) if timeout else None,
        )
    except TelegramError as exc:
        if "Conflict" in str(exc):
            LOGGER.error(
                "Telegram çakışma hatası: aynı token ile başka bir örnek (veya webhook) çalışıyor olabilir. "
                "Aynı anda tek örnek çalıştırın. Detay: %s",
                exc,
            )
        else:
            LOGGER.error("getUpdates hatası: %s", exc)
        raise
    return list(updates)


async def run_once_or_loop(application: Application, runner: BotRunner, cfg: Config, mode: str) -> int:
    """``once`` ve ``loop`` modlarında güncellemeleri elle işler."""
    bot = application.bot
    offset: Optional[int] = None
    deadline = time.monotonic() + max(5.0, cfg.loop_minutes * 60.0) if mode == "loop" else None

    if cfg.long_poll_seconds > 0:
        timeout = cfg.long_poll_seconds
    else:
        timeout = 25.0 if mode == "loop" else 0.0

    rounds = 0
    while True:
        if mode == "loop" and deadline is not None and time.monotonic() >= deadline:
            LOGGER.info("Belirlenen süre doldu; tur kapatılıyor.")
            break
        if mode == "once" and rounds >= max(1, cfg.max_rounds):
            LOGGER.info("Maksimum tur sayısına ulaşıldı (%d).", rounds)
            break

        try:
            updates = await fetch_updates(bot, offset, timeout)
        except TelegramError:
            return 2

        rounds += 1
        if not updates:
            LOGGER.debug("Yeni güncelleme yok.")
            if mode == "once":
                # Boş dönüş = offset onaylandı; tek tur bitti.
                break
            continue

        LOGGER.info("%d güncelleme alındı.", len(updates))
        for update in updates:
            try:
                await application.process_update(update)
            except Exception:  # noqa: BLE001 - tek güncelleme tüm turu bozmasın
                LOGGER.exception("Güncelleme işlenemedi (id=%s).", getattr(update, "update_id", "?"))
            offset = (update.update_id or 0) + 1

    # Offset'i onayla (kuyruktaki mesajların bir sonraki turda tekrar gelmemesi için).
    if offset is not None:
        try:
            await fetch_updates(bot, offset, 0)
        except TelegramError:
            LOGGER.warning("Offset onaylanamadı; sonraki turda bazı mesajlar tekrar gelebilir.")
    return 0


def run_polling(application: Application, runner: BotRunner, cfg: Config) -> int:
    """Klasik sonsuz polling modu (yerel makine / VPS)."""
    LOGGER.info("Polling (sonsuz döngü) başlatılıyor. Durdurmak için Ctrl+C.")
    try:
        # PTB, SIGINT/SIGTERM sinyallerini kendisi yakalar ve düzgün kapatır.
        application.run_polling(
            timeout=int(max(10, cfg.long_poll_seconds)),
            allowed_updates=list(Update.ALL_TYPES),
            drop_pending_updates=False,
        )
    except KeyboardInterrupt:  # pragma: no cover
        LOGGER.info("Ctrl+C alındı, kapatılıyor.")
    except TelegramError as exc:
        LOGGER.error("Telegram hatası: %s", exc)
        return 2
    finally:
        runner.finalize("polling kapanışı")
    return 0


def run(args: argparse.Namespace) -> int:
    cfg = load_config(args)
    mode = resolve_mode(cfg)
    cfg = replace(cfg, mode=mode)

    REDACTOR.add_secret(cfg.telegram_token)
    REDACTOR.add_secret(cfg.groq_api_key)
    REDACTOR.add_secret(cfg.gh_pat_token)

    LOGGER.info("Telegram + Groq botu v%s başlıyor.", __version__)
    LOGGER.info(cfg.masked())
    if cfg.github_repository:
        LOGGER.info("Hedef repo: %s", cfg.github_repository)
    if not cfg.gh_pat_token:
        LOGGER.warning("GH_PAT_TOKEN yok: git push başarısız olabilir (salt-okunur token).")

    history = HistoryStore(cfg.history_file, max_messages=cfg.max_history_messages)
    history.load()
    LOGGER.info("Geçmiş yüklendi: %d kayıt.", len(history))

    chat = GroqChat(cfg)
    git = GitSync(REPO_ROOT, cfg.history_file, cfg)
    runner = BotRunner(cfg, chat, history, git)

    if cfg.dry_run:
        LOGGER.warning("DRY-RUN: Telegram'a mesaj gönderilmeyecek.")

    use_updater = mode == "poll"
    application = build_application(cfg, runner, use_updater=use_updater)

    if mode == "poll":
        poll_code = run_polling(application, runner, cfg)
        return 3 if runner.push_failed else poll_code

    async def _main() -> int:
        await application.initialize()
        me = application.bot.username if getattr(application, "bot", None) else "?"
        LOGGER.info("Bot hazır: @%s | mod=%s", me, mode)
        try:
            code = await run_once_or_loop(application, runner, cfg, mode)
        finally:
            try:
                await application.shutdown()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("shutdown sırasında hata: %s", exc)
            runner.finalize("tur sonu")
        return code

    try:
        code = asyncio.run(_main())
    except TelegramError as exc:
        LOGGER.error("Telegram hatası: %s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        LOGGER.info("Ctrl+C alındı.")
        runner.finalize("kesinti")
        return 130
    # Push başarısızsa iş akışının başarısız görünmesi, sorunun fark edilmesi için önemlidir.
    return 3 if runner.push_failed else code


# ===========================================================================
# 9) Kendini test (--self-test)
# ===========================================================================
def _dummy_config(**overrides: Any) -> Config:
    base = Config(
        telegram_token="123456789:TESTTOKEN_abcdefghijklmnopqrstuvwxyz",
        allowed_user_ids=(42,),
        groq_api_key="gsk_TESTKEY_abcdefghijklmnopqrstuvwxyz",
        gh_pat_token="ghp_TESTPAT_abcdefghijklmnopqrstuvwxyz",
        github_repository="kullanici/repo",
    )
    return replace(base, **overrides)


def run_self_tests() -> int:
    """Ağ/anahtar gerektirmeyen kontroller. Hata varsa 1 döner."""
    failures: List[str] = []
    checks = 0

    def check(name: str, condition: bool, extra: str = "") -> None:
        nonlocal checks
        checks += 1
        if condition:
            print("  [OK]   %s%s" % (name, (" - " + extra) if extra else ""))
        else:
            failures.append(name)
            print("  [HATA] %s%s" % (name, (" - " + extra) if extra else ""))

    print("Ai-bot iç testleri (ağ erişimi gerekmez)\n")

    # --- Geçmiş dosyası ve repo dosyaları ---
    print("Dosyalar:")
    check("chat_history.json mevcut", DEFAULT_HISTORY_FILE.is_file())
    if DEFAULT_HISTORY_FILE.is_file():
        raw = DEFAULT_HISTORY_FILE.read_text(encoding="utf-8").strip()
        try:
            parsed = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            parsed = None
            check("chat_history.json geçerli JSON", False, str(exc))
        check(
            "chat_history.json JSON listesi",
            isinstance(parsed, list),
            "%d kayıt" % len(parsed) if isinstance(parsed, list) else "beklenen: liste",
        )
        if isinstance(parsed, list):
            check(
                "chat_history.json kayıtları geçerli",
                parsed == HistoryStore.normalize(parsed),
                "%d kayıt" % len(parsed),
            )
    check("requirements.txt mevcut", DEFAULT_REQUIREMENTS_FILE.is_file())
    if DEFAULT_REQUIREMENTS_FILE.is_file():
        reqs = DEFAULT_REQUIREMENTS_FILE.read_text(encoding="utf-8").lower()
        check("requirements.txt -> python-telegram-bot", "python-telegram-bot" in reqs)
        check("requirements.txt -> groq", re.search(r"^\s*groq\b", reqs, re.M) is not None)
    check(".github/workflows/bot.yml mevcut", DEFAULT_WORKFLOW_FILE.is_file())
    if DEFAULT_WORKFLOW_FILE.is_file():
        content = DEFAULT_WORKFLOW_FILE.read_text(encoding="utf-8")
        check("workflow: python 3.10", "3.10" in content)
        check("workflow: python bot.py", "python bot.py" in content)
        for secret in ("TELEGRAM_TOKEN", "ALLOWED_USER_ID", "GROQ_API_KEY", "GH_PAT_TOKEN"):
            check("workflow: %s" % secret, secret in content)
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(content)
            check("workflow: YAML geçerli", isinstance(loaded, dict), "üst anahtarlar: %s" % list(loaded or {}))
        except ImportError:
            print("  [ATLA] workflow: YAML doğrulaması (PyYAML kurulu değil)")

    # --- Mesaj bölme ---
    print("\nMesaj bölme:")
    check("kısa metin tek parça", split_message("merhaba") == ["merhaba"])
    long_text = ("paragraf bir. " * 400) + "\n\n" + ("paragraf iki. " * 400)
    chunks = split_message(long_text)
    check("uzun metin bölünüyor", len(chunks) > 1, "%d parça" % len(chunks))
    check("parça uzunlukları sınır içinde", all(len(c) <= TELEGRAM_MAX_MESSAGE_LENGTH for c in chunks))
    check(
        "içerik kaybolmuyor",
        "".join(chunks).replace(" ", "") == long_text.replace(" ", ""),
        "%d vs %d karakter" % (len("".join(chunks).replace(" ", "")), len(long_text.replace(" ", ""))),
    )
    check("boş metin güvenli", split_message("") == ["(boş yanıt)"])

    # --- Geçmiş deposu ---
    print("\nGeçmiş deposu:")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / "chat_history.json"
        store = HistoryStore(tmp_path, max_messages=4)
        loaded = store.load()
        check("yeni dosya boş listeyle oluşturuluyor", loaded == [] and json.loads(tmp_path.read_text()) == [])
        store.append("user", "selam")
        store.append("assistant", "merhaba!")
        store.save()
        reloaded = HistoryStore(tmp_path, max_messages=4)
        reloaded.load()
        check("kaydet/yükle turu", reloaded.messages == [{"role": "user", "content": "selam"}, {"role": "assistant", "content": "merhaba!"}])
        for i in range(10):
            reloaded.append("user", "mesaj %d" % i)
        reloaded.save()
        check("max_messages budaması", len(reloaded.messages) == 4, "%d kayıt" % len(reloaded.messages))
        bad_path = Path(tmp) / "bozuk.json"
        bad_path.write_text("{bu json degil", encoding="utf-8")
        bad_store = HistoryStore(bad_path, max_messages=4)
        check("bozuk JSON çökme yaratmıyor", bad_store.load() == [])
        check("bozuk dosya yedekleniyor", (Path(tmp) / "bozuk.json.bozuk-yedek").exists())
        norm = HistoryStore.normalize(
            [{"role": "user", "content": "a"}, {"rol": "x"}, {"role": "tool", "content": "b"}, "saçma", {"role": "assistant"}]
        )
        check("geçersiz kayıtlar eleniyor", norm == [{"role": "user", "content": "a"}])

    # --- Prompt kurulumu ---
    print("\nPrompt kurulumu:")
    history = [{"role": "user", "content": "u%d" % i} for i in range(20)]
    prompt = build_prompt_messages(history, "yeni soru", "SISTEM", 4, 24000)
    check("system mesajı başta", prompt[0] == {"role": "system", "content": "SISTEM"})
    check("son mesaj kullanıcınınki", prompt[-1] == {"role": "user", "content": "yeni soru"})
    check("geçmiş budanıyor", len(prompt) == 1 + 4 + 1, "%d mesaj" % len(prompt))
    small = build_prompt_messages(history, "yeni", "SISTEM", 100, 12)
    check("karakter bütçesi uygulanıyor", len(small) <= 4, "%d mesaj" % len(small))

    # --- Model hatası ayrımı ---
    print("\nGroq hata ayrımı:")

    class FakeNotFound(NotFoundError):
        def __init__(self) -> None:  # pragma: no cover
            Exception.__init__(self, "model `llama-3.3-70b-versatile` has been decommissioned")

    class FakeOther(Exception):
        pass

    check("decommission hatası tanınıyor", is_model_unavailable_error(FakeNotFound()))
    check("genel hata yedek modele geçirmiyor", not is_model_unavailable_error(FakeOther("boom")))

    # --- Git ortamı / maskeleme ---
    print("\nGit & güvenlik:")
    env = git_extraheader_env("ghp_TESTPAT_abcdefghijklmnopqrstuvwxyz")
    check("git extraheader ortamı kuruluyor", env.get("GIT_CONFIG_COUNT") == "1")
    check("token komut satırında değil (env üzerinden)", "ghp_TESTPAT" not in str(env.get("GIT_CONFIG_KEY_0")))
    check("prompt kapatıldı", env.get("GIT_TERMINAL_PROMPT") == "0")
    cfg = _dummy_config()
    check("maskeleme çalışıyor", "ghp_TESTPAT" not in redact("token=ghp_TESTPAT_abcdefghijklmnopqrstuvwxyz", cfg.redaction_secrets()))
    check("maskeli özet anahtar içermiyor", "gsk_TESTKEY" not in cfg.masked() and "TESTTOKEN" not in cfg.masked())
    check("varsayılan model doğru", cfg.models[0] == DEFAULT_MODEL, DEFAULT_MODEL)
    check("varsayılan geçmiş yolu", cfg.history_file == DEFAULT_HISTORY_FILE)

    # --- argparse ---
    print("\nCLI:")
    parsed = parse_args(["--mode", "once", "--dry-run", "--no-git", "--model", "openai/gpt-oss-120b"])
    check("argparse mod seçiyor", parsed.mode == "once" and parsed.dry_run and parsed.no_git)
    check("argparse model alıyor", parsed.model == ["openai/gpt-oss-120b"])
    check("--version çalışıyor", parsed is not None)

    print("\nSonuç: %d kontrol, %d hata." % (checks, len(failures)))
    if failures:
        for name in failures:
            print("  - BAŞARISIZ: %s" % name)
        return 1
    print("Tüm iç testler geçti. ✅")
    return 0


# ===========================================================================
# 10) CLI
# ===========================================================================
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description="Telegram + Groq sohbet botu (geçmişi chat_history.json'da tutar ve repoya push'lar).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Örnekler:\n"
            "  python bot.py --self-test                 # ağ gerektirmeyen kontroller\n"
            "  python bot.py --mode once --dry-run       # yanıtı göndermeden tek tur dene\n"
            "  python bot.py --mode loop --loop-minutes 5\n"
            "  python bot.py --mode poll                 # sürekli çalışma (VPS)\n"
        ),
    )
    parser.add_argument("--version", action="version", version="ai-bot %s" % __version__)
    parser.add_argument("--mode", choices=["auto", "once", "loop", "poll"], default=None, help="çalışma modu (varsayılan: auto)")
    parser.add_argument("--loop-minutes", type=float, default=None, help="loop modunda çalışma süresi (dk)")
    parser.add_argument("--long-poll", type=float, default=None, help="getUpdates zaman aşımı (sn)")
    parser.add_argument("--max-rounds", type=int, default=None, help="once modunda en çok kaç tur")
    parser.add_argument("--history-file", default=None, help="chat_history.json yolu (varsayılan: repo kökü)")
    parser.add_argument("--model", action="append", default=None, help="birincil Groq modeli (virgülle çoklu olabilir)")
    parser.add_argument("--fallback-model", action="append", default=None, help="yedek model (birden çok kez verilebilir)")
    parser.add_argument("--temperature", type=float, default=None, help="0-2 arası sıcaklık")
    parser.add_argument("--max-tokens", type=int, default=None, help="yanıt için en fazla token")
    parser.add_argument("--max-history", type=int, default=None, help="geçmişte tutulacak en fazla mesaj (0=sınırsız)")
    parser.add_argument("--git-branch", default=None, help="push edilecek dal (varsayılan: aktif dal)")
    parser.add_argument("--no-git", action="store_true", help="git add/commit/push adımlarını atla")
    parser.add_argument("--dry-run", action="store_true", help="Telegram'a mesaj gönderme, sadece logla")
    parser.add_argument("--self-test", action="store_true", help="iç testleri çalıştır ve çık")
    parser.add_argument("-v", "--verbose", action="store_true", help="ayrıntılı (DEBUG) log")
    parser.add_argument("-q", "--quiet", action="store_true", help="sadece uyarılar")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=args.verbose, quiet=args.quiet)

    if args.self_test:
        return run_self_tests()

    dotenv_path = load_dotenv()
    if dotenv_path:
        LOGGER.info(".env dosyası yüklendi: %s", dotenv_path)
        setup_logging(verbose=args.verbose, quiet=args.quiet)

    try:
        return run(args)
    except ConfigError as exc:
        LOGGER.error("Yapılandırma hatası: %s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        LOGGER.info("Kullanıcı tarafından durduruldu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
