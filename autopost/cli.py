"""python -m autopost <command>"""
import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import config, momentum, pipeline, store, video
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


SETUP_STEPS = [
    ("AI model - Gemini, FREE (picks the moments, translates, writes captions)",
     "Get a free key at https://aistudio.google.com/apikey -> Create API key",
     ["GEMINI_API_KEY"]),
    ("AI model - Claude, paid, optional (used instead of Gemini when set)",
     "https://console.anthropic.com -> API Keys. Press Enter to skip if you use Gemini.",
     ["ANTHROPIC_API_KEY"]),
    ("YouTube",
     "Google Cloud Console -> enable YouTube Data API v3 -> Credentials -> "
     "OAuth client ID of type 'Desktop app'",
     ["YT_CLIENT_ID", "YT_CLIENT_SECRET"]),
    ("Instagram (Business or Creator account)",
     "developers.facebook.com -> your app -> Instagram -> API setup with Instagram login "
     "-> Generate token",
     ["IG_USER_ID", "IG_ACCESS_TOKEN"]),
    ("TikTok",
     "developers.tiktok.com -> your app -> Login Kit + Content Posting API. "
     "The redirect URI is any https address you registered there.",
     ["TT_CLIENT_KEY", "TT_CLIENT_SECRET", "TT_REDIRECT_URI"]),
]


def _write_env(values):
    """Set KEY=value lines in .env, keeping everything else in the file."""
    path = config.ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, value in values.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key:
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        os.environ[key] = value
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _connected(platform):
    if platform == "youtube":
        return bool(config.get_tokens().get("youtube", {}).get("refresh_token"))
    if platform == "tiktok":
        return bool(config.get_tokens().get("tiktok", {}).get("refresh_token"))
    return bool(os.environ.get("IG_ACCESS_TOKEN") and os.environ.get("IG_USER_ID"))


def cmd_setup(args, cfg):
    """Ask for each key, save it, log in where needed, then verify everything."""
    print("\nSETUP - paste each value and press Enter. Press Enter alone to skip a "
          "platform you don't use (or to keep a value that is already saved).\n")
    for title, where, keys in SETUP_STEPS:
        print(f"--- {title}\n    {where}")
        new = {}
        for key in keys:
            have = " [already saved]" if os.environ.get(key) else ""
            value = input(f"    {key}{have}: ").strip().strip("\"'")
            if key == "ANTHROPIC_API_KEY" and value and not value.startswith("sk-ant-"):
                print("    That is not a Claude key (they start with sk-ant-). Skipped; "
                      "Gemini will be used.")
                value = ""
                if not os.environ.get(key, "").startswith("sk-ant-"):
                    _write_env({key: ""})  # also clear a wrong value saved earlier
            if value:
                new[key] = value
        if new:
            _write_env(new)
        print()
    if os.environ.get("IG_ACCESS_TOKEN", "").startswith("IG"):
        _write_env({"IG_GRAPH_HOST": "graph.instagram.com"})  # Instagram-login tokens

    for name, mod, keys in (("youtube", youtube, ["YT_CLIENT_ID", "YT_CLIENT_SECRET"]),
                            ("tiktok", tiktok, ["TT_CLIENT_KEY", "TT_CLIENT_SECRET",
                                                "TT_REDIRECT_URI"])):
        if all(os.environ.get(k) for k in keys) and not _connected(name):
            print(f"--- Connect your {name} account")
            try:
                cmd_auth(argparse.Namespace(platform=name), cfg)
            except Exception as e:
                print(f"    {name} login failed: {e}\n    Run setup again to retry.")
            print()

    # Publish only to what is actually connected, so one missing account
    # doesn't turn every post into an error.
    active = [p for p in ("youtube", "instagram", "tiktok") if _connected(p)]
    if active:
        cfg["platforms"] = active
        path = config.ROOT / "config.json"
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        saved["platforms"] = active
        path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print("--- Checking\n")
    cmd_check(args, cfg)
    return 0


