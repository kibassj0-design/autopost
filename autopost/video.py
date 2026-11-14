"""ffmpeg helpers: inspect a video, fix it for Reels/Shorts/TikTok, cut variants."""
import base64
import json
import subprocess
import tempfile
from pathlib import Path

W, H = 1080, 1920


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
    return {
        "width": w,
        "height": h,
        "duration": float(info["format"].get("duration") or video.get("duration") or 0),
        "vcodec": video.get("codec_name"),
        "acodec": audio.get("codec_name") if audio else None,
        "is_mp4": "mp4" in info["format"].get("format_name", ""),
    }


def edit_reasons(meta, max_seconds):
    """Why this file can't be posted as-is. Empty list = post untouched."""
    reasons = []
    if abs(meta["width"] / meta["height"] - W / H) > 0.02:
        reasons.append(f"aspect {meta['width']}x{meta['height']} is not 9:16")
    if meta["height"] > H:
        reasons.append("larger than 1080x1920")
    if meta["duration"] > max_seconds + 0.5:
        reasons.append(f"{meta['duration']:.0f}s is longer than {max_seconds}s")
    if meta["vcodec"] != "h264" or not meta["is_mp4"]:
        reasons.append(f"codec/container {meta['vcodec']} is not H.264 MP4")
    if meta["acodec"] != "aac":
        reasons.append(f"audio {meta['acodec'] or 'missing'} is not AAC")
    return reasons


def edit(src, dst, meta, start=0.0, duration=None):
    """Re-encode to 1080x1920 H.264/AAC. Non-vertical footage gets a blurred fill
    behind it rather than a crop, so nothing in frame is lost."""
    vertical = abs(meta["width"] / meta["height"] - W / H) <= 0.02
    if vertical:
        vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1[v]")
    else:
        vf = (f"[0:v]split[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:5[bg];"
              f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
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
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
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


def boost_window(source_duration, clip_seconds, index):
    """Start time of the index-th follow-up cut, or None when the source is too
    short to yield a clip that isn't just the original again."""
    if source_duration < clip_seconds * 1.5:
        return None
    windows = int(source_duration // clip_seconds)
    # Start from the end of the source: the first post already led with the opening.
    slot = windows - 1 - (index % windows)
    return slot * clip_seconds
