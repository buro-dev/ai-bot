#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Element (Matrix) + Groq sohbet botu — GitHub Actions üzerinde çalışır.

Nasıl çalışır:
  1) MATRIX_TOKEN ile matrix.org'a bağlanır (kullanıcı adını kendisi bulur).
  2) Oda davetlerini otomatik kabul eder (MATRIX_OWNER tanımlıysa sadece sahibinden).
  3) ŞİFRESİZ odalardaki mesajları Groq'a gönderir, cevabı odaya yazar.
  4) ŞİFRELİ odaları okuyamaz (Element'deki varsayılan DM'ler şifrelidir!).
     Bunun yerine kendisi şifresiz bir "AI Bot Sohbet" odası kurup
     o odadaki kişileri oraya davet eder. Daveti kabul et, o odada yaz.
  5) Sohbet geçmişini chat_history.json'a yazar ve repoya push eder.

Zorunlu ortam değişkenleri:
  MATRIX_TOKEN   Bot hesabının Matrix access token'ı
  GROQ_API_KEY   Groq API anahtarı

Opsiyonel:
  MATRIX_USER      @bot:matrix.org (verilmezse otomatik bulunur)
  MATRIX_OWNER     @siz:matrix.org (verilirse bot SADECE sana cevap verir
                   ve açılışta sana özel oda kurup davet gönderir)
  GH_PAT_TOKEN     Push için token; verilmezse GITHUB_TOKEN (Actions) kullanılır
  GITHUB_REPOSITORY  "kullanici/repo" (Actions otomatik verir)
  GITHUB_REF_NAME    Push edilecek dal (Actions otomatik verir)
  BOT_RUN_MINUTES    Bu oturumda kaç dakika dinleneceği (varsayılan 15)
  GROQ_MODEL         Varsayılan "llama-3.3-70b-versatile"
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from groq import Groq
from nio import AsyncClient, InviteEvent, MatrixRoom, MegolmEvent, RoomMessageText

try:
    from nio.api import RoomPreset
except Exception:  # eski sürümler için
    RoomPreset = None

# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------

def _env(name, default=""):
    return os.environ.get(name, default).strip()

def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default

HOMESERVER = _env("MATRIX_HOMESERVER", "https://matrix.org")
MATRIX_USER = _env("MATRIX_USER")          # opsiyonel
MATRIX_TOKEN = _env("MATRIX_TOKEN")        # ZORUNLU
MATRIX_OWNER = _env("MATRIX_OWNER")        # opsiyonel
GROQ_API_KEY = _env("GROQ_API_KEY")        # ZORUNLU

GROQ_MODEL = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
SYSTEM_PROMPT = _env(
    "BOT_SYSTEM_PROMPT",
    "Sen Element sohbetinde kullanıcının kişisel yapay zekâ asistanısın. "
    "Kullanıcının yazdığı dilde (çoğunlukla Türkçe) doğal, net ve yardımcı "
    "cevaplar ver. Gereksiz yere uzatma.",
)

HISTORY_FILE = Path(_env("HISTORY_FILE", "chat_history.json"))
RUN_MINUTES = _env_int("BOT_RUN_MINUTES", 15)
CONTEXT_MESSAGES = _env_int("CONTEXT_MESSAGES", 20)   # modele gönderilen mesaj sayısı
KEEP_MESSAGES = _env_int("KEEP_MESSAGES", 400)         # dosyada saklanan mesaj sayısı
KEEP_SEEN = 2000                                       # "cevaplanmış" olay listesinin boyutu
MAX_MESSAGE_AGE_MS = 7 * 24 * 60 * 60 * 1000           # 7 günden eski mesajlara cevap yok

GH_TOKEN = _env("GH_PAT_TOKEN") or _env("GITHUB_TOKEN")
GITHUB_REPO = _env("GITHUB_REPOSITORY")
TARGET_BRANCH = _env("GITHUB_REF_NAME") or "main"

STARTED_AT = time.time()

client = None            # AsyncClient (main içinde kurulur)
send_lock = None         # aynı anda tek cevap üretimi için

# ---------------------------------------------------------------------------
# Durum (chat_history.json)
# ---------------------------------------------------------------------------

