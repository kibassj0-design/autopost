"""Watch recent posts; when one takes off, cut a follow-up clip and post it."""
import logging
import time
from pathlib import Path

from . import pipeline, store, video
from .platforms import ALL

log = logging.getLogger("autopost.momentum")
_warned = set()


def velocity(db, post, now_stats, now):
    """Views per hour since the last snapshot at least 15 minutes old, or since
    publishing when this is the first look."""
    prev = store.last_stats(db, post["id"], now - 900)
    if prev and prev["views"] is not None:
        hours, gained = (now - prev["ts"]) / 3600, now_stats["views"] - prev["views"]
    else:
        hours, gained = (now - post["published_at"]) / 3600, now_stats["views"]
    return gained / max(hours, 0.25)


def check(cfg, db=None, dry_run=False, cut=True, boost=True):
    """Read the numbers of recent posts (always), and act on a hot one (boost=True).
    cut=False: more clips are already queued, so a hot video should release the
    next one early instead of cutting a follow-up. Returns what was (or should be) boosted."""
    m = cfg["momentum"]
    db = db or store.connect()
    now = time.time()
    posts = db.execute(
        "SELECT p.*, v.source, v.title, v.boosts, v.parent_id FROM posts p"
        " JOIN videos v ON v.id = p.video_id"
        " WHERE p.remote_id IS NOT NULL AND p.published_at >= ?",
        (now - m["window_hours"] * 3600,)).fetchall()

    hot = {}  # video_id -> (row, best views/hour)
    for post in posts:
        try:
            s = ALL[post["platform"]].stats(post["remote_id"])
        except Exception as e:
            # Reading views needs extra permissions on some accounts; not worth a
            # warning every half hour for every post.
            if (post["platform"], post["id"]) not in _warned:
                _warned.add((post["platform"], post["id"]))
                log.info("%s views unavailable for post %s: %s", post["platform"], post["id"],
                         str(e)[:160])
            continue
        if not s:
            continue
        if s.get("views") is None:  # likes and comments only (no views permission)
            store.add_stats(db, post["id"], s)
            continue
        rate = velocity(db, post, s, now)
        store.add_stats(db, post["id"], s)
        log.info("%s | %s | %d views | %.0f views/h",
                 post["platform"], post["title"], s["views"], rate)
        if rate >= m["views_per_hour"] and rate > hot.get(post["video_id"], (None, 0))[1]:
            hot[post["video_id"]] = (post, rate)

    boosted = []
    if not boost:
        return boosted
    for video_id, (post, rate) in hot.items():
        if post["parent_id"] is not None:
            continue  # a follow-up doesn't spawn its own follow-ups
        if post["boosts"] >= m["max_boosts_per_video"]:
            continue
        if not cut:
            log.info("MOMENTUM: %s at %.0f views/h on %s -> releasing the next queued clip",
                     post["title"], rate, post["platform"])
            if not dry_run:
                db.execute("UPDATE videos SET boosts = boosts + 1 WHERE id = ?", (video_id,))
                db.commit()
            boosted.append((post["title"], rate, None))
            continue
        source = Path(post["source"])
        if not source.exists():
            log.warning("Trending, but the source file is gone: %s", source)
            continue
        start = video.boost_window(video.probe(source)["duration"], cfg["max_seconds"],
                                   m["boost_clip_seconds"], post["boosts"])
        if start is None:
            log.info("%s is trending (%.0f views/h) but all of its footage is already "
                     "posted; not reposting the same clip", post["title"], rate)
            continue
        log.info("MOMENTUM: %s at %.0f views/h on %s -> posting a follow-up cut from %ds",
                 post["title"], rate, post["platform"], start)
        if dry_run:
            boosted.append((post["title"], rate, start))
            continue
        # Count the attempt first so a crash mid-upload can't cause a double post.
        db.execute("UPDATE videos SET boosts = boosts + 1 WHERE id = ?", (video_id,))
        db.commit()
        pipeline.publish(source, cfg, db=db, parent_id=video_id, boost_of=post["title"],
                         start=start, duration=m["boost_clip_seconds"])
        boosted.append((post["title"], rate, start))
    return boosted
