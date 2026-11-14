#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: installing, this takes a few minutes..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -r requirements.txt
fi
[ -f .env ] || cp .env.example .env
[ -f config.json ] || cp config.example.json config.json
exec .venv/bin/python -m autopost "$@"
