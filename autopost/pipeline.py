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


def _publish_youtube_alt(alt, text, cfg, db, video_id, results):
    """The same moment in the other orientation: vertical lands as a Short,
    landscape as a regular video."""
    meta = video.probe(alt)
    is_short = meta["height"] > meta["width"]
    description = re.sub(r"\s*#shorts\b", "", text["description"], flags=re.I).strip()
    if is_short:
        description += "\n#Shorts"
    kind = "youtube short" if is_short else "youtube video"
    try:
        remote_id, url = ALL["youtube"].publish(
            str(alt), {**text, "description": description}, cfg)
        store.add_post(db, video_id, "youtube", remote_id, url)
        results[kind] = url or "published"
        log.info("%s: published %s", kind, url or "")
    except Exception as e:
        store.add_post(db, video_id, "youtube", error=str(e))
        results[kind] = f"FAILED: {e}"
        log.error("%s: %s", kind, e)


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
    for name in platforms:
        try:
            remote_id, url = ALL[name].publish(str(ready), text[name], cfg)
            store.add_post(db, video_id, name, remote_id, url)
            results[name] = url or "published"
            log.info("%s: published %s", name, url or "")
            alt = Path(src).with_suffix(".alt.mp4")
            if name == "youtube" and alt.exists():
                _publish_youtube_alt(alt, text["youtube"], cfg, db, video_id, results)
        except Exception as e:  # one platform failing must not stop the others
            store.add_post(db, video_id, name, error=str(e))
            results[name] = f"FAILED: {e}"
            log.error("%s: %s", name, e)
    return video_id, results, text
