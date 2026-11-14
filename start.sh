#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
VPY=.venv/bin/python
if [ ! -f .venv/installed.ok ] || ! "$VPY" -c "import anthropic, requests" 2>/dev/null; then
  echo "Installing autopost. This runs once and takes a few minutes."
  rm -f .venv/installed.ok
  [ -x "$VPY" ] || python3 -m venv .venv
  "$VPY" -m pip install --upgrade pip
  "$VPY" -m pip install -r requirements.txt
  touch .venv/installed.ok
fi
[ -f .env ] || cp .env.example .env
[ -f config.json ] || cp config.example.json config.json
exec "$VPY" -m autopost "$@"
