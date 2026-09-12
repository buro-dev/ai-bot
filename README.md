# ai-bot — Telegram + Groq sohbet botu

Telegram'dan gelen mesajları Groq API ile yanıtlayan, sohbet geçmişini `chat_history.json`
dosyasında tutan ve bu dosyayı otomatik olarak Git'e (`add` → `commit` → `push`) gönderen bot.

Webhook kullanmaz (uzun-polling kullanır), bu yüzden internete açık bir sunucu gerekmez:
GitHub Actions üzerinde zamanlanmış görev olarak da, bir VPS'te sürekli çalışacak şekilde de
kullanılabilir.

## Dosyalar

| Dosya | Görevi |
| --- | --- |
| `bot.py` | Botun tamamı (Telegram + Groq + geçmiş + Git otomasyonu). |
| `chat_history.json` | Sohbet geçmişi: `[{"role": "user|assistant", "content": "..."}]` |
| `requirements.txt` | `python-telegram-bot`, `groq` |
| `.github/workflows/bot.yml` | GitHub Actions iş akışı (Python 3.10 + zamanlanmış çalıştırma) |

## 1) Gerekli GitHub Secrets

Depo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Zorunlu | Açıklama |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | ✅ | BotFather'dan alınan token. |
| `ALLOWED_USER_ID` | ✅ | Botun yanıt vereceği tek kullanıcının Telegram ID'si (`@userinfobot` ile öğrenilir). |
| `GROQ_API_KEY` | ✅ | https://console.groq.com/keys adresinden alınan anahtar. |
| `GH_PAT_TOKEN` | ➖ (önerilir) | `chat_history.json` commit'ini geri push edebilmek için **içerik yazma** yetkili fine-grained PAT. |

`gh` CLI ile:

```bash
gh secret set TELEGRAM_TOKEN   --repo <kullanıcı>/<depo> --body "123456:AA..."
gh secret set ALLOWED_USER_ID  --repo <kullanıcı>/<depo> --body "123456789"
gh secret set GROQ_API_KEY     --repo <kullanıcı>/<depo> --body "gsk_..."
gh secret set GH_PAT_TOKEN     --repo <kullanıcı>/<depo> --body "github_pat_..."
```

> `gh` kullanmıyorsanız değerler kullanıcı girdisi olarak sisteme verilir; token'ları hiçbir
> zaman koda/dosyaya yazmayın.

## 2) GitHub Actions ile çalıştırma

* İş akışı **5 dakikada bir** (UTC) ve elle (**Actions → Telegram AI Bot → Run workflow**) tetiklenir.
* Elle çalıştırmada mod seçilebilir: `once` (tek tur) veya `loop` (5 dakika boyunca yeni mesaj bekler).
* İş akışı önce `python -m py_compile bot.py` ve `python bot.py --self-test` adımlarını çalıştırır;
  ardından `python bot.py --mode once` ile botu çalıştırır.
* Aynı token ile iki örnek çakışmasın diye `concurrency: telegram-ai-bot` tanımlıdır.
* Bot sadece `chat_history.json` dosyasını commit'ler; başka dosyalara dokunmaz.

> GitHub'ın `schedule` zamanlaması yoğun saatlerde birkaç dakika gecikebilir. Yanıt süresini
> hızlandırmak için `--mode loop` kullanın veya botu bir VPS'te sürekli çalıştırın.

## 3) Yerel çalıştırma

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

export TELEGRAM_TOKEN="123456:AA..."
export ALLOWED_USER_ID="123456789"
export GROQ_API_KEY="gsk_..."
export GH_PAT_TOKEN="github_pat_..."        # push için (istenmiyorsa --no-git)

python bot.py --self-test                   # ağ gerektirmeyen kontroller
python bot.py                               # auto: yerelde sürekli (polling)
python bot.py --mode once                   # tek tur (cron/elle)
python bot.py --mode loop --loop-minutes 10 # 10 dk boyunca mesaj bekle
python bot.py --mode once --dry-run         # yanıtı göndermeden dene (sadece loglar)
python bot.py --no-git                      # commit/push yapma
```

Ayarlar `.env` dosyasından da okunur (`.env.example` şablonuna bakın).

## 4) Komutlar

| Komut | Açıklama |
| --- | --- |
| *(herhangi bir mesaj)* | Geçmişle birlikte Groq'a gönderilir, yanıt mesaja **cevap** olarak atılır. |
| `/start`, `/help` | Kısa bilgi. |
| `/status` | Aktif model, yedek modeller, geçmiş kayıt sayısı. |
| `/id` | Telegram kullanıcı/sohbet ID'niz (özellikle cron testlerinde kolaylık). |
| `/clear` | Sohbet geçmişini temizler (değişiklik repoya commit'lenir). |

## 5) Önemli notlar

* **Model:** Varsayılan model `llama-3.3-70b-versatile`. Groq bu modeli **16 Ağustos 2026**'da
  kullanımdan kaldırdı; bu yüzden bot, model kapalıysa sırayla `openai/gpt-oss-120b` ve
  `openai/gpt-oss-20b` modellerine düşer (`GROQ_MODEL`, `GROQ_FALLBACK_MODELS` ile değiştirilebilir).
  Kullanımdan kaldırılmış bir model denendiğinde logda "Groq modeli kullanılamıyor" uyarısı görünür.
* **Yetki:** Bot yalnızca `ALLOWED_USER_ID` ile eşleşen mesajlara yanıt verir; diğerleri loglanıp yok sayılır.
* **Geçmiş dosyası:** Yazma işlemi atomiktir (geçici dosya + `os.replace`). Dosya bozuksa
  `chat_history.json.bozuk-yedek` olarak yedeklenir ve geçmiş sıfırdan başlar.
* **Gizlilik:** Loglarda token/anahtarlar maskelenir (`***GIZLI***`).
* **Çıkış kodları:** `0` başarılı, `2` Telegram/yapılandırma hatası, `3` geçmiş push edilemedi, `130` Ctrl+C.
* **Tavsiye:** Aynı token'ı aynı anda iki yerde (Actions + yerel) çalıştırmayın; Telegram
  `Conflict` hatası verir.
