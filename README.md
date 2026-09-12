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

## 2) GitHub Actions ile çalıştırma (6 saatte bir kendini yenileyen kesintisiz döngü)

* Her çalıştırma **5.5 saat** boyunca uzun-polling ile mesaj dinler:
  `python bot.py --mode loop --loop-minutes 330` (`timeout-minutes: 350`).
  Yanıtlar anında gönderilir; işin sonu gelmeden yeni bir run ile uğraşmaya gerek yoktur.
* Job'un **son adımı akışı kendini yeniden tetikler**:
  `gh workflow run bot.yml -f mode=loop -f loop_minutes=330` (`if: always()`).
  Böylece çalıştırmalar zincirleme şekilde 6 saatte bir kendini yenileyen,
  kesintisiz bir dinleme döngüsü oluşturur.
* **`cron: 0 */6 * * *` (UTC) güvenlik ağıdır:** kendinden-tetikleme zinciri bozulursa
  (yetki/API sorunu, silinen run vb.) 6 saatlik sınırda GitHub kendiliğinden yeni
  bir çalıştırma başlatır.
* `permissions: contents: write` (geçmişi repoya push) + `actions: write`
  (kendini yeniden tetikleme) tanımlıdır.
* `concurrency: telegram-ai-bot` (`cancel-in-progress: false`): aynı anda tek örnek
  çalışır; üst üste düşen çalıştırmalar kuyruğa girer, iptal edilmez.
* `GIT_PUSH_AFTER_EACH_MESSAGE=1`: her yanıtlandıktan sonra `chat_history.json`
  derhal commit+push edilir (en kısa aralık `GIT_PUSH_MIN_INTERVAL=15` sn); oturum
  yarıda kalsa bile geçmiş repoda güncel kalır. Uzun oturumlarda `HEARTBEAT_SECONDS`
  (vars. 60) aralıklarla loglara heartbeat kaydı (geçen/kalan süre, sayaçlar) düşülür.
* İş akışı önce `python -m py_compile bot.py` ve `python bot.py --self-test` adımlarını
  çalıştırır; ardından botu çalıştırır.
* Elle çalıştırma: **Actions → Telegram AI Bot → Run workflow** (`mode`: loop/once,
  `loop_minutes`: dinleme süresi).
* Bot sadece `chat_history.json` dosyasını commit'ler; başka dosyalara dokunmaz.
* PAT ile push ederken `actions/checkout`'un repoya yazdığı
  `http.https://github.com/.extraheader` kaydı otomatik silinir (aksi halde git iki
  `Authorization` başlığı gönderir, GitHub reddeder); PAT başarısız olursa checkout
  kimliği (`GITHUB_TOKEN`) ile tekrar denenebilir.

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
python bot.py --mode once --dry-run         # yanıtı göndermeden dene (sadece loglar;
                                             # dosya ve repo DEĞİŞTİRİLMEZ)
python bot.py --no-git                      # commit/push yapma
```

Uzun döngüler için (`.env.example` içinde de örnekli):
`GIT_PUSH_AFTER_EACH_MESSAGE=1` her mesajdan sonra otomatik commit+push eder
(`GIT_PUSH_MIN_INTERVAL`, vars. 15 sn), `HEARTBEAT_SECONDS` (vars. 60) uzun
oturumlarda heartbeat log aralığını belirler.

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

* **Model:** Varsayılan model `openai/gpt-oss-120b`. Model kullanılamazsa sırayla
  `openai/gpt-oss-20b` ve `llama-3.3-70b-versatile` yedekleri denenir (`GROQ_MODEL`,
  `GROQ_FALLBACK_MODELS` ile değiştirilebilir; `GROQ_DISABLE_FALLBACK_MODELS=1` ile kapatılır).
  Not: Groq, `llama-3.3-70b-versatile` modelini **16 Ağustos 2026**'da kullanımdan kaldırdı
  ([deprecations](https://console.groq.com/docs/deprecations)); bu yüzden yalnızca yedek listede.
  Kullanılamayan bir model denendiğinde logda "Groq modeli kullanılamıyor" uyarısı görünür.
* **Yetki:** Bot yalnızca `ALLOWED_USER_ID` ile eşleşen mesajlara yanıt verir; diğerleri loglanıp yok sayılır.
* **Geçmiş dosyası:** Yazma işlemi atomiktir (geçici dosya + `os.replace`). Dosya bozuksa
  `chat_history.json.bozuk-yedek` olarak yedeklenir ve geçmiş sıfırdan başlar.
* **Gizlilik:** Loglarda token/anahtarlar maskelenir (`***GIZLI***`).
* **Çıkış kodları:** `0` başarılı, `2` Telegram/yapılandırma hatası, `3` geçmiş push edilemedi, `130` Ctrl+C.
* **Tavsiye:** Aynı token'ı aynı anda iki yerde (Actions + yerel) çalıştırmayın; Telegram
  `Conflict` hatası verir.
