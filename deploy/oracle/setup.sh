#!/usr/bin/env bash
# =============================================================================
# Kişisel Telegram AI Botu — Oracle Cloud (Ubuntu) kurulum betiği
#
# Kullanım (Oracle VM'i içinde, root veya sudo ile):
#   1) ssh ubuntu@<oracle-ip>
#   2) git clone https://github.com/buro-dev/ai-bot ~/ai-bot
#   3) cd ~/ai-bot
#   4) sudo bash deploy/oracle/setup.sh
#
# .env yoksa değerleri sorar. İsteğe bağlı olarak tek satırda:
#   sudo TELEGRAM_TOKEN=... ALLOWED_USER_ID=... GROQ_API_KEY=... \
#        GITHUB_REPOSITORY=kullanici/depo GH_PAT_TOKEN=... \
#        bash deploy/oracle/setup.sh
# =============================================================================
set -euo pipefail

# Çalıştıran kullanıcı (betik sudo ile çalışıyorsa SUDO_USER'ü kullan)
RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
  RUN_USER="ubuntu"
fi
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
SERVICE_NAME="ai-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "HATA: Betik repo içinde çalıştırılmalı (git clone yapmayı unutma)." >&2
  exit 1
fi

echo "==> Sistem bağımlılıkları yükleniyor (python3, venv, git)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

# ---------------------------------------------------------------------------
# .env oluşturma
# ---------------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  echo ""
  echo "==> .env dosyası oluşturuluyor (Enter = atla; boş bırakılıyorsa varsayılan kullanılır)"
  if [ -z "${TELEGRAM_TOKEN:-}" ]; then read -r -p "  TELEGRAM_TOKEN (BotFather): " TELEGRAM_TOKEN; fi
  if [ -z "${ALLOWED_USER_ID:-}" ]; then read -r -p "  ALLOWED_USER_ID (@userinfobot): " ALLOWED_USER_ID; fi
  if [ -z "${GROQ_API_KEY:-}" ]; then read -r -p "  GROQ_API_KEY (console.groq.com/keys): " GROQ_API_KEY; fi
  if [ -z "${GITHUB_REPOSITORY:-}" ]; then read -r -p "  GITHUB_REPOSITORY (kullanici/depo): " GITHUB_REPOSITORY; fi
  if [ -z "${GH_PAT_TOKEN:-}" ]; then read -r -p "  GH_PAT_TOKEN (kalıcı hafıza için, atlanabilir): " GH_PAT_TOKEN; fi

  if [ -z "${TELEGRAM_TOKEN:-}" ] || [ -z "${ALLOWED_USER_ID:-}" ] || [ -z "${GROQ_API_KEY:-}" ]; then
    echo "HATA: TELEGRAM_TOKEN, ALLOWED_USER_ID ve GROQ_API_KEY zorunludur." >&2
    exit 1
  fi

  cat > "$ENV_FILE" <<EOF
# Bu dosya kurulum betiği tarafından oluşturuldu.
TELEGRAM_TOKEN=${TELEGRAM_TOKEN}
ALLOWED_USER_ID=${ALLOWED_USER_ID}
GROQ_API_KEY=${GROQ_API_KEY}
GITHUB_REPOSITORY=${GITHUB_REPOSITORY:-}
GH_PAT_TOKEN=${GH_PAT_TOKEN:-}

# Oracle VM: poll modu (webhook/HTTPS gerekmez)
RUN_MODE=poll
LOG_LEVEL=INFO
EOF
  chmod 600 "$ENV_FILE"
  echo "==> $ENV_FILE oluşturuldu."
else
  echo "==> Mevcut .env kullanılıyor: $ENV_FILE"
fi

# ---------------------------------------------------------------------------
# Python ortamı
# ---------------------------------------------------------------------------
echo "==> Python sanal ortamı ve bağımlılıklar kuruluyor (birkaç dakika)..."
cd "$REPO_DIR"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# ---------------------------------------------------------------------------
# systemd servisi (7/24 çalışır, çökerse otomatik yeniden başlar)
# ---------------------------------------------------------------------------
echo "==> systemd servisi kuruluyor: $SERVICE_NAME"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Kisisel Telegram AI Botu
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
# Hafıza git push işlemleri için kimlik (opsiyonel)
Environment=GIT_TERMINAL_PROMPT=0

[Install]
WantedBy=multi-user.target
EOF
chown "$RUN_USER" "$REPO_DIR/.venv" -R || true
chown "$RUN_USER" "$ENV_FILE" 2>/dev/null || true
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 3

echo ""
echo "======================================================================"
echo " Kurulum tamam!"
echo "======================================================================"
echo "  Servis durumu:  systemctl status ${SERVICE_NAME}"
echo "  Logları izle:   journalctl -u ${SERVICE_NAME} -f"
echo "  Yeniden başlat: sudo systemctl restart ${SERVICE_NAME}"
echo ""
echo " Loglarda 'Bot başlatılıyor' ve 'Polling modunda başlatılıyor' görüyorsan"
echo " Telegram'dan bota /start yazıp deneyebilirsin."
echo "======================================================================"
