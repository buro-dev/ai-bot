"""Ağ ve API anahtarı gerektirmeyen iç testler.

Çalıştırma: ``python main.py --self-test``
"""
from __future__ import annotations

import base64
import io
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PASS_MARK = "  \u2705"
FAIL_MARK = "  \u274c"


def run_self_tests() -> int:
    passed = 0
    failed = 0

    def check(name: str, condition: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print("%s %s" % (PASS_MARK, name))
        else:
            failed += 1
            print("%s %s %s" % (FAIL_MARK, name, extra))

    print("İç testler çalışıyor...\n")

    # ------------------------------------------------------------------ 1
    print("[1] Hafıza deposu (MemoryStore)")
    from bot.memory import MemoryStore

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "chat_history.json"
        store = MemoryStore(path, max_messages=5, max_memories=10)
        store.load()
        store.append_message("user", "selam")
        store.append_message("assistant", "merhaba")
        store.add_memory("kullanıcının kedisi: Pamuk")
        store.add_reminder("r1", "2099-01-01 08:00", "test hatırlatıcı")
        store.save()

        store2 = MemoryStore(path, max_messages=5, max_memories=10)
        store2.load()
        check(
            "mesajlar kalıcı",
            store2.messages
            == [{"role": "user", "content": "selam"}, {"role": "assistant", "content": "merhaba"}],
            str(store2.messages),
        )
        check("notlar kalıcı", store2.memories == ["kullanıcının kedisi: Pamuk"], str(store2.memories))
        check("hatırlatıcılar kalıcı", len(store2.pending_reminders()) == 1)
        check(
            "aynı not tekrar eklenmez",
            store2.add_memory("Kullanıcının kedisi: pamuk") is False,
        )
        for i in range(10):
            store2.append_message("user", "m%02d" % i)
        store2.save()
        store3 = MemoryStore(path, max_messages=5, max_memories=10)
        store3.load()
        check("mesaj limiti uygulanıyor", len(store3.messages) == 5, "len=%d" % len(store3.messages))
        removed = store3.remove_memory(1)
        check("not silinebiliyor", removed == "kullanıcının kedisi: Pamuk" and len(store3.memories) == 0)

        # v1 uyumluluk: düz mesaj listesi
        v1 = Path(td) / "v1.json"
        v1.write_text('[{"role": "user", "content": "eski"}]', encoding="utf-8")
        store4 = MemoryStore(v1, max_messages=10)
        store4.load()
        check("v1 format uyumu", store4.messages == [{"role": "user", "content": "eski"}])

    # ------------------------------------------------------------------ 2
    print("[2] Prompt oluşturma")
    from bot.runner import build_prompt_messages

    history = [
        {"role": "user", "content": "a" * 100},
        {"role": "assistant", "content": "b" * 100},
    ]
    messages = build_prompt_messages(history, "soru", "sistem", max_history_messages=10, max_chars=200)
    check(
        "karakter bütçesi uygulanır",
        len(messages) == 3 and messages[0]["role"] == "system" and messages[-1]["content"] == "soru",
        str(len(messages)),
    )
    messages_long = build_prompt_messages(history, "soru", "sistem", max_history_messages=1, max_chars=100000)
    check(
        "mesaj limiti uygulanır",
        [m["content"] for m in messages_long] == ["sistem", "b" * 100, "soru"],
        str(messages_long),
    )
    # görsel parçalı kullanıcı mesajı
    parts = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,x"}}, {"type": "text", "text": "bu ne?"}]
    messages_img = build_prompt_messages([], parts, "sistem", max_history_messages=10, max_chars=10000)
    check("görsel parça mesajı", messages_img[-1]["content"] == parts)

    # ------------------------------------------------------------------ 3
    print("[3] Mesaj bölme")
    from bot.runner import split_message

    chunks = split_message("x" * 9000)
    check(
        "parçalar sınıra uyar ve içerik korunur",
        all(len(c) <= 4096 for c in chunks) and sum(len(c) for c in chunks) >= 9000,
        str([len(c) for c in chunks]),
    )
    check("kısa mesaj bölünmez", split_message("selam") == ["selam"])

    # ------------------------------------------------------------------ 4
    print("[4] Zaman çözümleri")
    from bot.reminders import parse_fire_at
    from bot.runner import parse_remind_args

    tz = timezone.utc
    check("geçerli zaman", parse_fire_at("2026-09-14 08:00", tz) is not None)
    check("geçersiz zaman (göreceli)", parse_fire_at("yarın 8:00", tz) is None)
    check("geçersiz tarih", parse_fire_at("2026-13-40 08:00", tz) is None)

    now = datetime(2026, 9, 13, 20, 0, tzinfo=tz)
    parsed = parse_remind_args(["08:00", "kahvaltı"], tz, now=now)
    check(
        "/remind bugün (saat geçtiyse +1 gün)",
        parsed is not None and parsed[0] == datetime(2026, 9, 14, 8, 0, tzinfo=tz) and parsed[1] == "kahvaltı",
        str(parsed),
    )
    parsed_tomorrow = parse_remind_args(["yarın", "21:30", "diş fırçala"], tz, now=now)
    check(
        "/remind yarın",
        parsed_tomorrow is not None and parsed_tomorrow[0] == datetime(2026, 9, 14, 21, 30, tzinfo=tz),
        str(parsed_tomorrow),
    )
    check("/remind geçersiz", parse_remind_args(["abc", "x"], tz, now=now) is None)

    # ------------------------------------------------------------------ 5
    print("[5] Git yardımcıları")
    from bot.memory import git_extraheader_env

    env = git_extraheader_env("TOKEN123")
    expected_basic = base64.b64encode(b"x-access-token:TOKEN123").decode("ascii")
    check(
        "PAT auth header",
        env.get("GIT_CONFIG_VALUE_0") == "AUTHORIZATION: basic %s" % expected_basic,
    )
    check("terminal prompt kapalı", env.get("GIT_TERMINAL_PROMPT") == "0")

    # ------------------------------------------------------------------ 6
    print("[6] Medya yardımcıları")
    from bot.media import data_url, decode_text, pdf_to_text

    check(
        "data_url",
        data_url(b"abc", "image/png").startswith("data:image/png;base64,"),
    )
    check("latin5 (Türkçe) çözümleme", decode_text(b"Merhaba \xf6d", "test.txt") == "Merhaba öd")

    try:
        from pypdf import PdfWriter

        buffer = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(buffer)
        result = pdf_to_text(buffer.getvalue())
        check("boş PDF işlenir", isinstance(result, str))
    except Exception as exc:  # noqa: BLE001
        check("PDF işlenir", False, str(exc))

    # ------------------------------------------------------------------ 7
    print("[7] Yapılandırma")
    os.environ["TELEGRAM_TOKEN"] = "123:TEST"
    os.environ["ALLOWED_USER_ID"] = "42"
    os.environ["GROQ_API_KEY"] = "gsk_test"
    os.environ.pop("PUBLIC_URL", None)
    os.environ.pop("RUN_MODE", None)
    try:
        from bot.config import ConfigError, load_config, resolve_mode

        cfg = load_config()
        check("config yüklenir", cfg.allowed_user_ids == (42,))
        check("poll modu (PUBLIC_URL yok)", resolve_mode(cfg) == "poll")
        check("webhook secret üretilmez (poll)", cfg.webhook_secret is None)
        check("varsayılan model", cfg.models[0] == "openai/gpt-oss-120b")

        os.environ["PUBLIC_URL"] = "https://ornek.onrender.com/"
        cfg2 = load_config()
        check("webhook modu (PUBLIC_URL var)", resolve_mode(cfg2) == "webhook")
        check("public_url kuyruksuz", cfg2.public_url == "https://ornek.onrender.com")
        check("webhook secret üretilir", bool(cfg2.webhook_secret))

        os.environ["ALLOWED_USER_ID"] = "abc"
        try:
            load_config()
            check("geçersiz kullanıcı ID yakalanır", False)
        except ConfigError:
            check("geçersiz kullanıcı ID yakalanır", True)
    finally:
        for key in ("TELEGRAM_TOKEN", "ALLOWED_USER_ID", "GROQ_API_KEY", "PUBLIC_URL", "RUN_MODE"):
            os.environ.pop(key, None)

    # ------------------------------------------------------------------ 8
    print("[8] Saat dilimi")
    from bot.runner import get_timezone

    tz_tr = get_timezone("Europe/Istanbul")
    check("Europe/Istanbul bulunur (ya da UTC+3'a düşer)", tz_tr is not None)
    tz_fallback = get_timezone("Bogus/Nowhere")
    check("geçersiz dilim UTC+3'e düşer", isinstance(tz_fallback, timezone))

    print("\nSonuç: %d başarılı, %d başarısız" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_self_tests())
