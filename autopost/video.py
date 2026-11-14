"""ffmpeg helpers: inspect a video, fix it for Reels/Shorts/TikTok, cut variants."""
import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

W, H = 1080, 1920


def target_size(meta, orientation="vertical"):
    """Output frame. 'original' keeps a landscape source landscape (1920x1080);
    anything else, and every non-landscape source, is 1080x1920."""
    if orientation == "original" and meta["width"] > meta["height"] * 1.1:
        return H, W
    return W, H


def ensure_ffmpeg():
    """Use the system ffmpeg, or fetch a bundled one so there is nothing to install."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except Exception as e:
        raise RuntimeError(f"ffmpeg is not installed and could not be fetched ({e}). "
                           "Install ffmpeg and try again.")


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[-600:]}")
    return proc.stdout


def probe(path):
    info = json.loads(_run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]))
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise RuntimeError(f"{path}: no video stream")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    w, h = int(video["width"]), int(video["height"])
    rotation = 0
    for side in video.get("side_data_list", []):
        rotation = abs(int(side.get("rotation", 0))) or rotation
    if rotation in (90, 270):
        w, h = h, w
    try:
        num, den = video["avg_frame_rate"].split("/")
        fps = float(num) / float(den)
    except (KeyError, ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "fps": fps,
        "width": w,
        "height": h,
        "duration": float(info["format"].get("duration") or video.get("duration") or 0),
        "vcodec": video.get("codec_name"),
        "acodec": audio.get("codec_name") if audio else None,
        "is_mp4": "mp4" in info["format"].get("format_name", ""),
    }


def edit_reasons(meta, max_seconds, orientation="vertical"):
    """Why this file can't be posted as-is. Empty list = post untouched."""
    reasons = []
    tw, th = target_size(meta, orientation)
    if abs(meta["width"] / meta["height"] - tw / th) > 0.02:
        reasons.append(f"aspect {meta['width']}x{meta['height']} is not "
                       f"{'16:9' if tw > th else '9:16'}")
    elif meta["height"] > th or meta["width"] > tw:
        reasons.append(f"larger than {tw}x{th}")
    elif meta["height"] < th - 8:
        reasons.append(f"{meta['width']}x{meta['height']} is below {tw}x{th}")
    if meta["duration"] > max_seconds + 0.5:
        reasons.append(f"{meta['duration']:.0f}s is longer than {max_seconds}s")
    if meta["vcodec"] != "h264" or not meta["is_mp4"]:
        reasons.append(f"codec/container {meta['vcodec']} is not H.264 MP4")
    if meta["acodec"] != "aac":
        reasons.append(f"audio {meta['acodec'] or 'missing'} is not AAC")
    return reasons


def fit_filter(meta, tw, th):
    """Filter chain (ending unlabelled) that fills a tw x th frame without cropping:
    matching aspect is simply scaled, anything else sits on a blurred copy of itself."""
    if abs(meta["width"] / meta["height"] - tw / th) <= 0.02:
        return (f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2")
    # Blur a quarter-size copy and scale it up: same look, a fraction of the work.
    return (f"[0:v]split[a][b];"
            f"[a]scale={tw // 4}:{th // 4}:force_original_aspect_ratio=increase,"
            f"crop={tw // 4}:{th // 4},boxblur=6:3,scale={tw}:{th}[bg];"
            f"[b]scale={tw}:{th}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2")


def edit(src, dst, meta, start=0.0, duration=None, orientation="vertical", fps=60):
    """Re-encode to H.264/AAC at 1080x1920, or 1920x1080 when a landscape source
    keeps its orientation. Nothing in frame is cropped away."""
    vf = fit_filter(meta, *target_size(meta, orientation)) + ",setsar=1[v]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src)]
    if meta["acodec"]:
        graph = vf + ";[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    else:  # platforms handle silent tracks better than missing ones
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        graph = vf + ";[1:a]anull[a]"
    left = meta["duration"] - start
    cmd += ["-t", f"{min(duration or left, left):.3f}"]
    cmd += [
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(dst),
    ]
    _run(cmd)
    return dst


def frames_b64(path, count=4, width=512):
    """Evenly spaced JPEG stills, for the caption model to look at."""
    duration = probe(path)["duration"]
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            shot = Path(tmp) / f"{i}.jpg"
            at = duration * (i + 0.5) / count
            _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path),
                  "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(shot)])
            if shot.exists():
                out.append(base64.standard_b64encode(shot.read_bytes()).decode())
    return out


def boost_window(source_duration, posted_seconds, clip_seconds, index):
    """Start time of the index-th follow-up cut. Follow-ups only come from footage
    that was trimmed off the original post; None when there is none left, because
    re-posting a piece of what is already up is duplicate content."""
    start = posted_seconds + index * clip_seconds
    return start if source_duration - start >= min(clip_seconds, 15) else None
