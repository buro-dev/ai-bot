# 🤖 Kişisel Telegram AI Asistan Botu

Sadece **senin** Telegram ID'ne cevap veren; internetten arama yapan, görsel analiz eden,
sesli mesajı metne çeviren, dosya okuyan, kalıcı hafızası ve zamanlanmış hatırlatıcısı olan
**tamamen bedava** AI botu. 7/24 çalışmak için en iyi seçenek **Oracle Cloud Always Free**
(sıfır kuruş, uyku yok) — alternatifler: **GitHub Actions** (yeni hesap yok) ve Render.
**Ayrıntılı kurulum: [DEPLOY.md](DEPLOY.md)**

## Özellikler

| Özellik | Nasıl |
| --- | --- |
| 🔒 Sadece sana cevap | `ALLOWED_USER_ID` dışındaki herkes **sessizce** yok sayılır |
| 💬 AI sohbet | Groq `gpt-oss-120b` (ücretsiz), model kapalıysa otomatik yedeğe geçer |
| 🌐 İnternet araması | Modelin kararına göre DuckDuckGo (anahtarsız, ücretsiz) veya `/search <sorgu>` |
| 🖼️ Görsel analizi | Fotoğraf/dosya görseli gönder, analiz etsin |
| 🎧 Sesli mesaj | Whisper (`whisper-large-v3-turbo`) ile metne çevirilir, sonra cevaplanır |
| 📄 Dosya okuma | PDF, metin, kod dosyaları okunup özetlenir |
| 🧠 Kalıcı hafıza | `chat_history.json` + **git push** (deploy'dan sonra bile kalır) |
| 📌 Kalıcı notlar | "hatırla: ..." veya `/remember` — sohbet geçmişi temizlense de notlar kalır |
| ⏰ Hatırlatıcılar | "yarın 08:00 hatırlat: ..." veya `/remind 08:00 mesaj` |
| ⚡ Hız | Hafıza dosya tabanlı ve sınırlı tutulur (son 40 mesaj); botu yavaşlatmaz |

## Mimari

```
Telegram ──► Hosting (Oracle Cloud VM / GitHub Actions / Render)
                 │  python main.py
                 ├─ (webhook modu ise) aiohttp: /health + /webhook
                 ├─ PTB:      güncelleme işleme (yalnızca izinli ID)
                 ├─ Groq:     gpt-oss-120b (metin + görsel + araçlar)
                 │             └─ araçlar: web_search / save_memory / set_reminder
                 ├─ DuckDuckGo: DDGS (anahtarsız arama)
                 ├─ Whisper:  sesli mesaj → metin
                 └─ git push: chat_history.json → "memory" dalı (kalıcılık)
```

**Çalışma modları:** `PUBLIC_URL` tanımlıysa **webhook** (Render), değilse **uzun-polling**
(Oracle VM / GitHub Actions / yerel makine). Oracle ve Actions'ta port/HTTPS/domain
gerekmez — bot Telegram'a kendisi bağlanır.

**Kalıcılık nasıl çalışıyor?** Geçici disk kullanan ortamlarda (Render, GitHub Actions)
dosya sistemi sıfırlanır; bot bu yüzden her hafıza değişikliğinde dosyayı `memory` dalına
force-push eder ve başlarken o daldan geri yükler. `memory` dalı hosting'e bağlı dal
olmadığı için bu push'lar yeniden dağıtım tetiklemez. Oracle gibi kalıcı diske sahip
ortamlarda hafıza zaten diskte durur, git push ek yedek olarak çalışır.

**Uyku modu nasıl çözüldü?**
- **Oracle Cloud**: uyku yok — 7/24 ayakta.
- **Render** (bölgenizde ücretsiz katman varsa): 15 dk boşta uyur; senin mesajın webhook
  ile uyanır (~30-60 sn ilk mesaj gecikmesi).
- **GitHub Actions**: ~5.5 saatlik dinleme + kendini yeniden tetikleme döngüsü (~6 saatte
  1 dk boşluk).

## Kurulum

### 1) Gerekli hesaplar (hepsi ücretsiz)

1. **Telegram botu:** [@BotFather](https://t.me/BotFather) → `/newbot` → **token**'ı not et.
2. **Kendi Telegram ID'n:** [@userinfobot](https://t.me/userinfobot)'a yaz → **ID**'ni not et.
3. **Groq API anahtarı:** [console.groq.com/keys](https://console.groq.com/keys) → anahtar oluştur.
4. **GitHub PAT (kalıcı hafıza için):** GitHub → Settings → Developer settings →
   *Fine-grained personal access tokens* → bu depo için **Contents: Read and write** yetkisi
   ile token oluştur.
5. **Render hesabı:** [render.com](https://render.com) (kredi kartı gerektirmez).

### 2) Yerelde deneme (isteğe bağlı)

```bash
git clone https://github.com/<kullanici>/ai-bot
cd ai-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # değerleri doldur
python main.py            # poll modunda başlar (PUBLIC_URL yoksa)
python main.py --self-test  # ağ gerektirmeyen iç testler
```

### 3) Hosting: [DEPLOY.md](DEPLOY.md) içine bak 👈

Kısa yollar:

- **Oracle Cloud (önerilen, gerçek 7/24):** VM aç → `sudo bash deploy/oracle/setup.sh` →
  bitmiş. (~20 dk, kart sadece hesap doğrulaması, ücret SIFIR.)
- **GitHub Actions (yeni hesap yok):** `deploy/alternative/bot.yml` →
  `.github/workflows/bot.yml` olarak kopyala + 4 secret ekle.
- **Render (bölgenizde ücretsiz katman varsa):** repo'yu bağla (`render.yaml` otomatik
  algılanır), env değişkenlerini ekle, deploy et.

Render env değişkenleri (Render kullanırsan):

| Değişken | Değer |
| --- | --- |
| `TELEGRAM_TOKEN` | BotFather token'ı |
| `ALLOWED_USER_ID` | Senin Telegram ID'n |
| `GROQ_API_KEY` | Groq anahtarın |
| `GITHUB_REPOSITORY` | `kullanici/depo` |
| `GH_PAT_TOKEN` | Fine-grained PAT (contents: write) |

> ⚠️ Aynı anda **yalnızca bir** hosting aktif olmalı (ikisi aynı tokenla bağlanırsa
> Telegram `Conflict` hatası verir).
> ⚠️ GitHub Actions/Render gibi geçici diskte `GH_PAT_TOKEN` yoksa hafıza her
> yeniden başlatmada sıfırlanır. Kalıcılık için PAT gerekli.

## Kullanım

Serbest dille:

- `internette ara: bugün Balıkesir hava durumu` → DuckDuckGo araması + kaynaklı özet
- Görsel gönder → analiz
- Sesli mesaj at → "🎧 dinliyorum..." → metin + cevap
- PDF/txt/kod dosyası gönder → özet
- `hatırla: ofisim Kadıköy'de` → kalıcı not (sistem istemine her mesajda girer)
- `yarın 08:00 hatırlat: toplantı var` → zamanlanmış mesaj

Komutlar:

| Komut | İşlev |
| --- | --- |
| `/search <sorgu>` | Zorunlu internet araması + özet |
| `/remember <not>` | Kalıcı not ekle |
| `/memory` | Kalıcı notları listele |
| `/forget <no>` | Notu sil |
| `/remind [yarın] 08:00 <mesaj>` | Hatırlatıcı kur (saat geçtiyse ertesi gün) |
| `/clear` | Sohbet geçmişini temizle (notlar ve hatırlatıcılar korunur) |
| `/status` | Model, sayaçlar, hafıza ve git durumu |
| `/id` | Telegram kimliklerin |
| `/help` | Yardım |

## Ayarlar (ortam değişkenleri)

Tümü opsiyonel; tam liste `.env.example` içinde. Sık kullanılanlar:

| Değişken | Varsayılan | Açıklama |
| --- | --- | --- |
| `RUN_MODE` | `auto` | `poll` / `webhook` / `auto` |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Birincil model |
| `GROQ_FALLBACK_MODELS` | `openai/gpt-oss-20b` | Yedek modeller (virgülle) |
| `SYSTEM_PROMPT` | yerleşik TR asistan | Botun kişiliği |
| `MAX_HISTORY_MESSAGES` | `40` | Prompt'a giren son mesaj sayısı |
| `MAX_PROMPT_CHARS` | `24000` | Prompt karakter bütçesi |
| `SEARCH_REGION` | `tr-tr` | DuckDuckGo bölgesi |
| `TIMEZONE` | `Europe/Istanbul` | Hatırlatıcıların saat dilimi |
| `VOICE_ENABLED` / `SEARCH_ENABLED` / `REMINDERS_ENABLED` / `FILES_ENABLED` | `1` | Özellik anahtarları |
| `GIT_MEMORY_BRANCH` | `memory` | Hafıza dalı (Render'a bağlı dal olmasın!) |
| `DRY_RUN` | `0` | Telegram'a yazmadan logla (test) |

## Maliyet

**0 TL / $.**

- Hosting: Oracle Cloud Always Free (0 $, kart sadece doğrulama) veya GitHub Actions
  (public depoda sınırsız bedava dakika) veya Render ücretsiz katman (bölgeye göre)
- Groq: ücretsiz katman (gpt-oss-120b: ~1000 istek/gün, 30/dk)
- DuckDuckGo (DDGS): anahtarsız, ücretsiz

## Sorun giderme

| Belirti | Çözüm |
| --- | --- |
| Bot hiç cevap vermiyor | `ALLOWED_USER_ID` doğru mu? Loglarda "İzin verilmeyen kullanıcı" var mı? |
| `YAPILANDIRMA HATASI` | Eksik env değişkeni; Render env bölümünü kontrol et |
| "Groq API hız sınırı aşıldı" | Ücretsiz kota (1000/gün) doldu; yarına sıfırlanır |
| Mesaj ~1 dk gecikmeli geldi | Servisi uyuydu ve ilk mesaj uyardı; normal. UptimeRobot ping'i ile önlenir |
| Hafıza deploy'dan sonra boş | `GH_PAT_TOKEN` eksik/yetkisiz. Loglarda "git push başarısız" arar |
| "Conflict: terminated by other getUpdates" | Aynı token ile başka bir örnek çalışıyor (eski Actions/yerel bot). Kapat |
| PDF özetlenemiyor | Taramalı (görsel) PDF'lerde OCR yok; dijital PDF kullan |

## Proje yapısı

```
main.py               # giriş noktası (poll/webhook karar verir)
bot/
  config.py           # env yapılandırması + loglama (anahtar maskeleme)
  ai.py               # Groq istemcisi (metin+görsel+araçlar, model yedekleme, Whisper)
  runner.py           # mesaj akışı, komutlar, araç döngüsü, git push
  memory.py           # MemoryStore (chat_history.json) + GitSync (memory dalı)
  web_search.py       # DuckDuckGo araması
  media.py            # medya indirme, PDF/metin çıkarma
  reminders.py        # APScheduler zamanlayıcı
  server.py           # aiohttp: /health + /webhook
  selftest.py         # python main.py --self-test
render.yaml           # Render Blueprint (bölgenizde ücretsiz katman varsa)
deploy/
  oracle/setup.sh     # Oracle Cloud tek komut kurulumu (systemd dahil)
  alternative/bot.yml # GitHub Actions yedek hostingi (etkinleştirmek için kopyala)
DEPLOY.md             # hosting kılavuzu (Oracle / Actions / yerel)
```

## Güvenlik

- Yalnızca `ALLOWED_USER_ID` yanıtlanır; diğer kullanıcılar loglanır ve sessizce yok sayılır.
- Token/anahtarlar hiçbir şekilde koda ve loglara yazılmaz (log filtresi maskeler).
- Webhook secret'ı her başlatmada otomatik üretilir; Telegram `X-Telegram-Bot-Api-Secret-Token`
  başlığı ile doğrulanır.
- Git push için yalnızca bu depoya özel, minimum yetkili PAT kullanılmalıdır.

## Geçmiş

Eski sürüm, botu 6 saatte bir kendini yeniden tetikleyen bir GitHub Actions döngüsüyle
çalıştırıyordu (`bot.py` + `.github/workflows/bot.yml`); tek dosyalı, metin-only idi.
Yeni sürüm bu çalışma modelini `deploy/alternative/bot.yml` içinde (yeni özelliklerle)
sürdürülebilir hâle getirdi; eski dosyalar git geçmişinden (`git log`) geri alınabilir.
