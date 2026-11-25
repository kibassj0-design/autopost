"""Keeps the tool from filling the disk: files it made and no longer needs are removed.

What goes (and when):
- data/edited/      platform copies (Instagram copy, Shorts copy): after 2 days; they are
                    made again if a retry needs them
- done/             published clips: after 7 days (retries and momentum follow-ups only
                    look back 3 days)
- failed/           clips that failed everywhere: after 14 days
- data/scheduled/   scheduled videos that were posted: 3 days after posting
- data/clips/       working folders of links: after 2 days, unless a job still uses them
When free space is low, the same runs with shorter ages.
Never touched: inbox/ (the queue), keys, accounts, settings, the database, backups.
"""
import json
import logging
import shutil
import time
from pathlib import Path

from . import config, store

log = logging.getLogger("autopost.cleanup")

DAY = 86400
LOW_SPACE = 5 * 1024 ** 3  # below this much free space, clean harder


def free_bytes():
    try:
        return shutil.disk_usage(config.ROOT).free
    except OSError:
        return None


def _old(path, age):
    """Last changed more than `age` seconds ago (a folder: its newest file)."""
    try:
        newest = path.stat().st_mtime
        if path.is_dir():
            newest = max([f.stat().st_mtime for f in path.rglob("*") if f.is_file()] or [newest])
        return time.time() - newest > age
    except OSError:
        return False


def _remove(path):
    size = 0
    try:
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            shutil.rmtree(path, ignore_errors=True)
        else:
            size = path.stat().st_size
            path.unlink(missing_ok=True)
    except OSError:
        pass
    return size


def run(urgent=None):
    """Remove what is no longer needed. Returns the bytes freed."""
    free = free_bytes()
    if urgent is None:
        urgent = free is not None and free < LOW_SPACE
    k = 0.25 if urgent else 1.0  # low on space: keep things for a quarter as long
    freed = 0
    root, data = config.ROOT, config.DATA

    for p in (data / "edited").glob("*") if (data / "edited").exists() else []:
        if _old(p, 2 * DAY * k):
            freed += _remove(p)
    for folder, days in (("done", 7), ("failed", 14)):
        d = root / folder
        for p in d.glob("*") if d.exists() else []:
            if _old(p, days * DAY * k):
                freed += _remove(p)

    # working folders of links, unless a job still uses them
    busy = set()
    try:
        for j in json.loads((data / "jobs.json").read_text(encoding="utf-8")):
            if j.get("work"):
                busy.add(str(j["work"]))
    except (OSError, ValueError):
        pass
    clips = data / "clips"
    for p in clips.iterdir() if clips.exists() else []:
        if str(p) not in busy and _old(p, 2 * DAY * k):
            freed += _remove(p)

    # scheduled videos already posted
    try:
        db = store.connect()
        rows = db.execute("SELECT path, at FROM schedule WHERE status='done' AND at < ?",
                          (time.time() - 3 * DAY * k,)).fetchall()
        db.close()
        for r in rows:
            p = Path(r["path"])
            if p.exists():
                freed += _remove(p)
                freed += _remove(p.with_suffix(".jpg"))
    except Exception as e:
        log.info("Schedule cleanup skipped: %s", e)

    for p in data.glob("cover-upload-*"):
        freed += _remove(p)
    if freed:
        log.info("Cleanup freed %.1f GB%s", freed / 1024 ** 3, " (low disk space)" if urgent else "")
    return freed