def load_state():
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = []
    # Dosya bir liste ise (ör. ilk kurulumda []) yapıya sar.
    if isinstance(data, list):
        data = {"messages": data}
    if not isinstance(data, dict):
        data = {"messages": []}
    data.setdefault("messages", [])
    data.setdefault("seen", [])            # cevaplanan mesaj olay ID'leri
    data.setdefault("encrypted_rooms", []) # şifreli oda ID'leri
    data.setdefault("alt_rooms", {})       # şifreli oda -> şifresiz oda eşlemesi
    data.setdefault("created_rooms", [])   # botun kurduğu odalar
    return data

STATE = load_state()

def save_state():
    STATE["messages"] = STATE["messages"][-KEEP_MESSAGES:]
    STATE["seen"] = STATE["seen"][-KEEP_SEEN:]
    tmp = HISTORY_FILE.with_name(HISTORY_FILE.name + ".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HISTORY_FILE)

def mark_seen(event_id):
    if event_id and event_id not in STATE["seen"]:
        STATE["seen"].append(event_id)

# ---------------------------------------------------------------------------
# Git: chat_history.json'u repoya push et
# ---------------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)

def push_history(reason="sohbet gecmisi guncellendi"):
    try:
        if git("status", "--porcelain", "--", HISTORY_FILE.name).stdout.strip() == "":
            return False  # değişiklik yok
        git("config", "user.name", "ai-bot")
        git("config", "user.email", "ai-bot@users.noreply.github.com")
        git("add", HISTORY_FILE.name)
        if git("commit", "-m", f"chat: {reason}").returncode != 0:
            return False
        if not (GITHUB_REPO and GH_TOKEN):
            print("(push atlandı: token/repo bilgisi yok — dosya yine de kaydedildi)", flush=True)
            return False
        url = f"https://x-access-token:{GH_TOKEN}@github.com/{GITHUB_REPO}.git"
        p = git("push", url, f"HEAD:{TARGET_BRANCH}")
        if p.returncode != 0:
            # Uzak dal ilerlemiş olabilir: üzerine rebase edip tekrar dene.
            pull = git("pull", "--rebase", url, TARGET_BRANCH)
            if pull.returncode != 0:
                git("rebase", "--abort")
            else:
                p = git("push", url, f"HEAD:{TARGET_BRANCH}")
        if p.returncode != 0:
            print("!! Push başarısız: " + (p.stderr or "")[-400:], flush=True)
            return False
        print(f"↻ chat_history.json repoya push edildi ({reason})", flush=True)
        return True
    except Exception as e:
        print("!! push_history hatası:", e, flush=True)
        return False

# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

_groq_client = None

def groq_reply(history):
    """history: [{'role': ..., 'content': ...}, ...] -> cevap metni"""
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-CONTEXT_MESSAGES:]]
    last_err = None
    for attempt in range(3):
        try:
            resp = _groq_client.chat.completions.create(
                model=GROQ_MODEL, messages=msgs, temperature=0.7,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e
            print(f"Groq denemesi {attempt + 1} başarısız: {e}", flush=True)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last_err))

# ---------------------------------------------------------------------------
# Matrix yardımcıları
# ---------------------------------------------------------------------------

async def send_text(room_id, text, reply_to=None):
    content = {"msgtype": "m.text", "body": (text or "…")[:60000]}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    await client.room_send(room_id, "m.room.message", content)

def room_is_encrypted(room):
    if room.room_id in STATE["encrypted_rooms"]:
        return True
    return bool(getattr(room, "encrypted", False))

async def create_plain_room(invites, reason_room_id=None):
    """Şifresiz özel oda kur, davetleri gönder."""
    invites = [i for i in dict.fromkeys(invites) if i and i != client.user_id]
    if not invites:
        return None
    kwargs = {
        "name": "AI Bot Sohbet",
        "topic": "Groq destekli sohbet odası (şifresiz) — bot burada çalışır",
        "invite": invites,
    }
    if RoomPreset is not None:
        kwargs["preset"] = RoomPreset.private_chat
    resp = await client.room_create(**kwargs)
    room_id = getattr(resp, "room_id", None)
    if not room_id:
        print(f"!! Oda kurulamadı: {resp}", flush=True)
        return None
    STATE["created_rooms"].append(room_id)
    if reason_room_id:
        STATE["alt_rooms"][reason_room_id] = room_id
    save_state()
    push_history("sifresiz oda olusturuldu")
    print(f"✔ Şifresiz oda kuruldu: {room_id} → davet gönderildi: {invites}", flush=True)
    return room_id

