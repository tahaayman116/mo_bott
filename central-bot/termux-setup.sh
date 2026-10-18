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

# ---- 2) get the code (git-tracked, safe to re-run) ----
BASE="$HOME/mo_bott"
SUBDIR="central-bot"
# Migration from old flat install (no .git)
if [ -d "$HOME/central-bot" ] && [ -f "$HOME/central-bot/main.py" ] \
   && [ ! -d "$HOME/central-bot/.git" ]; then
  if [ ! -d "$BASE" ]; then
    mkdir -p "$BASE"
    mv "$HOME/central-bot" "$BASE/$SUBDIR"
  fi
fi
# Download the code (tarball — no git needed, more reliable on Termux)
# Always attempt fresh download so fixes propagate; fall back to existing on failure.
say "[2/5] تحميل/تحديث الكود..."
rm -rf /tmp/mo_bott_dl
mkdir -p /tmp/mo_bott_dl
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o /tmp/mo_bott_dl/repo.tar.gz \
    "https://github.com/tahaayman116/mo_bott/archive/refs/heads/main.tar.gz"
elif command -v wget >/dev/null 2>&1; then
  wget -q -O /tmp/mo_bott_dl/repo.tar.gz \
    "https://github.com/tahaayman116/mo_bott/archive/refs/heads/main.tar.gz"
fi
if [ -s /tmp/mo_bott_dl/repo.tar.gz ]; then
  tar -xzf /tmp/mo_bott_dl/repo.tar.gz -C /tmp/mo_bott_dl
  extracted=$(find /tmp/mo_bott_dl -maxdepth 2 -type d -name "central-bot" | head -1)
  if [ -n "$extracted" ] && [ -f "$extracted/main.py" ]; then
    mkdir -p "$BASE/$SUBDIR"
    cp -r "$extracted/." "$BASE/$SUBDIR/"
    rm -rf /tmp/mo_bott_dl
    say "تم تحديث الكود ✅"
  else
    say "فشل استخراج الكود — استخدم النسخة الموجودة."
  fi
else
  say "فشل التحميل — استخدم النسخة الموجودة (إن وجدت)."
fi
if [ ! -f "$BASE/$SUBDIR/main.py" ]; then
  say "لا يوجد كود — تحقق من النت."
  exit 1
fi
cd "$BASE/$SUBDIR" || { say "تعذر الدخول للمجلد."; exit 1; }
say "[2/5] تحديث الكود (git pull)..."
git pull --ff-only 2>/dev/null || true
# backward-compatible symlink so old instructions (cd ~/central-bot) still work
[ -e "$HOME/central-bot" ] || ln -s "$BASE/$SUBDIR" "$HOME/central-bot" 2>/dev/null || true

have_pgrep() { command -v pgrep >/dev/null 2>&1; }
bot_running() {
  # Prefer the pid file the bot itself writes (reliable across Termux).
  if [ -f logs/bot.pid ]; then
    local p; p=$(tr -dc '0-9' < logs/bot.pid 2>/dev/null)
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then return 0; fi
  fi
  have_pgrep && pgrep -f "main\.py" >/dev/null 2>&1
}

# ---- 3) python env ----
say "[3/5] تثبيت المكتبات (دقيقة)..."
if [ ! -x .venv/bin/python ]; then
  python -m venv .venv
fi
.venv/bin/pip install --quiet -r requirements.txt

# ---- 4) keys (ask once, validate, save) ----
say "[4/5] المفاتيح:"
if [ -f .env ] && grep -q '^API_ID=' .env && grep -q '^API_HASH=' .env \
   && grep -q '^CONTROL_BOT_TOKEN=' .env && grep -q '^OWNER_ID=' .env; then
  say "موجود .env كامل — سأستخدمه (بدون سؤال)."
  say "للتعديل: امسح .env أو عدّله يدويًا."
else

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
fi

# ---- 5) run ----
say "[5/5] التشغيل..."
mkdir -p data sessions logs
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true
# stop any stale instance tracked by pid file first
if [ -f logs/bot.pid ]; then
  op=$(tr -dc '0-9' < logs/bot.pid 2>/dev/null)
  if [ -n "$op" ] && kill -0 "$op" 2>/dev/null; then
    say "إيقاف النسخة القديمة (PID $op)..."
    kill "$op" 2>/dev/null || true
    sleep 2
  fi
fi
if bot_running; then
  say "البوت شغال أصلًا — مش هشغل نسخة تانية."
else
  nohup .venv/bin/python main.py > bot.log 2>&1 &
  sleep 8
  if bot_running; then
    say "البوت شغال ✅"
    # verify token + show bot username
    if command -v curl >/dev/null 2>&1; then
      uname=$(curl -s "https://api.telegram.org/bot$TOKEN/getMe" 2>/dev/null \
              | sed -n 's/.*"username":"\([^"]*\)".*/@\1/p')
      [ -n "$uname" ] && say "بوت التحكم: $uname  (أرسل /start لهذا البوت)"
    fi
    say "--- آخر 12 سطر من bot.log ---"
    tail -n 12 bot.log 2>/dev/null || true
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
