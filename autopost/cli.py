"""python -m autopost <command>"""
import argparse
import json
import logging
import os
import shutil
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import checks, config, momentum, pipeline, store, video
from .platforms import tiktok, youtube
from .publisher import VIDEO_EXT, Publisher

log = logging.getLogger("autopost")


def cmd_publish(args, cfg):
    _, results, text = pipeline.publish(
        args.video, cfg, note=args.note, platforms=args.platforms, dry_run=args.dry_run)
    print(json.dumps({"text": text, "results": results}, ensure_ascii=False, indent=2))
    return 1 if any(str(r).startswith("FAILED") for r in results.values()) else 0


def cmd_watch(args, cfg):
    pub = Publisher(cfg)
    log.info("Watching %s (drop videos here). Ctrl+C to stop.", pub.inbox)
    pub.run_forever()


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
    ("More FREE AI keys, all optional (used automatically when the one before runs out)",
     "Mistral: console.mistral.ai/api-keys | OpenRouter: openrouter.ai/keys | "
     "Groq: console.groq.com/keys | Cerebras: cloud.cerebras.ai",
     ["MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY"]),
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
                    config.write_env({key: ""})  # also clear a wrong value saved earlier
            if value:
                new[key] = value
        if new:
            config.write_env(new)
        print()
    if os.environ.get("IG_ACCESS_TOKEN", "").startswith("IG"):
        config.write_env({"IG_GRAPH_HOST": "graph.instagram.com"})  # Instagram-login tokens

    for name, mod, keys in (("youtube", youtube, ["YT_CLIENT_ID", "YT_CLIENT_SECRET"]),
                            ("tiktok", tiktok, ["TT_CLIENT_KEY", "TT_CLIENT_SECRET",
                                                "TT_REDIRECT_URI"])):
        if all(os.environ.get(k) for k in keys) and not config.connected(name):
            print(f"--- Connect your {name} account")
            try:
                cmd_auth(argparse.Namespace(platform=name), cfg)
            except Exception as e:
                print(f"    {name} login failed: {e}\n    Run setup again to retry.")
            print()

    # Publish only to what is actually connected, so one missing account
    # doesn't turn every post into an error.
    active = [p for p in ("youtube", "instagram", "tiktok") if config.connected(p)]
    if active:
        config.save_config(cfg, {"platforms": active})
    print("--- Checking\n")
    cmd_check(args, cfg)
    return 0


def cmd_start(args, cfg):
    """The simple mode: paste links, everything else happens on its own."""
    from . import clipper
    if not any(config.connected(p) for p in cfg["platforms"]):
        print("No social account is connected yet, so nothing could be published.")
        cmd_setup(args, cfg)
    publishing = any(config.connected(p) for p in cfg["platforms"])
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
    results = checks.run(cfg)
    for name, good, detail in results:
        print(f"[{'OK' if good else 'MISSING'}] {name}{': ' + detail if detail else ''}")
    ok = all(good for _, good, _ in results)
    print("\nEverything is ready." if ok else
          "\nFix the MISSING lines above (see README), then run this again.")
    return 0 if ok else 1


def cmd_app(args, cfg):
    from . import app
    app.run(cfg, open_window=not args.no_window)


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

    p = sub.add_parser("app", help="open the desktop app (the default)")
    p.add_argument("--no-window", action="store_true", help="don't open a window")
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("start", help="console mode: paste links, clips get made and published")
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

    # `python -m autopost` opens the app; `python -m autopost <link>` is console mode.
    argv = sys.argv[1:]
    if not argv:
        argv = ["app"]
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
