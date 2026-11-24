"""Ready-made videos from a GitHub content repo, scheduled on their own.

Claude makes the story videos in the cloud and pushes them, with their caption and
posting time, to a content repository (``feed.repo``/``feed.branch``, by default kibassj0-design/relss, branch qisas).
The repo holds ``ready/index.json``:

    [{"id": "2026-11-21-onoda", "video": "ready/2026-11-21-onoda.mp4",
      "at": "2026-11-21T20:37:00+03:00", "title": "...", "caption": "... #hashtags",
      "platforms": ["youtube", "tiktok"]}]

Every few minutes this checks the index; each new id is downloaded once into
data/scheduled/ and added to the schedule with that caption and time, exactly as
if it had been added by hand in the Schedule tab. A time already past posts right
away. Ids already taken are remembered in data/feed_seen.json.
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

from . import config

log = logging.getLogger("autopost.feed")
SEEN = "feed_seen.json"


def _raw(cfg, path):
    f = cfg.get("feed", {})
    return f"https://raw.githubusercontent.com/{f.get('repo')}/{f.get('branch', 'qisas')}/{path}"


def _seen():
    try:
        return set(json.loads((config.DATA / SEEN).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _remember(seen):
    config.atomic_write(config.DATA / SEEN, json.dumps(sorted(seen)))


def _when(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value)).timestamp()


def check(app):
    """One pass: schedule every new item of the feed. Returns how many were added."""
    cfg = app.cfg
    r = requests.get(_raw(cfg, "ready/index.json"), params={"t": int(time.time())}, timeout=30)
    if r.status_code == 404:
        return 0
    r.raise_for_status()
    items = r.json()
    seen, added = _seen(), 0
    for item in items:
        iid = str(item.get("id") or "")
        if not iid or iid in seen or not item.get("video"):
            continue
        name = f"feed-{iid}{Path(item['video']).suffix or '.mp4'}"
        dst = app.schedule_dir() / name
        part = dst.with_suffix(".part")
        with requests.get(_raw(cfg, item["video"]), stream=True, timeout=120) as v:
            v.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in v.iter_content(1 << 20):
                    fh.write(chunk)
        part.replace(dst)
        at = max(_when(item.get("at") or time.time()), time.time() + 60)
        platforms = item.get("platforms") or None
        if platforms:  # only the platforms this install actually posts to
            platforms = [p for p in platforms if p in cfg["platforms"]] or None
        app.schedule_add(name, at, item.get("caption", ""), platforms,
                         note=item.get("note", ""), title=item.get("title", ""))
        seen.add(iid)
        _remember(seen)
        added += 1
        log.info("Feed: scheduled «%s» for %s", item.get("title") or iid,
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(at)))
    return added


def loop(app):
    while True:
        f = app.cfg.get("feed", {})
        if f.get("enabled") and f.get("repo"):
            try:
                check(app)
            except Exception as e:
                log.info("Feed check failed (will retry): %s", e)
        time.sleep(max(2, int(f.get("check_minutes", 10))) * 60)
