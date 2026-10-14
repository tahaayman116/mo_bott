#!/usr/bin/env bash
# Central Bot — one-command Termux setup (client-proof).
# Run this single line on a fresh Termux:
#   pkg install -y curl && curl -fsSL -o setup.sh https://raw.githubusercontent.com/tahaayman116/mo_bott/main/central-bot/termux-setup.sh && bash setup.sh
set -u

REPO_URL="https://github.com/tahaayman116/mo_bott.git"
SUBDIR="central-bot"

say() { printf '%s\n' "$*"; }
ask() { printf '%s ' "$*"; }
# NOTE: always read from /dev/tty (the keyboard), never stdin:
# the script may arrive via a pipe and stdin would be the download, not you.
tty_read() { read -r "$1" < /dev/tty; }

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    say "ناقص: $1 — هثبته دلوقتي."
    return 1
  fi
  return 0
}

say "=============================================="
say "  تثبيت البوت المركزي (خطوة واحدة)"
say "=============================================="

# ---- 1) system packages ----
if command -v pkg >/dev/null 2>&1; then
  say "[1/5] تثبيت بايثون و git..."
  pkg update -y >/dev/null 2>&1 || true
  pkg install -y python git >/dev/null 2>&1 || pkg install -y python git
else
  say "ده مش Termux؟ لازم pkg. وقف."
  exit 1
fi

# ---- 2) get the code (safe to re-run) ----
if [ ! -f main.py ]; then
  if [ ! -d "$SUBDIR" ]; then
    say "[2/5] تحميل الكود..."
    git clone --depth 1 "$REPO_URL" tmpbot >/dev/null 2>&1
    mv "tmpbot/$SUBDIR" "./$SUBDIR"
    rm -rf tmpbot
  fi
  cd "$SUBDIR" || exit 1
else
  say "[2/5] الكود موجود ✅"
fi

have_pgrep() { command -v pgrep >/dev/null 2>&1; }
bot_running() {
  have_pgrep && pgrep -f "central-bot.*main.py" >/dev/null 2>&1
}

# ---- 3) python env ----
say "[3/5] تثبيت المكتبات (دقيقة)..."
if [ ! -x .venv/bin/python ]; then
  python -m venv .venv
fi
.venv/bin/pip install --quiet -r requirements.txt

# ---- 4) keys (ask once, validate, save) ----
say "[4/5] المفاتيح (مرة واحدة بس):"
if [ -f .env ]; then
  cp .env .env.bak
  say "(موجود .env قديم — اتنسخ احتياطي .env.bak)"
fi

API_ID=""
while :; do
  ask "API_ID (أرقام من my.telegram.org/apps):"
  tty_read API_ID
  case "$API_ID" in
    ''|*[!0-9]*)
      say "لازم أرقام بس. حاول تاني."
      ;;
    *) break ;;
  esac
done

API_HASH=""
while :; do
  ask "API_HASH (حروف من نفس الصفحة):"
  tty_read API_HASH
  if [ "${#API_HASH}" -lt 10 ]; then
    say "قصير أوي. انسخه كامل وحاول تاني."
  else
    break
  fi
done

TOKEN=""
while :; do
  ask "توكن البوت (من @BotFather بعد /newbot):"
  tty_read TOKEN
  case "$TOKEN" in
    *:*????????*) break ;;
    *) say "التوكن شكله رقم:حروف. حاول تاني." ;;
  esac
done

OWNER=""
while :; do
  ask "رقمك العددي (من @userinfobot):"
  tty_read OWNER
  case "$OWNER" in
    ''|*[!0-9]*)
      say "لازم أرقام بس. حاول تاني."
      ;;
    *) break ;;
  esac
done

{
  echo "API_ID=$API_ID"
  echo "API_HASH=$API_HASH"
  echo ""
  echo "CONTROL_BOT_TOKEN=$TOKEN"
  echo "OWNER_ID=$OWNER"
  echo ""
  echo "DB_PATH=data/central.db"
  echo "SESSIONS_DIR=sessions"
  echo "POST_DAILY_CAP=100"
  echo "LOG_LEVEL=INFO"
} > .env
say "اتحفظ .env ✅"

# ---- 5) run ----
say "[5/5] التشغيل..."
mkdir -p data sessions logs
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true
if bot_running; then
  say "البوت شغال أصلًا — مش هشغل نسخة تانية."
else
  nohup .venv/bin/python main.py > bot.log 2>&1 &
  sleep 8
  if bot_running || ! have_pgrep; then
    say "البوت شغال ✅"
  else
    say "مشكلة في التشغيل — شوف آخر اللوج:"
    tail -n 20 bot.log 2>/dev/null || true
    exit 1
  fi
fi

say "=============================================="
say " خلص! افتح بوت التحكم وابعت /start"
say " إضافة رقم: الأرقام -> إضافة رقم -> الكود"
say "=============================================="