# ---------------------------------------------------------------------------
# Olay işleyicileri
# ---------------------------------------------------------------------------

async def on_invite(room, event):
    """Oda davetlerini kabul et (sahip kısıtı varsa sadece sahibinden)."""
    try:
        if MATRIX_OWNER and event.sender != MATRIX_OWNER:
            print(f"Davet yoksayıldı (sahip değil): {event.sender} / {room.room_id}", flush=True)
            try:
                await client.room_leave(room.room_id)
            except Exception:
                pass
            return
        print(f"→ Oda daveti kabul ediliyor: {room.room_id} (davet eden: {event.sender})", flush=True)
        await client.join(room.room_id)
    except Exception:
        traceback.print_exc()

async def on_encrypted_message(room, event):
    """Şifreli oda: mesajı OKUYAMAYIZ; şifresiz oda kurup herkesi davet et."""
    if event.sender == client.user_id:
        return
    if room.room_id not in STATE["encrypted_rooms"]:
        STATE["encrypted_rooms"].append(room.room_id)
        save_state()
    existing = STATE["alt_rooms"].get(room.room_id)
    if existing:
        print(f"(Şifreli oda {room.room_id} — zaten şifresiz odamız var: {existing})", flush=True)
        return
    # Not: matrix-nio 0.26'da üyeler room.users sözlüğünde tutulur (anahtarlar user ID).
    members = list((getattr(room, "users", None) or {}).keys())
    invites = members + ([MATRIX_OWNER] if MATRIX_OWNER else [])
    print(f"🔒 Şifreli oda algılandı ({room.room_id}). Şifresiz oda kuruluyor…", flush=True)
    await create_plain_room(invites, reason_room_id=room.room_id)

async def on_message(room, event):
    """Normal (şifresiz) oda mesajı: Groq ile cevapla."""
    if not isinstance(event, RoomMessageText):
        return
    if event.sender == client.user_id:
        return
    if event.event_id in STATE["seen"]:
        return
    mark_seen(event.event_id)

    # Çok eski mesajlara (bot kapalıyken 7 günden fazla geçmiş) cevap verme.
    if event.server_timestamp and \
       time.time() * 1000 - event.server_timestamp > MAX_MESSAGE_AGE_MS:
        return

    if room_is_encrypted(room):
        # Şifreli odaya düz metaj düşmüş — okunamayabilir, şifresiz odaya yönlendir.
        await on_encrypted_message(room, event)
        return

    sender = event.sender
    if MATRIX_OWNER and sender != MATRIX_OWNER:
        print(f"(Mesaj yoksayıldı, sahip değil: {sender})", flush=True)
        return

    body = (event.body or "").strip()
    if not body:
        return

    print(f"← [{sender}] {body[:120]}", flush=True)

    # Basit komutlar
    low = body.lower()
    if low in ("!reset", "/reset", "!temizle", "/temizle"):
        STATE["messages"] = []
        save_state()
        push_history("gecmis temizlendi")
        await send_text(room.room_id, "🧹 Sohbet geçmişi temizlendi.", event.event_id)
        return
    if low in ("!durum", "/durum", "!status", "/status"):
        up = int(time.time() - STARTED_AT)
        durum = (
            f"⏱️ Bu oturumda {up // 60} dk {up % 60} sn'dir dinliyorum\n"
            f"🤖 Model: {GROQ_MODEL}\n"
            f"📜 Geçmişte {len(STATE['messages'])} mesaj var\n"
            f"👤 {'sadece ' + MATRIX_OWNER + ' için' if MATRIX_OWNER else 'herkese'} cevap veriyorum"
        )
        await send_text(room.room_id, durum, event.event_id)
        return

    async with send_lock:
        try:
            await client.room_typing(room.room_id, True, timeout=30000)
        except Exception:
            pass
        try:
            reply = groq_reply(STATE["messages"] + [{"role": "user", "content": body}])
        except Exception as e:
            await send_text(room.room_id, f"⚠️ Groq'a ulaşamadım: {e}", event.event_id)
            return
        # Önce cevabı gönder; başarırsa geçmişi kaydet (crash olursa cevap gitmiş olur).
        await send_text(room.room_id, reply, event.event_id)
        STATE["messages"].append({
            "role": "user", "content": body,
            "mx": sender, "room": room.room_id,
            "ts": int(event.server_timestamp or time.time() * 1000),
        })
        STATE["messages"].append({
            "role": "assistant", "content": reply,
            "room": room.room_id, "ts": int(time.time() * 1000),
        })
        save_state()
        push_history("sohbet guncellendi")
        try:
            await client.room_typing(room.room_id, False)
        except Exception:
            pass
        print(f"→ cevap gönderildi ({len(reply)} karakter)", flush=True)

