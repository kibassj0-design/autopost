"""python -m autopost <command>"""
import argparse
import json
import logging
import shutil
import time
import urllib.parse
from pathlib import Path

from . import config, momentum, pipeline, store
from .platforms import tiktok, youtube

log = logging.getLogger("autopost")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def cmd_publish(args, cfg):
    _, results, text = pipeline.publish(
        args.video, cfg, note=args.note, platforms=args.platforms, dry_run=args.dry_run)
    print(json.dumps({"text": text, "results": results}, ensure_ascii=False, indent=2))
    return 1 if any(str(r).startswith("FAILED") for r in results.values()) else 0


def _stable(path, seen):
    """True once a file's size stopped changing between two scans (copy finished)."""
    size = path.stat().st_size
    done = size > 0 and seen.get(path) == size
    seen[path] = size
    return done


def cmd_watch(args, cfg):
    inbox, done, failed = (config.ROOT / d for d in ("inbox", "done", "failed"))
    for d in (inbox, done, failed):
        d.mkdir(exist_ok=True)
    db = store.connect()
    seen, last_check, release = {}, 0.0, False
    m = cfg["momentum"]
    gap = cfg["min_minutes_between_posts"] * 60
    row = db.execute("SELECT MAX(p.published_at) t FROM posts p JOIN videos v"
                     " ON v.id = p.video_id WHERE p.error IS NULL"
                     " AND v.parent_id IS NULL").fetchone()
    last_post = row["t"] or 0.0
    log.info("Watching %s (drop videos here). Ctrl+C to stop.", inbox)
    while True:
        ready = [p for p in sorted(inbox.iterdir())
                 if p.suffix.lower() in VIDEO_EXT and _stable(p, seen)]
        wait = gap - (time.time() - last_post)
        if ready and wait > 0 and not release:
            if seen.get("logged") != len(ready):
                log.info("%d video(s) queued; next post in %.0f min", len(ready), wait / 60)
                seen["logged"] = len(ready)
            ready = []
        # One per pass: posts are spaced out, not dumped all at once.
        for path in ready[:1]:
            release = False
            # An optional "<name>.txt" beside the video is the creator's note.
            note_file = path.with_suffix(".txt")
            note = note_file.read_text(encoding="utf-8").strip() if note_file.exists() else ""
            # Move first: the database must point at where the file will live.
            kept = Path(shutil.move(path, done / f"{int(time.time())}-{path.name}"))
            target = done
            try:
                _, results, _ = pipeline.publish(kept, cfg, note=note, db=db)
                if all(str(r).startswith("FAILED") for r in results.values()):
                    target = failed
            except Exception as e:
                log.error("%s: %s", path.name, e)
                target = failed
            if target is failed:
                shutil.move(kept, failed / kept.name)
            else:
                last_post = time.time()
            if note_file.exists():
                shutil.move(note_file, target / note_file.name)
            seen.pop(path, None)
        if m["enabled"] and time.time() - last_check >= m["check_every_minutes"] * 60:
            last_check = time.time()
            try:
                queued = any(p.suffix.lower() in VIDEO_EXT for p in inbox.iterdir())
                if momentum.check(cfg, db, cut=not queued) and queued:
                    release = True
            except Exception as e:
                log.error("momentum check: %s", e)
        time.sleep(cfg["watch_interval_seconds"])


def cmd_clip(args, cfg):
    from . import clipper
    clips = clipper.run(args.url, cfg, count=args.count, start=args.start, end=args.end,
                        queue=not args.no_queue)
    print(f"{len(clips)} clip(s) ready:")
    for c in clips:
        print(f"  {c}")
    if args.no_queue:
        return
    if args.watch:
        cmd_watch(args, cfg)
    else:
        print("Queued in inbox/. `python -m autopost watch` publishes them.")


def cmd_momentum(args, cfg):
    boosted = momentum.check(cfg, dry_run=args.dry_run)
    print(f"{len(boosted)} follow-up(s) {'would be ' if args.dry_run else ''}posted")


def cmd_status(args, cfg):
    db = store.connect()
    rows = db.execute(
        "SELECT p.id, p.platform, p.url, p.error, v.title, v.parent_id,"
        " (SELECT views FROM stats s WHERE s.post_id = p.id ORDER BY ts DESC LIMIT 1) views"
        " FROM posts p JOIN videos v ON v.id = p.video_id ORDER BY p.id DESC LIMIT 30").fetchall()
    for r in rows:
        kind = "boost" if r["parent_id"] else "post "
        state = f"FAILED {r['error'][:80]}" if r["error"] else (r["url"] or "published")
        views = "-" if r["views"] is None else r["views"]
        print(f"{kind} {r['platform']:<9} views={views:<8} {r['title'][:40]:<40} {state}")
    if not rows:
        print("Nothing posted yet.")


def cmd_auth(args, cfg):
    mod = {"youtube": youtube, "tiktok": tiktok}[args.platform]
    print("1) Open this link and approve access:\n\n" + mod.auth_url() + "\n")
    print("2) The browser lands on a page that may fail to load. That's fine.\n"
          "   Copy the full address from the address bar and paste it here.")
    pasted = input("> ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    code = query.get("code", [pasted])[0]
    mod.exchange(code)
    print(f"{args.platform} connected. Token saved to {config.TOKENS}")


def main():
    ap = argparse.ArgumentParser(prog="autopost")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish", help="edit if needed, caption and post one video")
    p.add_argument("video")
    p.add_argument("--note", default="", help="what the video is about (helps the caption)")
    p.add_argument("--platforms", nargs="+", choices=["youtube", "instagram", "tiktok"])
    p.add_argument("--dry-run", action="store_true", help="edit + caption, upload nothing")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("clip", help="cut the best 30-80s moments out of a YouTube/Twitch link")
    p.add_argument("url", help="YouTube or Twitch link, or a local video file")
    p.add_argument("--count", type=int, help="how many clips to make")
    p.add_argument("--start", help="only use the video from this time (e.g. 1:30:00)")
    p.add_argument("--end", help="only use the video up to this time")
    p.add_argument("--no-queue", action="store_true", help="make the clips but don't publish")
    p.add_argument("--watch", action="store_true", help="then stay running and publish them")
    p.set_defaults(fn=cmd_clip)

    p = sub.add_parser("watch", help="post everything dropped in inbox/ and track momentum")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("momentum", help="check stats now and post follow-ups for hot videos")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_momentum)

    p = sub.add_parser("status", help="recent posts and their views")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("auth", help="connect an account")
    p.add_argument("platform", choices=["youtube", "tiktok"])
    p.set_defaults(fn=cmd_auth)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        raise SystemExit(args.fn(args, config.load()) or 0)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except RuntimeError as e:
        log.error("%s", e)
        raise SystemExit(1)
