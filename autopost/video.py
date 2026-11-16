"""ffmpeg helpers: inspect a video, fix it for Reels/Shorts/TikTok, cut variants."""
import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

W, H = 1080, 1920


QUALITY = {"resolution": "1080", "encoder": "auto"}  # set by config.load()


def short_side(meta, resolution=None):
    """The output's short side: 1080p (or 2K when chosen and available), otherwise the
    source's own size (a 720p source stays 720p). Never upscaled."""
    source = min(meta["width"], meta["height"])
    setting = str(resolution or QUALITY["resolution"])
    want = 1440 if setting == "1440" else 1080
    for size in (1440, 1080):
        if size <= want and source >= size - 40:  # a few pixels short still counts
            return size
    return max(2, source - source % 2)  # below 1080: keep the source size


def target_size(meta, orientation="vertical", resolution=None):
    """Output frame. 'original' keeps a landscape source landscape (e.g. 1920x1080);
    anything else, and every non-landscape source, is vertical (e.g. 1080x1920)."""
    short = short_side(meta, resolution)
    long = short * 16 // 9
    long -= long % 2  # H.264 needs even sizes
    if orientation == "original" and meta["width"] > meta["height"] * 1.1:
        return long, short
    return short, long


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


def out_fps(meta, wanted=60):
    """60 fps only when the source has it: doubling a 30 fps source costs twice the
    encoding time and adds copied frames, not smoothness."""
    source = meta.get("fps") or 30
    if source >= wanted - 5:
        return wanted
    return 30 if source >= 25 else max(1, round(source))


# Hardware encoders, best first, with settings close to x264 CRF 20 quality.
_HW = [
    ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21",
                    "-b:v", "0", "-pix_fmt", "yuv420p"]),
    ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "faster", "-global_quality", "21",
                  "-pix_fmt", "nv12"]),
    ("h264_amf", ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "20",
                  "-qp_p", "22", "-pix_fmt", "yuv420p"]),
    ("h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-q:v", "65",
                           "-pix_fmt", "yuv420p"]),
]
_CPU = ("libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p"])
_encoder = None


def encoder():
    """(name, ffmpeg args). The graphics card's encoder when one really works on this
    machine (tested with a tiny encode), which is several times faster; else x264."""
    global _encoder
    if _encoder is None:
        _encoder = _CPU
        if QUALITY.get("encoder", "auto") != "cpu":
            for name, args in _HW:
                test = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=s=1280x720:r=30:d=0.5", "-frames:v", "10", *args,
                        "-f", "null", "-"]
                try:
                    ok = subprocess.run(test, capture_output=True, timeout=30).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    ok = False
                if ok:
                    _encoder = (name, args)
                    break
    return _encoder


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
        "bitrate": float(info["format"].get("bit_rate") or 0),
        "abitrate": float(audio.get("bit_rate") or 0) if audio else 0.0,
        "size": float(info["format"].get("size") or 0),
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


# Safe for every platform's upload rules (Instagram's limit is 25 Mbps).
SAFE_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "12M",
             "-bufsize", "24M", "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p"]


def edit(src, dst, meta, start=0.0, duration=None, orientation="vertical", fps=60,
         resolution=None, safe=False):
    """Re-encode to H.264/AAC at 1080x1920, or 1920x1080 when a landscape source
    keeps its orientation. Nothing in frame is cropped away."""
    vf = fit_filter(meta, *target_size(meta, orientation, resolution)) + ",setsar=1[v]"
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
        "-r", str(out_fps(meta, fps)), *(SAFE_X264 if safe else encoder()[1]),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
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