async def on_room_event(room, event):
    """Tüm oda olaylarını tek noktadan dağıt (hata burada yutulur)."""
    try:
        if isinstance(event, MegolmEvent):
            await on_encrypted_message(room, event)
        else:
            await on_message(room, event)
    except Exception:
        traceback.print_exc()

# ---------------------------------------------------------------------------
# Ana döngü
# ---------------------------------------------------------------------------

async def main():
    global client, send_lock
    if not MATRIX_TOKEN:
        sys.exit("!! MATRIX_TOKEN eksik. Repo Secrets'a MATRIX_TOKEN ekleyin.")
    if not GROQ_API_KEY:
        sys.exit("!! GROQ_API_KEY eksik. Repo Secrets'a GROQ_API_KEY ekleyin.")

    client = AsyncClient(HOMESERVER, MATRIX_USER)
    client.access_token = MATRIX_TOKEN

    who = await client.whoami()
    user_id = getattr(who, "user_id", None)
    if not user_id:
        sys.exit(f"!! Matrix token geçersiz görünüyor: {who}")
    if MATRIX_USER and MATRIX_USER != user_id:
        print(f"UYARI: MATRIX_USER={MATRIX_USER} ama token {user_id} hesabına ait. "
              f"{user_id} kullanılacak.", flush=True)
    client.user_id = user_id
    print(f"✔ Matrix bağlantısı kuruldu: {user_id}", flush=True)

    send_lock = asyncio.Lock()
    client.add_event_callback(on_room_event, (RoomMessageText, MegolmEvent))
    client.add_invite_callback(on_invite)

    sync_filter = {"room": {"timeline": {"limit": 50}}}

    # İlk senkronizasyon: mevcut odalar + bot kapalıyken gelen mesajlar
    print("İlk senkronizasyon…", flush=True)
    await client.sync(0, sync_filter=sync_filter)
    rooms = list(client.rooms.values())
    print(f"📍 Bot {len(rooms)} odada bulunuyor.", flush=True)
    for r in rooms:
        enc = "🔒 şifreli" if room_is_encrypted(r) else "açık"
        print(f"   - {r.room_id} ({enc}, {getattr(r, 'member_count', '?')} üye)", flush=True)

    # Sahip tanımlıysa ve henüz hiç ortak odamız yoksa: ona özel şifresiz oda kur.
    if MATRIX_OWNER and not STATE["created_rooms"]:
        owner_in_room = any(
            MATRIX_OWNER in (getattr(r, "members", set()) or set()) for r in rooms
        )
        if not owner_in_room:
            print(f"✔ {MATRIX_OWNER} için şifresiz sohbet odası kuruluyor…", flush=True)
            await create_plain_room([MATRIX_OWNER])
        else:
            print(f"({MATRIX_OWNER} zaten ortak bir odada; yeni oda kurulmadı.)", flush=True)

    deadline = time.monotonic() + RUN_MINUTES * 60
    stopping = {"v": False}

    def _stop(*_a):
        stopping["v"] = True

    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _stop)
        except NotImplementedError:
            pass

    end_time = time.strftime("%H:%M:%S", time.localtime(time.time() + RUN_MINUTES * 60))
    print(f"👂 Dinleme başladı: {RUN_MINUTES} dakika (planlanan bitiş {end_time}). "
          f"Çıkmak için Ctrl+C / işi iptal edin.", flush=True)

    while not stopping["v"] and time.monotonic() < deadline:
        try:
            await client.sync(30000, sync_filter=sync_filter)
        except Exception as e:
            print("Sync hatası (5 sn sonra tekrar):", e, flush=True)
            await asyncio.sleep(5)

    print("Kapanış: geçmiş kaydediliyor…", flush=True)
    save_state()
    push_history("oturum sonu")
    await client.close()
    print("✔ Oturum tamamlandı.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
