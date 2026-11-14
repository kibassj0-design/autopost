"""Watch recent posts; when one takes off, cut a follow-up clip and post it."""
import logging
import time
from pathlib import Path

from . import pipeline, store, video
from .platforms import ALL

log = logging.getLogger("autopost.momentum")


def velocity(db, post, now_stats, now):
    """Views per hour since the last snapshot at least 15 minutes old, or since
    publishing when this is the first look."""
    prev = store.last_stats(db, post["id"], now - 900)
    if prev:
        hours, gained = (now - prev["ts"]) / 3600, now_stats["views"] - prev["views"]
    else:
        hours, gained = (now - post["published_at"]) / 3600, now_stats["views"]
    return gained / max(hours, 0.25)


def check(cfg, db=None, dry_run=False):
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
            log.warning("%s stats for post %s: %s", post["platform"], post["id"], e)
            continue
        if not s:
            continue
        rate = velocity(db, post, s, now)
        store.add_stats(db, post["id"], s)
        log.info("%s | %s | %d views | %.0f views/h",
                 post["platform"], post["title"], s["views"], rate)
        if rate >= m["views_per_hour"] and rate > hot.get(post["video_id"], (None, 0))[1]:
            hot[post["video_id"]] = (post, rate)

    boosted = []
    for video_id, (post, rate) in hot.items():
        if post["parent_id"] is not None:
            continue  # a follow-up doesn't spawn its own follow-ups
        if post["boosts"] >= m["max_boosts_per_video"]:
            continue
        source = Path(post["source"])
        if not source.exists():
            log.warning("Trending, but the source file is gone: %s", source)
            continue
        start = video.boost_window(video.probe(source)["duration"],
                                   m["boost_clip_seconds"], post["boosts"])
        if start is None:
            log.info("%s is trending (%.0f views/h) but the source is too short for a "
                     "different cut; not reposting the same clip", post["title"], rate)
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
