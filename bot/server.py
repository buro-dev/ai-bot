"""Render için küçük HTTP sunucusu: /health + Telegram webhook'u.

Webhook modunda Telegram güncellemeleri POST /webhook üzerinden gelir;
yanıt 200 ile anında döner, gerçek işleme arka planda (create_task) yapılır.
Böylece Telegram'ın 60 saniyelik webhook zaman aşımına takılmayız.
"""
from __future__ import annotations

import logging

from aiohttp import web
from telegram import Update

from .config import Config
from .runner import BotRunner

LOGGER = logging.getLogger("ai_bot")


async def _health(request: web.Request) -> web.Response:
    runner: BotRunner = request.app["runner"]
    return web.json_response(
        {
            "ok": True,
            "uptime_s": int(runner.uptime()),
            "mode": runner.mode,
            "pending_reminders": len(runner.memory.pending_reminders()),
        }
    )


async def _root(request: web.Request) -> web.Response:
    runner: BotRunner = request.app["runner"]
    return web.json_response(
        {
            "bot": "ai-bot",
            "ok": True,
            "mode": runner.mode,
            "uptime_s": int(runner.uptime()),
            "webhook": runner.cfg.webhook_path if runner.mode == "webhook" else None,
        }
    )


async def _webhook(request: web.Request) -> web.Response:
    cfg: Config = request.app["cfg"]
    runner: BotRunner = request.app["runner"]
    application = request.app["application"]

    if cfg.webhook_secret:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if provided != cfg.webhook_secret:
            LOGGER.warning("Webhook gizli token eşleşmedi: %s", request.remote)
            return web.Response(status=403, text="geçersiz token")

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="geçersiz json")

    bot = application.bot
    if bot is None:
        return web.Response(status=503, text="bot hazır değil")

    update = Update.de_json(data, bot)
    if update is None:
        return web.Response(status=400, text="geçersiz update")

    runner.spawn_update(update)
    return web.Response(status=200, text="ok")


def build_http_app(runner: BotRunner, cfg: Config, application) -> web.Application:
    app = web.Application()
    app["runner"] = runner
    app["cfg"] = cfg
    app["application"] = application
    app.router.add_get("/", _root)
    app.router.add_get("/health", _health)
    app.router.add_post(cfg.webhook_path, _webhook)
    return app
