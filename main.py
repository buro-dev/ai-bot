#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kişisel Telegram AI asistan botu — giriş noktası.

Ne yapar?
---------
1. Yalnızca ``ALLOWED_USER_ID``'ye sahip kullanıcıya cevap verir (diğerleri sessizce yok sayılır).
2. Metin, görsel, sesli mesaj ve PDF/metin dosyalarını işler.
3. Groq (gpt-oss) ile sohbet eder; modelin kararına göre DuckDuckGo'da internet araması yapar,
   kalıcı notlar tutar ve zamanlanmış hatırlatıcılar kurar.
4. Hafızayı ``chat_history.json`` içinde tutar ve her değişiklikte repoya (``memory`` dalı)
   push eder; başlarken o daldan geri yükler. Render'ın geçici dosya sistemine karşı hafıza
   deploy'lar arasında böylece kalıcıdır.

Çalışma modları (``RUN_MODE``)
------------------------------
* ``poll``    : uzun-polling (yerel makine).
* ``webhook`` : Render'da — Telegram güncellemeleri POST /webhook üzerinden gelir.
* ``auto``    : varsayılan. ``PUBLIC_URL`` tanımlıysa webhook (Render otomatik tanımlar),
               değilse poll.

Kullanım
--------
    cp .env.example .env   # değerleri doldurun
    pip install -r requirements.txt
    python main.py              # çalıştır (yerelde poll modunda)
    python main.py --self-test  # ağ/anahtar gerektirmeyen iç testler
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from aiohttp import web
from telegram import Update

from bot import __version__
from bot.ai import GroqChat
from bot.config import (
    REPO_ROOT,
    ConfigError,
    load_config,
    load_dotenv,
    resolve_mode,
    setup_logging,
)
from bot.memory import GitSync, MemoryStore
from bot.reminders import ReminderScheduler
from bot.runner import BotRunner, build_application, get_timezone
from bot.server import build_http_app

LOGGER = logging.getLogger("ai_bot")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kişisel Telegram AI asistan botu")
    parser.add_argument("--self-test", action="store_true", help="iç testleri çalıştır (ağ/anahtar gerektirmez)")
    parser.add_argument("--verbose", action="store_true", help="DEBUG log seviyesi")
    parser.add_argument("--version", action="version", version="%%(prog)s %s" % __version__)
    return parser.parse_args(argv)


async def _run_webhook(application, runner: BotRunner, cfg) -> None:
    """Webhook modu: PTB'yi elle başlat + kendi aiohttp sunucumuzu dinlet."""
    await application.initialize()
    await application.start()
    # run_* yardımcıları olmadığı için yaşam döngüsü kancalarını elle çağırıyoruz.
    await runner.post_init(application)

    http_app = build_http_app(runner, cfg, application)
    http_runner = web.AppRunner(http_app)
    await http_runner.setup()
    site = web.TCPSite(http_runner, "0.0.0.0", cfg.port)
    await site.start()
    base = (cfg.public_url or "http://localhost:%d" % cfg.port).rstrip("/")
    LOGGER.info("HTTP dinleniyor: 0.0.0.0:%d | health: %s/health | webhook: %s%s", cfg.port, base, base, cfg.webhook_path)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    await stop.wait()

    # Kapanış: önce kancalar (zamanlayıcı + son push), sonra PTB.
    try:
        await runner.post_shutdown(application)
    except Exception:  # noqa: BLE001
        LOGGER.exception("post_shutdown hatası")
    try:
        await application.stop()
        await application.shutdown()
    except Exception:  # noqa: BLE001
        LOGGER.exception("application kapanış hatası")
    finally:
        await http_runner.cleanup()


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.self_test:
        from bot.selftest import run_self_tests

        return run_self_tests()

    load_dotenv()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print("YAPILANDIRMA HATASI:\n%s" % exc)
        return 2
    setup_logging(cfg, verbose=args.verbose)

    mode = resolve_mode(cfg)

    memory = MemoryStore(cfg.history_file, cfg.max_history_messages, cfg.max_memories, cfg.dry_run)
    git = GitSync(REPO_ROOT, cfg.history_file, cfg)

    # 1) Hafızayı repodan geri yükle (Render'ın geçici dosya sistemi için).
    if cfg.git_push_enabled and not cfg.dry_run and git.is_repo():
        try:
            result = git.restore()
            if result.ok:
                LOGGER.info("Hafıza git'ten geri yüklendi: %s", result.detail)
            else:
                LOGGER.info("Hafıza geri yükleme: %s", result.detail)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("git restore hatası (boş hafızayla devam): %s", exc)

    # 2) Hafızayı yükle, bileşenleri kur.
    memory.load()
    chat = GroqChat(cfg)
    scheduler = ReminderScheduler(get_timezone(cfg.timezone_name))
    runner = BotRunner(cfg, chat, memory, git, scheduler, mode)
    application = build_application(cfg, runner, mode)

    if mode == "poll":
        LOGGER.info("Polling modunda başlatılıyor...")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    else:
        try:
            asyncio.run(_run_webhook(application, runner, cfg))
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
