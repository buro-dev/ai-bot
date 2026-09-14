"""Telegram güncellemelerini işleyen ana akış ve komutlar."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .ai import ChatResult, GroqChat, TOOLS
from .config import Config
from .media import (
    IMAGE_EXTS,
    MIME_BY_EXT,
    TEXT_EXTS,
    data_url,
    decode_text,
    download_bytes,
    ext_of,
    pdf_to_text,
)
from .memory import GitSync, MemoryStore
from .reminders import ReminderScheduler, parse_fire_at
from .web_search import ddg_search, format_results

LOGGER = logging.getLogger("ai_bot")

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def get_timezone(name: str):
    """Saat dilimi bul; yoksa sabit UTC+3 (Türkiye, DST'siz) kullan."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - tzdata eksik olabilir
        LOGGER.warning("Saat dilimi '%s' bulunamadı; sabit UTC+3 (TR) kullanılıyor.", name)
        return timezone(timedelta(hours=3), "UTC+3")


# ===========================================================================
# Metin yardımcıları
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


def user_display(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "bilinmeyen"
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return "%s (@%s, id=%s)" % (name or "-", user.username or "-", user.id)


def build_system_prompt(
    base_prompt: str, now: datetime, memories: Sequence[str], tz_name: str
) -> str:
    """Sistem istemine güncel zaman ve kalıcı notları ekler."""
    parts = [
        base_prompt.strip(),
        "\nGÜNCEL BİLGİ\n- Şu an: %s (%s)" % (now.strftime("%A %d.%m.%Y %H:%M"), tz_name),
    ]
    recent = list(memories)[-15:]
    if recent:
        parts.append("\nKALICI NOTLARIN (kullanıcının hatırlanmasını istediği):\n" + "\n".join("- %s" % m for m in recent))
    else:
        parts.append("\nKALICI NOTLARIN: (henüz yok)")
    return "\n".join(parts)


def _content_len(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    total = 0
    for part in content or []:
        if isinstance(part, dict):
            total += len(part.get("text", "") or "")
    return total


def build_prompt_messages(
    history: Sequence[Dict[str, str]],
    last_user: Any,
    system_prompt: Optional[str],
    max_history_messages: int,
    max_chars: int,
) -> List[Dict[str, Any]]:
    """Groq'a gönderilecek mesaj listesini hazırlar (geçmişi budayarak)."""
    payload: List[Dict[str, Any]] = []
    if system_prompt:
        payload.append({"role": "system", "content": system_prompt})

    tail = [m for m in history if m.get("role") in MemoryStore.VALID_ROLES and m.get("content")]
    if max_history_messages and max_history_messages > 0:
        tail = tail[-max_history_messages:]

    # Karakter bütçesini aşmayacak şekilde sondan başa doğru ekle.
    selected: List[Dict[str, str]] = []
    used = (len(system_prompt) if system_prompt else 0) + _content_len(last_user)
    for message in reversed(tail):
        cost = len(message["content"])
        if used + cost > max_chars and selected:
            break
        used += cost
        selected.append(message)
    selected.reverse()

    payload.extend(selected)
    payload.append({"role": "user", "content": last_user})
    return payload


def parse_remind_args(
    args: Sequence[str], tz, now: Optional[datetime] = None
) -> Optional[Tuple[datetime, str]]:
    """/remind argümanlarını çözer: [yarın] HH:MM <mesaj...>"""
    tokens = list(args or [])
    day = "today"
    if tokens and tokens[0].lower() in ("yarın", "yarin", "tomorrow"):
        day = "tomorrow"
        tokens = tokens[1:]
    if not tokens:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})$", tokens[0])
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    current = now or datetime.now(tz)
    fire_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "tomorrow":
        fire_at += timedelta(days=1)
    elif fire_at <= current:
        fire_at += timedelta(days=1)  # bugün o saat geçtiyse ertesi gün
    message = " ".join(tokens[1:]).strip()
    if not message:
        return None
    return fire_at, message


# ===========================================================================
# BotRunner
# ===========================================================================
class BotRunner:
    """Telegram güncellemelerini işleyen ana sınıf."""

    def __init__(
        self,
        cfg: Config,
        chat: GroqChat,
        memory: MemoryStore,
        git: GitSync,
        scheduler: ReminderScheduler,
        mode: str,
    ) -> None:
        self.cfg = cfg
        self.chat = chat
        self.memory = memory
        self.git = git
        self.scheduler = scheduler
        self.mode = mode
        self.tz = get_timezone(cfg.timezone_name)

        self.application: Optional[Application] = None
        self.bot = None
        self.last_chat_id: Optional[int] = None

        self._lock = asyncio.Lock()
        self._tasks: Set[asyncio.Task] = set()
        self._pushing = False
        self._last_push_at: Optional[float] = None
        self._last_push_detail: str = "-"

        self.started_at = time.monotonic()
        self.processed = 0
        self.replied = 0
        self.errors = 0

    # ---------- yaşam döngüsü ---------------------------------------------
    def uptime(self) -> float:
        return time.monotonic() - self.started_at

    async def post_init(self, application: Application) -> None:
        self.application = application
        self.bot = application.bot
        LOGGER.info("Bot başlatılıyor: mod=%s | %s", self.mode, self.cfg.masked())
        if self.mode == "webhook" and not self.cfg.dry_run:
            await self._setup_webhook()
        self.scheduler.start()
        self._load_pending_reminders()
        if not self.cfg.dry_run:
            try:
                await self.bot.set_my_commands(self._commands())
            except TelegramError as exc:
                LOGGER.debug("set_my_commands başarısız: %s", exc)

    async def post_shutdown(self, _application: Application) -> None:
        LOGGER.info("Kapanış başlatıldı...")
        try:
            self.scheduler.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self._do_push("kapanış")

    async def _setup_webhook(self) -> None:
        base = (self.cfg.public_url or "").rstrip("/")
        url = "%s%s" % (base, self.cfg.webhook_path)
        kwargs: Dict[str, Any] = dict(
            url=url,
            drop_pending_updates=False,
            allowed_updates=Update.ALL_TYPES,
            max_connections=40,
        )
        if self.cfg.webhook_secret:
            kwargs["secret_token"] = self.cfg.webhook_secret
        await self.bot.set_webhook(**kwargs)
        LOGGER.info("Webhook kuruldu: %s", url)

    def _commands(self) -> List[BotCommand]:
        return [
            BotCommand("start", "Merhaba / özellikler"),
            BotCommand("help", "Yardım"),
            BotCommand("search", "İnternetten ara: /search <sorgu>"),
            BotCommand("remember", "Kalıcı not ekle: /remember <not>"),
            BotCommand("memory", "Kalıcı notları listele"),
            BotCommand("forget", "Notu sil: /forget <no>"),
            BotCommand("remind", "Hatırlatıcı: /remind [yarın] 08:00 <mesaj>"),
            BotCommand("clear", "Sohbet geçmişini temizle"),
            BotCommand("status", "Durum bilgisi"),
            BotCommand("id", "Telegram kimliklerim"),
        ]

    # ---------- webhook girişi ----------------------------------------------
    def spawn(self, coro) -> None:
        """Fire-and-forget görevi başlat (GC edilmesin diye set'te tut)."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def spawn_update(self, update: Update) -> None:
        self.spawn(self._handle_update(update))

    async def _handle_update(self, update: Update) -> None:
        if self.application is None:
            return
        try:
            await self.application.process_update(update)
        except Exception:  # noqa: BLE001
            self.errors += 1
            LOGGER.exception("Update işlenirken beklenmeyen hata")

    # ---------- hatırlatıcılar ------------------------------------------------
    def _load_pending_reminders(self) -> None:
        if not self.cfg.reminders_enabled:
            return
        now = datetime.now(self.tz)
        changed = False
        for rem in self.memory.pending_reminders():
            rid = rem.get("id", "")
            fire_raw = rem.get("fire_at", "")
            message = rem.get("message", "")
            fire_at = parse_fire_at(fire_raw, self.tz)
            if not rid or fire_at is None:
                self.memory.mark_reminder_done(rid or "?")
                changed = True
                continue
            if fire_at > now:
                self.scheduler.schedule(rid, fire_at, self._fire_reminder, rid, fire_raw, message)
            else:
                LOGGER.info("Gecikmiş hatırlatıcı gönderilecek: %s (%s)", rid, fire_raw)
                self.spawn(self._send_reminder_now(rid, fire_raw, message, late=True))
                self.memory.mark_reminder_done(rid)
                changed = True
        if changed:
            self.memory.save()
            self._schedule_push("hatırlatıcılar")

    async def _fire_reminder(self, rid: str, fire_raw: str, message: str) -> None:
        LOGGER.info("Hatırlatıcı tetiklendi: %s", rid)
        await self._send_reminder_now(rid, fire_raw, message, late=False)

    async def _send_reminder_now(self, rid: str, fire_raw: str, message: str, late: bool) -> None:
        if self.bot is None:
            return
        chat_id = self.last_chat_id or (self.cfg.allowed_user_ids[0] if self.cfg.allowed_user_ids else None)
        if not chat_id:
            LOGGER.warning("Hatırlatıcı gönderilecek sohbet yok: %s", rid)
            return
        prefix = "⏰ (gecikmiş) Hatırlatma: " if late else "⏰ Hatırlatma: "
        try:
            await self.bot.send_message(chat_id, "%s%s" % (prefix, message))
            LOGGER.info("Hatırlatıcı gönderildi: %s", rid)
        except TelegramError as exc:
            LOGGER.error("Hatırlatıcı gönderilemedi (%s): %s", rid, exc)
            return
        self.memory.mark_reminder_done(rid)
        self.memory.save()
        self._schedule_push("hatırlatıcı")

    # ---------- yetki kontrolü ------------------------------------------------
    async def _guard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user = update.effective_user
        if user is None or user.id not in self.cfg.allowed_user_ids:
            if user is not None:
                LOGGER.info(
                    "İzin verilmeyen kullanıcı sessizce yok sayıldı: id=%s (@%s)", user.id, user.username
                )
            return False
        return True

    # ---------- gönderim -------------------------------------------------------
    async def send(
        self,
        bot,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
    ) -> None:
        """Markdown ile gönder; çözümlenemezse düz metne düş."""
        for index, chunk in enumerate(split_message(text)):
            if self.cfg.dry_run:
                LOGGER.info("[DRY-RUN] Telegram'a gönderilecek yanıt: %s", chunk)
                continue
            sent = False
            for parse_mode in (ParseMode.MARKDOWN, None):
                try:
                    kwargs: Dict[str, Any] = {}
                    if parse_mode:
                        kwargs["parse_mode"] = parse_mode
                    if index == 0 and reply_to:
                        kwargs["reply_to_message_id"] = reply_to
                    await bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
                    sent = True
                    break
                except TelegramError as exc:
                    if parse_mode is not None and "parse" in str(exc).lower():
                        LOGGER.debug("Markdown çözümlenemedi; düz metin deneniyor.")
                        continue
                    if index == 0 and reply_to:
                        LOGGER.warning("Yanıt olarak gönderilemedi (%s); normal mesaj deneniyor.", exc)
                        try:
                            await bot.send_message(chat_id=chat_id, text=chunk)
                            sent = True
                            break
                        except TelegramError as exc2:
                            LOGGER.error("Telegram gönderim hatası: %s", exc2)
                            return
                    LOGGER.error("Telegram gönderim hatası: %s", exc)
                    return
            if not sent:
                return

    def _start_typing(self, chat_id: int) -> asyncio.Task:
        async def _loop() -> None:
            while True:
                if self.bot is None:
                    return
                try:
                    await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                except TelegramError:
                    return
                await asyncio.sleep(5)

        return asyncio.ensure_future(_loop())

    # ---------- git push (hafıza senkronu) ---------------------------------------
    def _schedule_push(self, note: str = "") -> None:
        """Hafıza değişikliğini (throttle ile) arka planda repoya push'lar."""
        if self.cfg.dry_run or not self.cfg.git_push_enabled:
            return
        now = time.monotonic()
        wait = 0.0
        if self._last_push_at is not None:
            wait = max(0.0, self.cfg.git_push_min_interval - (now - self._last_push_at))
        if wait > 0.05:

            def _delayed() -> None:
                time.sleep(wait + 0.25)
                self._do_push(note)

            threading.Thread(target=_delayed, daemon=True, name="git-push").start()
        else:
            self._do_push(note)

    def _do_push(self, note: str) -> None:
        for _attempt in range(20):
            if not self._pushing:
                self._pushing = True
                break
            time.sleep(1.0)
        else:
            LOGGER.warning("Git push kuyruğu dolu; değişiklik bir sonraki push'a kaldı.")
            return
        try:
            stamp = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M %Z")
            message = "%s: %s | %d mesaj, %d not%s" % (
                self.cfg.commit_message_prefix,
                stamp,
                len(self.memory),
                len(self.memory.memories),
                ", " + note if note else "",
            )
            result = self.git.commit_and_push(message)
            self._last_push_detail = result.detail
            if result.pushed:
                self._last_push_at = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Git push hatası: %s", exc)
            self._last_push_detail = str(exc)
        finally:
            self._pushing = False

    def _push_now(self, note: str) -> None:
        if self.cfg.dry_run or not self.cfg.git_push_enabled:
            return
        self._do_push(note)

    # ---------- ana işlem akışı -----------------------------------------------------
    def _active_tools(self) -> Optional[List[Dict[str, Any]]]:
        if not self.chat.tools_supported():
            return None
        wanted: Set[str] = {"save_memory"}
        if self.cfg.search_enabled:
            wanted.add("web_search")
        if self.cfg.reminders_enabled:
            wanted.add("set_reminder")
        tools = [t for t in TOOLS if t["function"]["name"] in wanted]
        return tools or None

    async def _execute_tool(self, tool_call) -> str:
        name = tool_call.name
        if name == "web_search":
            if not self.cfg.search_enabled:
                return "Arama devre dışı (SEARCH_ENABLED=0)."
            query = str(tool_call.args.get("query", "")).strip()[:300]
            if not query:
                return "Arama sorgusu boş."
            results, err = await asyncio.to_thread(
                ddg_search, query, self.cfg.search_max_results, self.cfg.search_region
            )
            if err:
                return err
            if not results:
                return "Arama sonucu bulunamadı."
            return "İnternet araması sonuçları ('%s'):\n\n%s" % (query, format_results(results))

        if name == "save_memory":
            note = str(tool_call.args.get("note", "")).strip()
            if not note:
                return "Not boş."
            added = self.memory.add_memory(note)
            self.memory.save()
            self._schedule_push("not")
            return "Kalıcı not kaydedildi." if added else "Bu not zaten hafızanda."

        if name == "set_reminder":
            if not self.cfg.reminders_enabled:
                return "Hatırlatıcılar devre dışı (REMINDERS_ENABLED=0)."
            fire_raw = str(tool_call.args.get("fire_at", "")).strip()
            message = str(tool_call.args.get("message", "")).strip()[:500]
            if not message:
                return "Hatırlatma metni boş."
            fire_at = parse_fire_at(fire_raw, self.tz)
            if fire_at is None:
                return "Zaman biçimi geçersiz: %r. 'YYYY-MM-DD HH:MM' biçimini kullan." % fire_raw
            if fire_at <= datetime.now(self.tz):
                return "Bu zaman geçmişte; hatırlatıcı kurulamadı."
            rid = uuid.uuid4().hex[:10]
            if not self.scheduler.schedule(rid, fire_at, self._fire_reminder, rid, fire_at.strftime("%Y-%m-%d %H:%M"), message):
                return "Hatırlatıcı kurulamadı."
            self.memory.add_reminder(rid, fire_at.strftime("%Y-%m-%d %H:%M"), message)
            self.memory.save()
            self._schedule_push("hatırlatıcı")
            return "Hatırlatıcı kuruldu: %s — %s" % (fire_at.strftime("%d.%m.%Y %H:%M"), message)

        return "Bilinmeyen araç: %s" % name

    async def _process(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        user_text: str,
        images: Optional[List[Tuple[bytes, str]]] = None,
        status_text: Optional[str] = None,
    ) -> None:
        """Bir kullanıcı girdisini (metin/medya) AI'ya ulaştırıp yanıtı yollar."""
        message = update.effective_message
        chat_id = update.effective_chat.id
        self.last_chat_id = chat_id
        self.processed += 1
        action_task = self._start_typing(chat_id)
        status_msg = None
        round_no = 0
        try:
            async with self._lock:
                if status_text and not self.cfg.dry_run:
                    try:
                        status_msg = await context.bot.send_message(chat_id, status_text)
                    except TelegramError:
                        status_msg = None

                now = datetime.now(self.tz)
                system_prompt = build_system_prompt(
                    self.cfg.system_prompt, now, self.memory.memories, self.cfg.timezone_name
                )

                parts: List[Dict[str, Any]] = []
                for data, mime in (images or [])[:4]:
                    parts.append({"type": "image_url", "image_url": {"url": data_url(data, mime)}})
                if user_text:
                    parts.append({"type": "text", "text": user_text})
                if not parts:
                    parts.append({"type": "text", "text": "(medya)"})
                last_user: Any = parts if (len(parts) > 1 or parts[0]["type"] != "text") else user_text

                prompt = build_prompt_messages(
                    history=self.memory.messages,
                    last_user=last_user,
                    system_prompt=system_prompt,
                    max_history_messages=self.cfg.max_history_messages,
                    max_chars=self.cfg.max_prompt_chars,
                )
                LOGGER.debug("Groq'a gönderilen mesaj sayısı: %d", len(prompt))

                tools = self._active_tools()
                try:
                    result: ChatResult = await asyncio.to_thread(self.chat.complete, prompt, tools)
                    while result.tool_calls and round_no < 2:
                        round_no += 1
                        prompt.append(
                            {
                                "role": "assistant",
                                "content": result.text or None,
                                "tool_calls": [
                                    {
                                        "id": tc.id,
                                        "type": "function",
                                        "function": {
                                            "name": tc.name,
                                            "arguments": json.dumps(tc.args, ensure_ascii=False),
                                        },
                                    }
                                    for tc in result.tool_calls
                                ],
                            }
                        )
                        for tc in result.tool_calls:
                            response_text = await self._execute_tool(tc)
                            prompt.append({"role": "tool", "tool_call_id": tc.id, "content": response_text})
                        result = await asyncio.to_thread(self.chat.complete, prompt, None)
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    LOGGER.exception("AI isteği başarısız: %s", type(exc).__name__)
                    await self.send(
                        context.bot,
                        chat_id,
                        "Üzgünüm, yanıt üretilemedi. %s" % GroqChat.friendly_error(exc),
                        reply_to=message.message_id if message else None,
                    )
                    return

                await self.send(
                    context.bot, chat_id, result.text, reply_to=message.message_id if message else None
                )
                self.memory.append_message("user", user_text or "[medya gönderdi]")
                self.memory.append_message("assistant", result.text)
                self.memory.save()
                self.replied += 1
                LOGGER.info(
                    "Yanıt gönderildi (model=%s, %d karakter, araç_döngüsü=%d)",
                    result.model,
                    len(result.text),
                    round_no,
                )
                if status_msg is not None:
                    try:
                        await status_msg.delete()
                    except TelegramError:
                        pass
                self._schedule_push()
        finally:
            action_task.cancel()

    # ---------- komutlar -------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        await self._help_message(update, context, greeting=True)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        await self._help_message(update, context)

    async def _help_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, greeting: bool = False) -> None:
        chat_id = update.effective_chat.id
        blocks = []
        if greeting:
            blocks.append("Merhaba! Kişisel asistan botun hazırım. 🤖")
        blocks.append(
            "Ne yapabilirsin:\n"
            "• Herhangi bir şey yaz — cevaplarım\n"
            "• Görsel gönder — analiz ederim\n"
            "• Sesli mesaj at — metne çevirip cevaplarım\n"
            "• PDF / metin / kod dosyası gönder — okur, özetlerim\n"
            '• "internetten ara: ..." yaz ya da /search <sorgu>\n'
            '• "hatırla: ..." yaz ya da /remember <not> — kalıcı not\n'
            '• "yarın 08:00 hatırlat: ..." yaz ya da /remind 08:00 <mesaj>'
        )
        blocks.append(
            "Komutlar:\n"
            "/search <sorgu> — internetten ara\n"
            "/remember <not> — kalıcı not ekle\n"
            "/memory — notları listele\n"
            "/forget <no> — notu sil\n"
            "/remind [yarın] 08:00 <mesaj> — hatırlatıcı kur\n"
            "/clear — sohbet geçmişini temizle (notlar ve hatırlatıcılar kalır)\n"
            "/status — durum | /id — kimliklerin | /help — bu mesaj"
        )
        await self.send(
            context.bot,
            chat_id,
            "\n\n".join(blocks),
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
        uptime = self.uptime()
        uptime_str = "%dg %dh %02dm" % (uptime // 86400, (uptime % 86400) // 3600, (uptime % 3600) // 60)
        lines = [
            "📊 *Durum*",
            "Mod: %s" % self.mode,
            "Model: %s" % self.chat.active_model,
            "Yedek modeller: %s" % (", ".join(m for m in self.cfg.models[1:]) or "-"),
            "Sohbet kaydı: %d | Kalıcı not: %d | Bekleyen hatırlatıcı: %d"
            % (len(self.memory), len(self.memory.memories), len(self.memory.pending_reminders())),
            "Çalışma süresi: %s" % uptime_str,
            "İşlenen mesaj: %d | Yanıt: %d | Hata: %d" % (self.processed, self.replied, self.errors),
            "Git hafıza: %s" % self._last_push_detail,
        ]
        if self.cfg.public_url:
            lines.append("Health: %s/health" % self.cfg.public_url.rstrip("/"))
        await self.send(
            context.bot,
            update.effective_chat.id,
            "\n".join(lines),
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        self.memory.clear_messages()
        self.memory.save()
        self._schedule_push("temizle")
        await self.send(
            context.bot,
            update.effective_chat.id,
            "Sohbet geçmişi temizlendi. (Kalıcı notların ve hatırlatıcıların korunuyor.)",
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        chat_id = update.effective_chat.id
        reply_to = update.effective_message.message_id if update.effective_message else None
        query = " ".join(context.args or []).strip()
        if not query:
            await self.send(context.bot, chat_id, "Kullanım: /search <sorgu>\nÖrnek: /search Balıkesir hava durumu", reply_to=reply_to)
            return
        if not self.cfg.search_enabled:
            await self.send(context.bot, chat_id, "Arama devre dışı (SEARCH_ENABLED=0).", reply_to=reply_to)
            return
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError:
            pass
        results, err = await asyncio.to_thread(ddg_search, query, self.cfg.search_max_results, self.cfg.search_region)
        if err:
            await self.send(context.bot, chat_id, "Arama yapılamadı: %s" % err, reply_to=reply_to)
            return
        if not results:
            await self.send(context.bot, chat_id, "'%s' için sonuç bulunamadı." % query, reply_to=reply_to)
            return

        now = datetime.now(self.tz)
        system_prompt = build_system_prompt(self.cfg.system_prompt, now, self.memory.memories, self.cfg.timezone_name)
        question = (
            "Kullanıcının sorusu: %s\n\nİnternet arama sonuçları:\n%s\n\n"
            "Bu sonuçlara dayanarak kısa ve net cevap ver; kaynak linklerini ekle." % (query, format_results(results))
        )
        prompt = build_prompt_messages(
            history=self.memory.messages,
            last_user=question,
            system_prompt=system_prompt,
            max_history_messages=self.cfg.max_history_messages,
            max_chars=self.cfg.max_prompt_chars,
        )
        try:
            result = await asyncio.to_thread(self.chat.complete, prompt, None)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            LOGGER.exception("Arama özeti üretilemedi: %s", type(exc).__name__)
            await self.send(
                context.bot,
                chat_id,
                "Özet üretilemedi, ham arama sonuçları:\n\n%s" % format_results(results),
                reply_to=reply_to,
            )
            return
        await self.send(context.bot, chat_id, result.text, reply_to=reply_to)
        self.memory.append_message("user", "/search %s" % query)
        self.memory.append_message("assistant", result.text)
        self.memory.save()
        self.replied += 1
        self._schedule_push()

    async def cmd_remember(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        chat_id = update.effective_chat.id
        reply_to = update.effective_message.message_id if update.effective_message else None
        note = " ".join(context.args or []).strip()
        if not note:
            await self.send(context.bot, chat_id, "Kullanım: /remember <not>\nÖrnek: /remember Ofisim Kadıköy'de", reply_to=reply_to)
            return
        if self.memory.add_memory(note):
            self.memory.save()
            self._schedule_push("not")
            await self.send(context.bot, chat_id, "📌 Kaydedildi: %s" % note, reply_to=reply_to)
        else:
            await self.send(context.bot, chat_id, "Bu not zaten hafızanda.", reply_to=reply_to)

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        chat_id = update.effective_chat.id
        reply_to = update.effective_message.message_id if update.effective_message else None
        memories = self.memory.memories
        if not memories:
            await self.send(context.bot, chat_id, "Hafızanda henüz kalıcı not yok. (/remember <not> ile ekleyebilirsin.)", reply_to=reply_to)
            return
        lines = ["️ Kalıcı notların (%d):" % len(memories)]
        for index, note in enumerate(memories, 1):
            lines.append("%d. %s" % (index, note))
        await self.send(context.bot, chat_id, "\n".join(lines), reply_to=reply_to)

    async def cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        chat_id = update.effective_chat.id
        reply_to = update.effective_message.message_id if update.effective_message else None
        if not context.args or not str(context.args[0]).isdigit():
            await self.send(
                context.bot,
                chat_id,
                "Kullanım: /forget <no> (önce /memory ile listele)",
                reply_to=reply_to,
            )
            return
        removed = self.memory.remove_memory(int(context.args[0]))
        if removed is None:
            await self.send(context.bot, chat_id, "Böyle numaralı not yok. /memory ile listele.", reply_to=reply_to)
            return
        self.memory.save()
        self._schedule_push("not-sil")
        await self.send(context.bot, chat_id, "🗑️ Silindi: %s" % removed, reply_to=reply_to)

    async def cmd_remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        chat_id = update.effective_chat.id
        reply_to = update.effective_message.message_id if update.effective_message else None
        if not self.cfg.reminders_enabled:
            await self.send(context.bot, chat_id, "Hatırlatıcılar devre dışı (REMINDERS_ENABLED=0).", reply_to=reply_to)
            return
        parsed = parse_remind_args(context.args or [], self.tz)
        if parsed is None:
            await self.send(
                context.bot,
                chat_id,
                "Kullanım:\n/remind 08:00 kahvaltı\n/remind yarın 21:30 diş fırçala\n"
                "(Bugün o saat geçtiyse ertesi güne alınır. 'yarın 08:00 hatırlat: ...' şeklinde yazmak da çalışır.)",
                reply_to=reply_to,
            )
            return
        fire_at, message = parsed
        rid = uuid.uuid4().hex[:10]
        if not self.scheduler.schedule(rid, fire_at, self._fire_reminder, rid, fire_at.strftime("%Y-%m-%d %H:%M"), message):
            await self.send(context.bot, chat_id, "Hatırlatıcı kurulamadı (zaman geçmişte kalmış olabilir).", reply_to=reply_to)
            return
        self.memory.add_reminder(rid, fire_at.strftime("%Y-%m-%d %H:%M"), message)
        self.memory.save()
        self._schedule_push("hatırlatıcı")
        await self.send(context.bot, chat_id, "⏰ Kuruldu: %s — %s" % (fire_at.strftime("%d.%m.%Y %H:%M"), message), reply_to=reply_to)

    # ---------- mesaj handler'ları -----------------------------------------------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        if not await self._guard(update, context):
            return
        text = message.text.strip()
        if not text:
            return
        LOGGER.info("Mesaj alındı: %s | %s", user_display(update), text[:200])
        await self._process(update, context, text)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        photos = message.photo if message else []
        if not photos:
            return
        photo = photos[-1]  # en büyük boy
        if photo.file_size and photo.file_size > self.cfg.max_photo_bytes:
            await self.send(
                context.bot,
                chat_id,
                "Görsel çok büyük: %d MB (limit %d MB)."
                % (photo.file_size // (1024 * 1024), self.cfg.max_photo_bytes // (1024 * 1024)),
            )
            return
        data = await download_bytes(context.bot, photo.file_id)
        if data is None:
            await self.send(context.bot, chat_id, "Görsel indirilemedi, tekrar gönderir misin?")
            return
        caption = (message.caption or "").strip()
        user_text = caption or "Bu görseli analiz et: ne var, ne anlatıyor? Önemli detayları ve varsa görseldeki yazıları belirt."
        LOGGER.info("Görsel alındı: %s (%d bayt)", user_display(update), len(data))
        await self._process(update, context, user_text, images=[(data, "image/jpeg")], status_text="🖼️ Görsel inceleniyor...")

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        if not self.cfg.voice_enabled:
            await self.send(context.bot, chat_id, "Sesli mesaj desteği kapalı (VOICE_ENABLED=0).")
            return
        voice = message.voice if message else None
        if voice is None:
            return
        if voice.file_size and voice.file_size > self.cfg.max_voice_bytes:
            await self.send(
                context.bot,
                chat_id,
                "Ses çok uzun/büyük (limit %d MB)." % (self.cfg.max_voice_bytes // (1024 * 1024)),
            )
            return
        data = await download_bytes(context.bot, voice.file_id)
        if data is None:
            await self.send(context.bot, chat_id, "Ses indirilemedi, tekrar gönderir misin?")
            return
        status_msg = None
        if not self.cfg.dry_run:
            try:
                status_msg = await context.bot.send_message(chat_id, "🎧 Sesli mesaj dinleniyor...")
            except TelegramError:
                status_msg = None
        try:
            text = await asyncio.to_thread(self.chat.transcribe, data)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            LOGGER.exception("Ses çevrimi başarısız: %s", type(exc).__name__)
            notice = "😕 Sesi metne çeviremedim. Tekrar dener misin?"
            if status_msg is not None:
                try:
                    await status_msg.edit_text(notice)
                except TelegramError:
                    await self.send(context.bot, chat_id, notice)
            else:
                await self.send(context.bot, chat_id, notice)
            return
        if not text.strip():
            if status_msg is not None:
                try:
                    await status_msg.edit_text("😕 Seste konuşma anlayamadım, tekrar dener misin?")
                except TelegramError:
                    pass
            else:
                await self.send(context.bot, chat_id, "😕 Seste konuşma anlayamadım, tekrar dener misin?")
            return
        if status_msg is not None:
            try:
                shown = text[:300] + ("…" if len(text) > 300 else "")
                await status_msg.edit_text("✅ Anladım: %s" % shown)
            except TelegramError:
                pass
        LOGGER.info("Sesli mesaj çevrildi: %s | %s", user_display(update), text[:200])
        await self._process(update, context, text)

    async def on_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        audio = message.audio if message else None
        if audio is None:
            return
        if not self.cfg.voice_enabled:
            await self.send(context.bot, chat_id, "Ses dosyası desteği kapalı (VOICE_ENABLED=0).")
            return
        mime = (audio.mime_type or "").lower()
        if not mime.startswith("audio/"):
            await self.send(context.bot, chat_id, "Bu ses dosyası tipini çeviremiyorum (mime: %s)." % mime)
            return
        if audio.file_size and audio.file_size > self.cfg.max_voice_bytes:
            await self.send(
                context.bot,
                chat_id,
                "Ses dosyası çok büyük (limit %d MB)." % (self.cfg.max_voice_bytes // (1024 * 1024)),
            )
            return
        data = await download_bytes(context.bot, audio.file_id)
        if data is None:
            await self.send(context.bot, chat_id, "Ses dosyası indirilemedi, tekrar gönderir misin?")
            return
        try:
            text = await asyncio.to_thread(self.chat.transcribe, data)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            LOGGER.exception("Ses dosyası çevrimi başarısız: %s", type(exc).__name__)
            await self.send(context.bot, chat_id, "😕 Ses dosyasını metne çeviremedim.")
            return
        if not text.strip():
            await self.send(context.bot, chat_id, "😕 Dosyada konuşma anlayamadım.")
            return
        LOGGER.info("Ses dosyası çevrildi: %s | %s", user_display(update), text[:200])
        await self._process(update, context, text, status_text="🎧 Ses dosyası dinleniyor...")

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        message = update.effective_message
        chat_id = update.effective_chat.id
        if not self.cfg.files_enabled:
            await self.send(context.bot, chat_id, "Dosya okuma devre dışı (FILES_ENABLED=0).")
            return
        doc = message.document if message else None
        if doc is None:
            return
        name = doc.file_name or "dosya"
        ext = ext_of(name)
        if doc.file_size and doc.file_size > self.cfg.max_document_bytes:
            await self.send(
                context.bot,
                chat_id,
                "Dosya çok büyük (limit %d MB)." % (self.cfg.max_document_bytes // (1024 * 1024)),
            )
            return
        data = await download_bytes(context.bot, doc.file_id)
        if data is None:
            await self.send(context.bot, chat_id, "Dosya indirilemedi, tekrar gönderir misin?")
            return
        caption = (message.caption or "").strip()

        if ext in IMAGE_EXTS:
            user_text = caption or "Bu görseli analiz et."
            await self._process(update, context, user_text, images=[(data, MIME_BY_EXT.get(ext, "image/jpeg"))], status_text="🖼️ Görsel inceleniyor...")
            return

        if ext == ".pdf":
            text = await asyncio.to_thread(pdf_to_text, data, self.cfg.max_file_text_chars)
            if not text.strip():
                await self.send(context.bot, chat_id, "Bu PDF'ten metin çıkarılamadı (taramalı/görsel olabilir; OCR desteği yok).")
                return
            user_text = (
                "[PDF: %s] dosyasının metni:\n\n%s\n\n---\n"
                "Kullanıcım bu dosyayı gönderdi. İçeriğini kısaca özetle, önemli noktaları belirt; detay istenirse ver."
                % (name, text[: self.cfg.max_file_text_chars])
            )
            await self._process(update, context, user_text, status_text="📄 PDF okunuyor...")
            return

        if ext in TEXT_EXTS or ext == "":
            text = decode_text(data, name)[: self.cfg.max_file_text_chars]
            if not text.strip():
                await self.send(context.bot, chat_id, "Bu dosyada okunabilir metin bulamadım.")
                return
            user_text = (
                "[Dosya: %s] içeriği:\n\n%s\n\n---\n"
                "Kullanıcım bu dosyayı gönderdi. İçeriğini kısaca özetle, önemli noktaları belirt; detay istenirse ver."
                % (name, text)
            )
            await self._process(update, context, user_text, status_text="📄 Dosya okunuyor...")
            return

        await self.send(
            context.bot,
            chat_id,
            "Bu dosya tipini okuyamıyorum. Desteklenenler: PDF, metin/kod dosyaları, görseller.",
        )

    async def on_other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update, context):
            return
        if self.cfg.dry_run:
            return
        await self.send(
            context.bot,
            update.effective_chat.id,
            "Bu mesaj tipini işleyemiyorum. (Desteklenenler: yazı, görsel, sesli mesaj, PDF/metin dosyası.)",
            reply_to=update.effective_message.message_id if update.effective_message else None,
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.errors += 1
        LOGGER.error("Handler hatası: %s", context.error, exc_info=context.error)
        try:
            if context and context.bot and update and update.effective_message:
                await context.bot.send_message(
                    update.effective_chat.id,
                    "😵 Beklenmeyen bir hata oluştu; loglara yazıldı. Tekrar dener misin?",
                )
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# Uygulama kurulumu
# ===========================================================================
def build_application(cfg: Config, runner: BotRunner, mode: str) -> Application:
    builder = ApplicationBuilder().token(cfg.telegram_token)
    if mode == "webhook":
        # Kendi HTTP sunucumuz güncellemeleri process_update'e iletiyor.
        builder = builder.updater(None)
    if cfg.telegram_api_base_url:
        LOGGER.info("Özel Telegram API adresi kullanılıyor: %s", cfg.telegram_api_base_url)
        builder = builder.base_url(cfg.telegram_api_base_url)
    application = builder.build()

    application.add_handler(CommandHandler("start", runner.cmd_start))
    application.add_handler(CommandHandler("help", runner.cmd_help))
    application.add_handler(CommandHandler("search", runner.cmd_search))
    application.add_handler(CommandHandler("remember", runner.cmd_remember))
    application.add_handler(CommandHandler("memory", runner.cmd_memory))
    application.add_handler(CommandHandler("forget", runner.cmd_forget))
    application.add_handler(CommandHandler("remind", runner.cmd_remind))
    application.add_handler(CommandHandler("clear", runner.cmd_clear))
    application.add_handler(CommandHandler("status", runner.cmd_status))
    application.add_handler(CommandHandler("id", runner.cmd_id))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, runner.on_text))
    application.add_handler(MessageHandler(filters.PHOTO, runner.on_photo))
    application.add_handler(MessageHandler(filters.VOICE, runner.on_voice))
    application.add_handler(MessageHandler(filters.AUDIO, runner.on_audio))
    application.add_handler(MessageHandler(filters.Document.ALL, runner.on_document))
    application.add_handler(
        MessageHandler(
            ~(
                filters.TEXT
                | filters.PHOTO
                | filters.VOICE
                | filters.AUDIO
                | filters.Document.ALL
                | filters.StatusUpdate.ALL
            ),
            runner.on_other,
        )
    )
    application.add_error_handler(runner.on_error)

    # Yaşam döngüsü kancaları:
    # - poll modunda run_polling() bunları otomatik çağırır.
    # - webhook modunda main.py bunları elle çağırır (initialize/start sonrası).
    application.post_init = runner.post_init
    application.post_shutdown = runner.post_shutdown
    return application
