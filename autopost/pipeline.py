"""One video in -> edited if needed -> captioned -> posted to every platform."""
import logging
import re
import time
from pathlib import Path

from . import caption, config, store, video
from .platforms import ALL

log = logging.getLogger("autopost")


def prepare(src, cfg, start=0.0, duration=None, force=False):
    """Return a file that is safe to upload: the original, or an edited copy."""
    meta = video.probe(src)
    if meta["duration"] < 3:
        raise RuntimeError(f"{src}: shorter than 3 seconds, platforms reject it")
    reasons = video.edit_reasons(meta, cfg["max_seconds"], cfg["orientation"])
    if not reasons and not force:
        log.info("%s is ready as-is, no editing", Path(src).name)
        return Path(src)
    if reasons:
        log.info("Editing %s: %s", Path(src).name, "; ".join(reasons))
    out_dir = config.DATA / "edited"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{Path(src).stem}-{int(time.time())}.mp4"
    return video.edit(src, dst, meta, start, duration or cfg["max_seconds"],
                      cfg["orientation"], cfg["fps"])


SHORT_MAX = 179  # YouTube lists vertical uploads up to 3 minutes as Shorts


def _is_short(path):
    meta = video.probe(path)
    return meta["height"] > meta["width"] and meta["duration"] <= SHORT_MAX


def _vertical_copy(path, cfg):
    """A 9:16 render of `path`, so YouTube files it as a Short."""
    meta = video.probe(path)
    out = config.DATA / "edited" / f"{Path(path).stem}-short.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        log.info("Making a vertical copy of %s for YouTube Shorts", Path(path).name)
        video.edit(path, out, meta, 0, min(meta["duration"], SHORT_MAX), "vertical",
                   cfg["fps"])
    return out


def _instagram_file(path, cfg):
    """Every Reel goes up as a copy made to Instagram's published Reels spec (H.264
    high profile, closed GOP, 4:2:0, <=1920 px, <=60 fps, <=12 Mbps, AAC 128 kbps 48 kHz,
    moov at the front). Clips that are slightly off spec fail on Instagram's side with
    "ProcessingFailedError" and nothing else to go on, so no chances are taken."""
    out = config.DATA / "edited" / f"{Path(path).stem}-ig.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        meta = video.probe(path)
        log.info("Making the Instagram copy of %s", Path(path).name)
        landscape = meta["width"] > meta["height"]
        video.edit(path, out, meta, 0, None, "original" if landscape else "vertical",
                   min(cfg["fps"], 60), resolution="1080", safe=True)
    return out


def _youtube_files(src, ready, cfg):
    """What goes to YouTube. 'short' (default): one vertical Short, made here when the
    clip has none. 'both': the Short plus the landscape video. 'video': the clip as is."""
    mode = cfg["youtube_mode"]
    if mode == "video":
        return [ready]
    alt = Path(src).with_suffix(".alt.mp4")  # the clipper's other-orientation render
    short = next((p for p in (alt, ready) if p.exists() and _is_short(p)), None)
    if short is None:
        short = _vertical_copy(ready, cfg)
    if mode == "both" and not _is_short(ready):
        return [short, ready]
    return [short]


def _youtube_text(path, text):
    """YouTube text for this file: #Shorts on a vertical upload, not on a landscape one."""
    meta = video.probe(path)
    description = re.sub(r"\s*#shorts\b", "", text["description"], flags=re.I).strip()
    if meta["height"] > meta["width"]:
        description += "\n#Shorts"
    return {**text, "description": description}


class PublishCancelled(RuntimeError):
    pass


