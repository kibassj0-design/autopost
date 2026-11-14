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


def _is_vertical(path):
    meta = video.probe(path)
    return meta["height"] > meta["width"]


def _youtube_text(path, text):
    """YouTube text for this file: #Shorts on a vertical upload, not on a landscape one."""
    meta = video.probe(path)
    description = re.sub(r"\s*#shorts\b", "", text["description"], flags=re.I).strip()
    if meta["height"] > meta["width"]:
        description += "\n#Shorts"
    return {**text, "description": description}


def publish(src, cfg, note="", platforms=None, dry_run=False, db=None,
            parent_id=None, boost_of=None, start=0.0, duration=None):
    """Returns (video_id, {platform: url-or-error}, text)."""
    if not platforms:  # by default, only the enabled accounts that are actually connected
        platforms = [p for p in cfg["platforms"] if config.connected(p) or dry_run]
        if not platforms:
            raise RuntimeError("No social account is connected; nothing to publish to")
    ready = prepare(src, cfg, start, duration, force=parent_id is not None)
    text, origin = caption.generate(
        ready, cfg, note, boost_of, name=re.sub(r"^\d{10}-", "", Path(src).name))
    log.info("Caption written by %s", origin)

    if dry_run:
        return None, {p: "dry-run (not uploaded)" for p in platforms}, text

    db = db or store.connect()
    video_id = store.add_video(db, src, text["youtube"]["title"], parent_id)
    results = {}
    # A second render in the other orientation, made by the clipper for YouTube.
    alt = Path(src).with_suffix(".alt.mp4")
    uploads = []
    for name in platforms:
        if name == "youtube" and alt.exists():
            vertical = [p for p in (alt, ready) if _is_vertical(p)]
            if cfg["youtube_mode"] == "short" and vertical:
                uploads.append((name, vertical[0], _youtube_text(vertical[0], text[name])))
            else:  # "both": the regular video and the Short
                for path in (ready, alt):
                    uploads.append((name, path, _youtube_text(path, text[name])))
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
