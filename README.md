# ai-bot — Element (Matrix) + Groq Sohbet Botu

GitHub Actions üzerinde çalışan, Element/Matrix üzerinden konuştuğunuz,
cevapları Groq API'siyle üreten ve sohbet geçmişini `chat_history.json`
olarak repoya kaydeden bir sohbet botu.

## Nasıl çalışır?

1. Bot, `MATRIX_TOKEN` ile matrix.org'a bağlanır.
2. Oda davetlerini otomatik kabul eder.
3. Mesajları Groq'a gönderir (`llama-3.3-70b-versatile`), cevabı odaya yazar.
4. **Şifreli odaları okuyamaz** (Element'de "Sohbet başlat" ile açılan DM'ler
   varsayılan olarak uçtan uca şifrelidir). Bu durumda bot kendisi **şifresiz**
   bir "AI Bot Sohbet" odası kurup sizi oraya davet eder — daveti kabul edip
   o odada konuşursunuz.
5. Her cevaptan sonra geçmişi `chat_history.json`'a yazar ve repoya push eder.

## Kurulum

### 1) Secrets ekle

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret adı | Değer | Zorunlu? |
|---|---|---|
| `MATRIX_TOKEN` | Bot hesabının Element access token'ı (`mat_...`) | ✅ |
| `GROQ_API_KEY` | console.groq.com'dan aldığın anahtar (`gsk_...`) | ✅ |
| `MATRIX_USER` | Botun tam adresi (`@botadi:matrix.org`) — verilmezse otomatik bulunur | önerilir |
| `MATRIX_OWNER` | Senin ana hesabının adresi (`@sen:matrix.org`) — bot sadece sana cevap verir ve sana özel oda kurar | önerilir |
| `GH_PAT_TOKEN` | GitHub PAT — **gerekmez**, Actions'ın kendi token'ı push için yeterli | opsiyonel |

> Element'de User ID'yi bulmak: **Ayarlar → Yardım & Hakkında → Kullanıcı Kimliği**
> (Access Token'ı kopyaladığın yerin hemen üstü).

### 2) Çalıştır

- **Elle:** Repo → **Actions → Run Matrix Bot → Run workflow**. İstersen
  "minutes" girişine 5–340 arası bir süre yaz (boş bırakırsan 60 dk).
- **Otomatik:** Workflow her 6 saatte bir (`cron: 0 */6 * * *`) çalışır ve
  bot her seferinde `BOT_RUN_MINUTES` kadar dinler.

### 3) Element'de konuş

Bot çalışınca ya sizi "AI Bot Sohbet" odasına davet eder ya da mevcut
DM'inizde size bir davet bırakır. Daveti kabul edip mesaj yazın — bot
saniyeler içinde cevap verir.

Sohbet içindeki komutlar:

- `!reset` → sohbet geçmişini (botun hafızasını) temizler
- `!durum` → botun durumunu gösterir

## Dakika limiti (önemli!)

- Repo **private** ise: GitHub ücretsiz planı ayda **2.000 Actions dakikası**
  verir. Bu yüzden varsayılan ayar: 6 saatte bir × 15 dk = günde ~1 saat
  (ayda ~1.800 dk). Bot çevrimdışıyken yazdıklarınız sıraya girer; bot
  uyanınca hepsine sırayla cevap verir.
- Repo **public** ise dakika limiti yoktur: `bot.yml` içinde
  `BOT_RUN_MINUTES: '15'` değerini `'340'` yapın → bot ~7/24 çalışır.
  (Dikkat: public repoda `chat_history.json` herkes tarafından okunabilir.
  Token'lar Secrets'ta olduğundan asla görünmez.)

## Dosyalar

- `bot.py` — botun kendisi
- `chat_history.json` — sohbet geçmişi (bot otomatik günceller)
- `requirements.txt` — bağımlılıklar (`matrix-nio`, `groq`)
- `.github/workflows/bot.yml` — Actions otomasyonu

## Sorun giderme

- **Bot cevap vermiyor:** Actions → Run Matrix Bot → son çalışmanın loguna bak.
  `!! MATRIX_TOKEN eksik` → Secrets'u eklemedin. `!! Matrix token geçersiz`
  → token yanlış veya hesaptan çıkılmış (Element'de "Tüm oturumları kapat"
  token'ı geçersiz kılar; yeni token alıp Secret'u güncelle).
- **Mesaj attım ama davet gelmedi:** Bot zaten şifresiz bir oda kurduysa
  yeniden kurmaz; Element'de "AI Bot Sohbet" odasını ve *Invites/Davetler*
  bölümünü kontrol et.
- **Zamanlanmış çalışma durdu:** GitHub, 60 gün işlem olmayan repolarda
  zamanlamayı kapatır. Botun kendi push'ları repoyu canlı tutar; yine de
  durursa Actions sayfasından workflow'u yeniden etkinleştir.