def publish(src, cfg, note="", platforms=None, dry_run=False, db=None,
            parent_id=None, boost_of=None, start=0.0, duration=None, own_caption=None,
            stop=None, publish_at=None):
    """Returns (video_id, {platform: url-or-error}, text). publish_at: YouTube publishes
    it itself at that time (the others are posted when this is called)."""
    if not platforms:  # by default, only the enabled accounts that are actually connected
        platforms = [p for p in cfg["platforms"] if config.connected(p) or dry_run]
        if not platforms:
            raise RuntimeError("No social account is connected; nothing to publish to")
    ready = prepare(src, cfg, start, duration, force=parent_id is not None)
    if stop and stop():
        raise PublishCancelled("cancelled")
    if own_caption and own_caption.strip():
        text, origin = caption.fixed(own_caption, ready), "you"
    else:
        text, origin = caption.generate(
            ready, cfg, note, boost_of, name=re.sub(r"^\d{10}-", "", Path(src).name))
    log.info("Caption written by %s", origin)

    if publish_at:
        text = {**text, "publish_at": publish_at}
    if dry_run:
        return None, {p: "dry-run (not uploaded)" for p in platforms}, text

    db = db or store.connect()
    video_id = store.add_video(db, src, text["youtube"]["title"], parent_id)
    results = {}
    for name in platforms:
        # Cancelling stops before the next platform; an upload already under way
        # finishes first (the platforms offer no way to abort one half-way).
        if stop and stop():
            log.info("Publishing cancelled before %s", name)
            raise PublishCancelled("cancelled")
        results[name] = _send(db, video_id, name, src, ready, text, cfg, stop)
    if any(not str(r).startswith("FAILED") for r in results.values()):
        from . import studio
        studio.save_thumb(video_id, ready)  # for the studio's content grid
    return video_id, results, text


def _uploads(name, src, ready, text, cfg):
    """The file(s) and text this platform gets."""
    if name == "youtube":
        extra = {"publish_at": text["publish_at"]} if text.get("publish_at") else {}
        return [(path, {**_youtube_text(path, text[name]), **extra})
                for path in _youtube_files(src, ready, cfg)]
    if name == "instagram":
        return [(_instagram_file(ready, cfg), text[name])]
    return [(ready, text[name])]


def _send(db, video_id, name, src, ready, text, cfg, stop=None, post_id=None):
    """Publish to one platform and record each upload. Returns the url, or 'FAILED: ...'
    when nothing went up. Making the platform's copy is inside the try: a copy that
    cannot be made fails that platform only, never the others."""
    def record(remote_id=None, url=None, error=None):
        nonlocal post_id
        if post_id:  # a retry fills the row it came from
            store.update_post(db, post_id, remote_id, url, error)
            post_id = None
        else:
            store.add_post(db, video_id, name, remote_id, url, error)

    try:
        uploads = _uploads(name, src, ready, text, cfg)
    except Exception as e:
        record(error=str(e))
        log.error("%s: %s", name, e)
        return f"FAILED: {e}"
    result = None
    for i, (path, body) in enumerate(uploads):
        if i and stop and stop():
            break
        try:
            remote_id, url = ALL[name].publish(str(path), body, cfg)
            record(remote_id, url)
            result = result or url or "published"
            if not str(url or "").startswith("tiktok:"):
                log.info("%s: published %s", name, url or "")
        except Exception as e:  # one platform failing must not stop the others
            record(error=str(e))
            log.error("%s: %s", name, e)
            if result is None and i == len(uploads) - 1:
                return f"FAILED: {e}"
    return result or "FAILED: nothing was uploaded"


def retry(db, post_id, cfg, stop=None):
    """Publish a clip again to the one platform where it failed, in the same row."""
    row = db.execute("SELECT p.platform, p.video_id, v.source, v.title FROM posts p"
                     " JOIN videos v ON v.id = p.video_id WHERE p.id=?", (post_id,)).fetchone()
    if row is None:
        raise RuntimeError("this post no longer exists")
    src = Path(row["source"])
    if not src.exists():
        raise RuntimeError("the video file is no longer on the computer")
    name = row["platform"]
    if not config.connected(name):
        raise RuntimeError(f"{name} is not connected")
    ready = prepare(src, cfg)
    text, origin = caption.generate(ready, cfg, "", None,
                                    name=re.sub(r"^\d{10}-", "", src.name))
    log.info("Retrying %s on %s (caption by %s)", src.name, name, origin)
    return _send(db, row["video_id"], name, src, ready, text, cfg, stop, post_id=post_id)
