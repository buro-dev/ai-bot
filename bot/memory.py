"""Kalıcı hafıza (chat_history.json) ve onu repoya taşıyan git senkronu.

Tek dosyada üç bölüm tutulur (format v2):

* ``messages``  : sohbet geçmişi (role/content)
* ``memories``  : kullanıcının kalıcı olarak hatırlanmasını istediği notlar
* ``reminders`` : zamanlanmış hatırlatıcılar (yapıldıkça işaretlenir)

Render'ın ücretsiz katmanında dosya sistemi geçicidir; bu dosya her
değişiklikten sonra ``GIT_MEMORY_BRANCH`` (varsayılan: ``memory``) dalına
push edilir ve bot başlarken o daldan geri yüklenir. Böylece deploy'dan
sonra bile hafıza kalır. Hafıza dalı Render'a bağlanmadığı için push'lar
yeniden derleme/dağıtım tetiklemez.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import Config, ConfigError, redact

LOGGER = logging.getLogger("ai_bot")

GIT_TIMEOUT = 120


# ===========================================================================
# Hafıza deposu
# ===========================================================================
class MemoryStore:
    """``chat_history.json`` dosyasını güvenli şekilde okur/yazar."""

    VALID_ROLES = {"user", "assistant"}
    NOTE_MAX_LEN = 500

    def __init__(
        self,
        path: Path,
        max_messages: int = 40,
        max_memories: int = 50,
        dry_run: bool = False,
    ) -> None:
        self.path = Path(path)
        self.max_messages = max_messages
        self.max_memories = max_memories
        self.dry_run = dry_run
        self._messages: List[Dict[str, str]] = []
        self._memories: List[str] = []
        self._reminders: List[Dict[str, Any]] = []
        self._dirty = False

    # -- okuma -------------------------------------------------------------
    @staticmethod
    def _normalize_messages(raw: Any) -> List[Dict[str, str]]:
        messages: List[Dict[str, str]] = []
        if not isinstance(raw, list):
            return messages
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            content = item.get("content")
            if role not in MemoryStore.VALID_ROLES or not isinstance(content, str) or not content.strip():
                continue
            messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _normalize_reminders(raw: Any) -> List[Dict[str, Any]]:
        reminders: List[Dict[str, Any]] = []
        if not isinstance(raw, list):
            return reminders
        for item in raw:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id", "")).strip()
            fire_at = str(item.get("fire_at", "")).strip()
            message = str(item.get("message", "")).strip()
            if not (rid and fire_at and message):
                continue
            reminders.append(
                {"id": rid, "fire_at": fire_at, "message": message, "done": bool(item.get("done", False))}
            )
        return reminders

    def load(self) -> None:
        if not self.path.exists():
            LOGGER.info("Hafıza dosyası yok, oluşturuluyor: %s", self.path)
            self._dirty = True
            self.save()
            return

        try:
            raw_text = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("Hafıza dosyası okunamadı (%s): %s" % (self.path, exc)) from exc

        raw: Any
        if not raw_text:
            raw = []
        else:
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                if self.dry_run:
                    LOGGER.warning("[DRY-RUN] Hafıza dosyası bozuk JSON (%s); boş hafıza kullanılıyor.", exc)
                    return
                backup = self.path.with_suffix(self.path.suffix + ".bozuk-yedek")
                try:
                    shutil.copy2(self.path, backup)
                    LOGGER.warning("Hafıza dosyası bozuk JSON (%s); yedek: %s", exc, backup)
                except OSError:
                    LOGGER.warning("Hafıza dosyası bozuk JSON (%s)", exc)
                raw = []

        # Uyum: v1 format (sadece mesaj listesi) veya eski dict format.
        if isinstance(raw, list):
            messages = self._normalize_messages(raw)
            memories: List[str] = []
            reminders: List[Dict[str, Any]] = []
        elif isinstance(raw, dict):
            messages = self._normalize_messages(raw.get("messages"))
            memories = [
                str(m).strip()[: self.NOTE_MAX_LEN]
                for m in (raw.get("memories") or [])
                if str(m).strip()
            ]
            reminders = self._normalize_reminders(raw.get("reminders"))
        else:
            messages, memories, reminders = [], [], []

        self._messages = messages
        self._memories = memories[-self.max_memories :] if self.max_memories else memories
        self._reminders = reminders
        self._dirty = True
        self.save()
        LOGGER.info(
            "Hafıza yüklendi: %d mesaj, %d not, %d bekleyen hatırlatıcı",
            len(self._messages),
            len(self._memories),
            len(self.pending_reminders()),
        )

    def save(self) -> None:
        """Dosyayı atomik olarak yazar (yarım dosya kalma riski yok)."""
        if self.dry_run:
            LOGGER.info("[DRY-RUN] Hafıza dosyasına yazılmadı: %s", self.path)
            self._dirty = False
            return
        if self.max_messages and self.max_messages > 0:
            self._messages = self._messages[-self.max_messages :]
        if self.max_memories and self.max_memories > 0:
            self._memories = self._memories[-self.max_memories :]
        # Bitmiş hatırlatıcıları daire içinde tut (dosya şişmesin).
        if len(self._reminders) > 100:
            done = [r for r in self._reminders if r.get("done")]
            self._reminders = [r for r in self._reminders if not r.get("done")] + done[-20:]
        payload = json.dumps(
            {"version": 2, "messages": self._messages, "memories": self._memories, "reminders": self._reminders},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix="." + self.path.name + ".")
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
        LOGGER.debug(
            "Hafıza kaydedildi: %s (%d mesaj, %d not)", self.path, len(self._messages), len(self._memories)
        )

    # -- sohbet geçmişi ------------------------------------------------------
    @property
    def messages(self) -> List[Dict[str, str]]:
        return self._messages

    @property
    def dirty(self) -> bool:
        return self._dirty

    def append_message(self, role: str, content: str) -> None:
        content = (content or "").strip()
        if role in self.VALID_ROLES and content:
            self._messages.append({"role": role, "content": content})
            self._dirty = True

    def clear_messages(self) -> None:
        self._messages = []
        self._dirty = True
        self.save()

    # -- kalıcı notlar ---------------------------------------------------------
    @property
    def memories(self) -> List[str]:
        return self._memories

    def add_memory(self, note: str) -> bool:
        """Not ekler; zaten var ise False döner."""
        note = (note or "").strip()[: self.NOTE_MAX_LEN]
        if not note:
            return False
        if note.lower() in {m.lower() for m in self._memories}:
            return False
        self._memories.append(note)
        self._dirty = True
        return True

    def remove_memory(self, index: int) -> Optional[str]:
        """1 tabanlı sıra numarasıyla not siler; silinen notu döndürür."""
        if 1 <= index <= len(self._memories):
            removed = self._memories.pop(index - 1)
            self._dirty = True
            return removed
        return None

    # -- hatırlatıcılar ---------------------------------------------------------
    @property
    def reminders(self) -> List[Dict[str, Any]]:
        return self._reminders

    def pending_reminders(self) -> List[Dict[str, Any]]:
        return [r for r in self._reminders if not r.get("done")]

    def add_reminder(self, rid: str, fire_at: str, message: str) -> None:
        self._reminders.append({"id": rid, "fire_at": fire_at, "message": message, "done": False})
        self._dirty = True

    def mark_reminder_done(self, rid: str) -> None:
        for rem in self._reminders:
            if rem.get("id") == rid:
                rem["done"] = True
                self._dirty = True
                return

    def __len__(self) -> int:
        return len(self._messages)


# ===========================================================================
# Git senkronu
# ===========================================================================
@dataclass
class GitResult:
    ok: bool
    pushed: bool
    detail: str = ""

    def __bool__(self) -> bool:  # pragma: no cover
        return self.ok


def _git_env(token: Optional[str]) -> Dict[str, str]:
    """Kimlik bilgisini komut satırına yazmadan git'e aktarır (GIT_CONFIG_*)."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # asla kullanıcı adı/şifre sorma
            "GIT_ASKPASS": shutil.which("true") or "/bin/true",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    if token:
        # x-access-token, GitHub PAT için geçerli kullanıcı adıdır.
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
    """Hafıza dosyasını hafıza dalına push'lar / başlangıçta geri yükler."""

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

    def _relative_file_path(self) -> Optional[str]:
        try:
            return str(self.file_path.resolve().relative_to(self.repo_dir.resolve()))
        except ValueError:
            return None

    # -- geri yükleme (başlangıç) -------------------------------------------
    def restore(self) -> GitResult:
        """Hafıza dosyasını uzak hafıza dalından geri yükler.

        Dal yoksa (ilk kurulum) veya çekilemiyorsa hata vermeden geri döner;
        bot boş (veya yerel) hafızayla çalışmaya devam eder.
        """
        if self.cfg.dry_run:
            return GitResult(True, False, "dry-run: atlandı")
        if not self.cfg.git_push_enabled:
            return GitResult(False, False, "devre dışı")
        if not self.is_repo():
            return GitResult(False, False, "git deposu yok")
        rel_path = self._relative_file_path()
        if rel_path is None:
            return GitResult(False, False, "dosya repo dışında")

        branch = self.cfg.git_memory_branch
        ref = "refs/remotes/%s/%s" % (self.cfg.git_remote, branch)
        token = self.cfg.gh_pat_token
        # "+" prefix: dal bağımsız tarihli olsa da ref güncellensin.
        fetch = self._run(
            ["git", "fetch", "--quiet", self.cfg.git_remote, "+%s:%s" % (branch, ref)], token=token
        )
        if fetch.returncode != 0:
            detail = redact(fetch.stderr.strip(), self.secrets)[:200]
            LOGGER.info("Hafıza dalı alınamadı (%s); yeni başlangıç kabul edildi. %s", branch, detail)
            return GitResult(False, False, "hafıza dalı yok/alınamadı")

        show = self._run(["git", "show", "%s:%s" % (ref, rel_path)], token=token)
        if show.returncode != 0:
            return GitResult(False, False, "hafıza dosyası dalda bulunamadı")

        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.file_path.parent), prefix="." + self.file_path.name + ".")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(show.stdout)
            os.replace(tmp, self.file_path)
        except OSError as exc:
            return GitResult(False, False, "hafıza dosyası yazılamadı: %s" % exc)
        return GitResult(True, False, "%s dalından geri yüklendi" % branch)

    # -- commit + push ---------------------------------------------------------
    def commit_and_push(self, commit_message: str) -> GitResult:
        """Hafıza dosyasındaki değişikliği commit'leyip hafıza dalına push'lar.

        Tek yazar (bot) olduğu için hafıza dalına ``--force`` push güvenlidir.
        """
        if self.cfg.dry_run:
            LOGGER.info("[DRY-RUN] git commit/push atlandı; repoya dokunulmadı.")
            return GitResult(True, False, "dry-run: atlandı")
        if not self.cfg.git_push_enabled:
            return GitResult(False, False, "devre dışı")
        if not self.is_repo():
            LOGGER.warning("Git deposu bulunamadı (%s); push atlanıyor.", self.repo_dir)
            return GitResult(False, False, "git deposu yok")
        rel_path = self._relative_file_path()
        if rel_path is None:
            LOGGER.warning("Hafıza dosyası repo dışında (%s); push atlanıyor.", self.file_path)
            return GitResult(False, False, "dosya repo dışında")

        # 1) Dosyayı sahneye al (yalnızca hafıza dosyası).
        add = self._run(["git", "add", "--", rel_path])
        if add.returncode != 0:
            return GitResult(False, False, "git add başarısız: %s" % redact(add.stderr.strip(), self.secrets))

        # 2) Değişiklik yoksa commit/push yapma.
        staged = self._run(["git", "diff", "--cached", "--quiet", "--", rel_path])
        if staged.returncode == 0:
            return GitResult(True, False, "değişiklik yok")

        # 3) Commit (kimlik bilgileri geçici -c ile, repoya yazılmaz).
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

        # 4) Hafıza dalına push (PAT ile).
        branch = self.cfg.git_memory_branch
        push = self._run(
            ["git", "push", "--force", self.cfg.git_remote, "HEAD:refs/heads/%s" % branch],
            token=self.cfg.gh_pat_token,
        )
        if push.returncode != 0 and self.cfg.gh_pat_token:
            # PAT başarısızsa çevremizdeki kimlik (örn. GITHUB_TOKEN) ile dene.
            checkout_token = os.environ.get("GITHUB_TOKEN")
            if checkout_token and checkout_token != self.cfg.gh_pat_token:
                LOGGER.warning("PAT ile push başarısız; GITHUB_TOKEN ile tekrar denenecek.")
                push = self._run(
                    ["git", "push", "--force", self.cfg.git_remote, "HEAD:refs/heads/%s" % branch],
                    token=checkout_token,
                )
        if push.returncode == 0:
            LOGGER.info("Hafıza push tamam: %s (%s) -> %s", commit_hash, commit_message, branch)
            return GitResult(True, True, "push ok (%s)" % branch)

        stderr = redact(push.stderr.strip(), self.secrets)
        detail = "git push başarısız: %s" % (stderr or "bilinmeyen hata")
        if not self.cfg.gh_pat_token:
            detail += (
                " | İpucu: GH_PAT_TOKEN tanımlı değil. Hafıza kalıcı olsun isterseniz "
                "içerik yazma (contents: write) yetkili bir fine-grained PAT tanımlayın."
            )
        LOGGER.error(detail)
        return GitResult(False, False, detail)
