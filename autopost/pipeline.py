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
    """Instagram takes at most 1920 pixels a side: a 2K clip goes up as a 1080p copy."""
    meta = video.probe(path)
    if max(meta["width"], meta["height"]) <= 1920:
        return path
    out = config.DATA / "edited" / f"{Path(path).stem}-1080.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        log.info("Making a 1080p copy of %s for Instagram", Path(path).name)
        landscape = meta["width"] > meta["height"]
        video.edit(path, out, meta, 0, None, "original" if landscape else "vertical",
                   cfg["fps"], resolution="1080")
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


def publish(src, cfg, note="", platforms=None, dry_run=False, db=None,
            parent_id=None, boost_of=None, start=0.0, duration=None, own_caption=None):
    """Returns (video_id, {platform: url-or-error}, text)."""
    if not platforms:  # by default, only the enabled accounts that are actually connected
        platforms = [p for p in cfg["platforms"] if config.connected(p) or dry_run]
        if not platforms:
            raise RuntimeError("No social account is connected; nothing to publish to")
    ready = prepare(src, cfg, start, duration, force=parent_id is not None)
    if own_caption and own_caption.strip():
        text, origin = caption.fixed(own_caption, ready), "you"
    else:
        text, origin = caption.generate(
            ready, cfg, note, boost_of, name=re.sub(r"^\d{10}-", "", Path(src).name))
    log.info("Caption written by %s", origin)

    if dry_run:
        return None, {p: "dry-run (not uploaded)" for p in platforms}, text

    db = db or store.connect()
    video_id = store.add_video(db, src, text["youtube"]["title"], parent_id)
    results = {}
    uploads = []
    for name in platforms:
        if name == "youtube":
            for path in _youtube_files(src, ready, cfg):
                uploads.append((name, path, _youtube_text(path, text[name])))
        elif name == "instagram":
            uploads.append((name, _instagram_file(ready, cfg), text[name]))
        else:
            uploads.append((name, ready, text[name]))

    for name, path, body in uploads:
        try:
            remote_id, url = ALL[name].publish(str(path), body, cfg)
            store.add_post(db, video_id, name, remote_id, url)
            results[name] = url or "published"
            log.info("%s: published %s", name, url or "")
        except Exception as e:  # one platform failing must not stop the others
            store.add_post(db, video_id, name, error=str(e))
            results[name] = f"FAILED: {e}"
            log.error("%s: %s", name, e)
    return video_id, results, text