def cmd_start(args, cfg):
    """The simple mode: paste links, everything else happens on its own."""
    from . import clipper
    if not any(_connected(p) for p in cfg["platforms"]):
        print("No social account is connected yet, so nothing could be published.")
        cmd_setup(args, cfg)
    publishing = any(_connected(p) for p in cfg["platforms"])
    if publishing:
        threading.Thread(target=cmd_watch, args=(args, cfg), daemon=True).start()
    else:
        print("\nStill no account connected. Clips will be made and kept in the inbox "
              "folder; they publish once you finish setup and start again.")
    link = getattr(args, "link", None)
    while True:
        if not link:
            try:
                link = input("\nPaste a YouTube/Twitch link and press Enter (q to quit): ").strip()
            except EOFError:
                link = "q"
        if link.lower() in ("q", "quit", "exit"):
            return
        if link:
            try:
                clips = clipper.run(link, cfg)
                if publishing:
                    log.info("%d clip(s) queued. They publish on their own, one every "
                             "%d min. Keep this window open.", len(clips),
                             cfg["min_minutes_between_posts"])
                else:
                    log.info("%d clip(s) saved in %s", len(clips), config.ROOT / "inbox")
            except Exception as e:
                log.error("Could not process %s: %s", link, e)
        link = None


def cmd_check(args, cfg):
    """Say exactly what is set up and what is missing, testing each login for real."""
    import requests
    from .platforms import instagram
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"[{'OK' if good else 'MISSING'}] {name}{': ' + detail if detail else ''}")

    def attempt(name, fn):
        try:
            fn()
            report(name, True, "login works")
        except Exception as e:
            # Error text can quote the request URL; never print the token in it.
            report(name, False, re.sub(r"access_token=[^&\s)']+", "access_token=***", str(e))[:160])

    report("ffmpeg", bool(shutil.which("ffmpeg") and shutil.which("ffprobe")))
    for mod in ("yt_dlp", "faster_whisper", "cv2", "numpy"):
        try:
            __import__(mod)
            report(f"python package {mod}", True)
        except ImportError:
            report(f"python package {mod}", False, "run: pip install -r requirements.txt")
    from . import llm
    who = llm.provider(cfg)
    if who == "gemini":
        attempt("AI model (Gemini)", lambda: llm.gemini_models(cfg))
    else:
        report("AI model (Gemini or Claude key)", who is not None, "Claude" if who else
               "without one: weaker moment picking, no translation, basic captions")
    if "youtube" in cfg["platforms"]:
        attempt("YouTube", youtube._headers)
    if "instagram" in cfg["platforms"]:
        def ig():
            r = requests.get(f"{instagram._base()}/{config.env('IG_USER_ID')}", timeout=30,
                             params={"fields": "username",
                                     "access_token": config.env("IG_ACCESS_TOKEN")})
            if r.status_code >= 400:
                raise RuntimeError(r.text[:160])
        attempt("Instagram", ig)
    if "tiktok" in cfg["platforms"]:
        attempt("TikTok", lambda: tiktok._call(
            "/post/publish/creator_info/query/", {}, "TikTok"))
    print("\nEverything is ready." if ok else
          "\nFix the MISSING lines above (see README), then run this again.")
    return 0 if ok else 1


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
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("publish", help="edit if needed, caption and post one video")
    p.add_argument("video")
    p.add_argument("--note", default="", help="what the video is about (helps the caption)")
    p.add_argument("--platforms", nargs="+", choices=["youtube", "instagram", "tiktok"])
    p.add_argument("--dry-run", action="store_true", help="edit + caption, upload nothing")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("start", help="simple mode: paste links, clips get made and published")
    p.add_argument("link", nargs="?")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("setup", help="enter your keys and connect your accounts")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("check", help="verify the setup and every account login")
    p.set_defaults(fn=cmd_check)

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

    # `python -m autopost` or `python -m autopost <link>` is the simple mode.
    argv = sys.argv[1:]
    if not argv:
        argv = ["start"]
    elif argv[0].startswith(("http://", "https://", "www.")) or Path(argv[0]).is_file():
        argv = ["start", argv[0]]
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Arabic titles crash a default Windows console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        cfg = config.load()
        if args.cmd not in ("check", "setup", "auth"):
            video.ensure_ffmpeg()
        raise SystemExit(args.fn(args, cfg) or 0)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except RuntimeError as e:
        log.error("%s", e)
        raise SystemExit(1)
