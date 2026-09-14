# 🚀 Hosting Kılavuzu (Tamamen Bedava, 7/24)

Botun çalışma modu otomatiktir: `PUBLIC_URL` varsa **webhook** (Render vb.), yoksa
**uzun-polling** (Oracle VM, GitHub Actions, yerel makine) kullanır. Kod hiçbir
değişiklik gerektirmez; sadece nerede çalıştıracağına karar ver.

## Seçenek 1 — Oracle Cloud Always Free (önerilen: gerçek 7/24, uyku yok) ✅

Oracle, Always Free paketinde kart doğrulaması isteyen ama **asla ücret almayan**
gerçek bir sunucu verir. Bot burada 7/24 uyumadan çalışır; hafıza da kalıcı diskte
durur (git push yedeği de ayrıca çalışır).

### Adım 1 — Oracle hesabı aç
1. [cloud.oracle.com](https://cloud.oracle.com) → **Start for free**.
2. Kredi/banka kartı istenecektir: bu **kimlik doğrulaması** içindir;
   Always Free limitleri içindeyken karttan asla para alınmaz.
   (Kart eklemeyi reddediyorsan → Seçenek 2'ye bak.)

### Adım 2 — Sunucu (instance) oluştur
1. **Home → Compute → Compute Instances → Create Instance**.
2. *Image*: **Ubuntu 22.04** (veya 24.04) LTS.
3. *Shape*: **"Always Free is supported"** kutusunu işaretle.
   - En kolay: `VM.Standard.E2.1.Micro` (1 OCPU, 1 GB RAM) — bu bot için fazlasıyla yeterli.
   - Varsa ARM `Ampere A1` şekilleri (2 OCPU/50 GB, 4 OCPU/24 GB) daha iyi;
     bazı bölgelerde bulunamaz, o zaman Micro yeterli.
4. *Networking*: varsayılan VCN kalabilir; **Public IPv4** işaretli olmalı.
5. *SSH keys*: **"Add SSH keys"** → kendi anahtarını seç/yükle.
   (Anahtarın yoksa kendi bilgisayarında: `ssh-keygen -t ed25519 -f ~/id_ed25519`
   → `cat ~/id_ed25519.pub` çıktısını buraya yapıştır.)
6. **Create** → birkaç dakika içinde IP hazır.

### Adım 3 — Kurulum (4 komut)
```bash
ssh ubuntu@<ORACLE_IP>
git clone https://github.com/buro-dev/ai-bot ~/ai-bot
cd ~/ai-bot
sudo bash deploy/oracle/setup.sh
```
Betik: sistem paketleri + Python ortamı + `.env` (değerleri sorar) + **systemd
servisi** kurar ve botu başlatır. Çökse bile 10 sn içinde kendiliğinden ayağa kalkar.

### Adım 4 — Kontrol
```bash
journalctl -u ai-bot -f        # logları izle
systemctl status ai-bot        # durum
```
Loglarda `Polling modunda başlatılıyor` görüldüyse Telegram'dan `/start` yaz. ✅

**Güncelleme:** `cd ~/ai-bot && git pull && sudo systemctl restart ai-bot`

> Not: Oracle'da port açmana, HTTPS'e, domain'e İHTİYAÇ YOK — bot Telegram'a kendi
> bağlanır (polling). Güvenlik duvarına dokunma.

---

## Seçenek 2 — GitHub Actions (yeni hesap SIFIR, kart SIFIR)

Eski sisteminin devamı: bot, GitHub'ın ücretsiz hesaplama dakikalarıyla ~5.5 saat
dinler, kendini yeniden tetikler; 6 saatlik cron güvenlik ağı vardır. Depo **public**
olduğu için dakika limiti bedava ve sınırsızdır. Tek kusur: her ~6 saatte bir
yeniden başlatma (1 dk civarı boşluk).

### Etkinleştirme
1. `deploy/alternative/bot.yml` dosyasını **`.github/workflows/bot.yml`** olarak
   kopyala ve push'la (GitHub arayüzünden de: Add file → Upload, `workflows/` altı).
2. Repo → **Settings → Secrets and variables → Actions** → yeni secret ekle:
   `TELEGRAM_TOKEN`, `ALLOWED_USER_ID`, `GROQ_API_KEY`, `GH_PAT_TOKEN`.
3. **Actions** sekmesinde "Telegram AI Bot (yedek)" akışı gör; bir kez
   **Run workflow** ile başlat. Kendisi devam ettirir.

> ⚠️ Aynı anda YALNIZCA BİR hosting aktif olmalı (Oracle VEYA Actions VEYA yerel).
> İki örnek aynı token ile bağlanırsa Telegram `Conflict` hatası verir.
> Actions'tan vazgeçersen `.github/workflows/bot.yml` dosyasını sil.

---

## Seçenek 3 — Kendi 7/24 açık makinende

```bash
git clone https://github.com/buro-dev/ai-bot
cd ai-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # doldur
python main.py         # polling modunda çalışır
```
Linux'ta arka planda tutmak istersen `deploy/oracle/setup.sh` içindeki systemd
parçasını aynen kullanabilirsin.

---

## Karşılaştırma

| | Oracle Cloud | GitHub Actions | Yerel makine |
|---|---|---|---|
| Ücret | 0 (kart sadece doğrulama) | 0 | 0 |
| 7/24 kesintisiz | ✅ uyku yok | ~6 saatte 1 dk boşluk | makine açıkken |
| Kurulum | ~20 dk | ~5 dk | ~10 dk |
| Yeni hesap | Oracle gerekir | gerekmez | gerekmez |
| Hafıza kalıcılığı | disk + git yedeği | git yedeği (disk geçici) | disk + git yedeği |

## Sorun giderme (ortak)

| Belirti | Çözüm |
|---|---|
| `Conflict: terminated by other getUpdates` | Aynı tokenla başka örnek çalışıyor (eski VM/Actions/yerel). Diğerini durdur. |
| `YAPILANDIRMA HATASI` | Eksik env değişkeni; mesajda hangisi eksik yazıyor. |
| "Groq API hız sınırı aşıldı" | Günlük kota (1000) doldu; yarın sıfırlanır. |
| Hafıza silindi | `GH_PAT_TOKEN` yetkili mi? Loglarda `git push başarısız` arar. |
| Oracle instance açılmıyor | Şekil seçilirken "Always Free is supported" filtresi açık mı? Bölge değiştir. |
