#!/usr/bin/env bash
# Termux setup for telegram-userbot.
# Run INSIDE the project folder after copying it to the phone:
#   chmod +x termux-setup.sh
#   ./termux-setup.sh
set -e

echo "== packages =="
pkg update -y
pkg install -y python git tmux nano unzip

echo "== virtualenv =="
if [ ! -d .venv ]; then
  python -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "== dependencies =="
pip install --upgrade pip
pip install -r requirements.txt

echo "== config =="
if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from example."
  echo "EDIT IT NOW with your API_ID / API_HASH from https://my.telegram.org/apps"
  echo "  nano .env"
else
  echo ".env already exists, left untouched."
fi
# Never reuse a session file from another device:
rm -f data/*.session data/*.session-journal 2>/dev/null || true

echo ""
echo "done ✅  next steps:"
echo "  1) nano .env        (API_ID + API_HASH, keep DRY_RUN=true at first)"
echo "  2) python -m pytest -q"
echo "  3) tmux new -s bot  ->  python main.py   (detach: Ctrl+B then D)"
echo "  4) termux-wake-lock (keeps Android from sleeping the CPU)"
