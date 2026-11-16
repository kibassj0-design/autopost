#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
VPY=.venv/bin/python
if [ ! -f .venv/installed.ok ] || ! cmp -s requirements.txt .venv/installed.ok \
    || ! "$VPY" -c "import anthropic, requests" 2>/dev/null; then
  echo "Installing autopost. This runs once and takes a few minutes."
  rm -f .venv/installed.ok
  [ -x "$VPY" ] || python3 -m venv .venv
  "$VPY" -m pip install --upgrade pip
  "$VPY" -m pip install -r requirements.txt
  cp requirements.txt .venv/installed.ok
fi
[ -f .env ] || cp .env.example .env
[ -f config.json ] || cp config.example.json config.json
# Exit code 3: the tool installed an update; start again (reinstalling packages
# if the list changed). A new launcher is swapped in before this script re-runs.
while true; do
  set +e
  "$VPY" -m autopost "$@"
  code=$?
  set -e
  [ "$code" = 3 ] || exit "$code"
  if [ -f start.sh.new ]; then mv -f start.sh.new start.sh; chmod +x start.sh; exec ./start.sh "$@"; fi
  if ! cmp -s requirements.txt .venv/installed.ok; then exec ./start.sh "$@"; fi
done
