"""Link in -> best moments found -> vertical 30-80s clips with captions -> inbox/.

Works on YouTube videos, Twitch VODs/clips (anything yt-dlp can fetch) and local files.
Moments are chosen by the AI model (Claude or Gemini) from the transcript plus two hype signals that matter for
streams: loudness spikes and chat-message spikes.
"""
import json
import logging
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path

from . import config, llm, video

log = logging.getLogger("autopost.clipper")

W, H = video.W, video.H


# ---------------------------------------------------------------- download

def parse_time(value):
    """'90', '1:30' or '1:02:03' -> seconds."""
    if value is None:
        return None
    seconds = 0.0
    for part in str(value).split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def download(url, work, max_height, start=None, end=None):
    """Returns (video path, {title, channel, url}, chat timestamps in seconds)."""
    local = Path(url).expanduser()
    if local.exists():
        return local, {"title": local.stem, "channel": "", "url": ""}, []

    import yt_dlp

    opts = {
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(work / "source.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
    }
    if start is not None or end is not None:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(
            None, [(start or 0, end or float("inf"))])
    log.info("Downloading %s", url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = next(p for p in work.glob("source.*") if p.suffix in (".mp4", ".mkv", ".webm"))
    meta = {"title": info.get("title") or "", "url": info.get("webpage_url") or url,
            "channel": info.get("uploader") or info.get("channel") or ""}

    chat = []
    try:  # chat replay is a bonus signal; many videos simply don't have one
        with yt_dlp.YoutubeDL({
            "skip_download": True, "writesubtitles": True, "quiet": True,
            "no_warnings": True, "subtitleslangs": ["rechat", "live_chat"],
            "outtmpl": str(work / "chat.%(ext)s"),
        }) as ydl:
            ydl.download([url])
        chat = _chat_times(work, start or 0)
    except Exception as e:
        log.info("No chat replay (%s)", str(e)[:120])
    return path, meta, chat


def _chat_times(work, offset):
    times = []
    for f in work.glob("chat.*.json"):
        text = f.read_text(encoding="utf-8", errors="ignore")
        if "rechat" in f.name:  # Twitch: one JSON list
            for msg in json.loads(text):
                times.append(float(msg["content_offset_seconds"]))
        else:  # YouTube live chat: JSON lines
            for line in text.splitlines():
                m = re.search(r'"videoOffsetTimeMsec":\s*"?(\d+)', line)
                if m:
                    times.append(int(m.group(1)) / 1000)
    return sorted(t - offset for t in times if t >= offset)


# ---------------------------------------------------------------- signals

def extract_audio(src, work):
    wav = work / "audio.wav"
    video._run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    return wav


def loudness(wav):
    """RMS level for every second of audio."""
    import numpy as np

    levels = []
    with wave.open(str(wav)) as w:
        rate = w.getframerate()
        while True:
            raw = w.readframes(rate * 60)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            for i in range(0, len(samples), rate):
                sec = samples[i:i + rate]
                levels.append(float(np.sqrt(np.mean(sec * sec))) if len(sec) else 0.0)
    return levels


def hype_curve(levels, chat):
    """Per-second score: how far loudness and chat rate sit above this video's norm."""
    import numpy as np

    n = len(levels)
    if n == 0:
        return np.zeros(0)

    def z(x):
        x = np.asarray(x, dtype=np.float32)
        spread = x.std()
        return (x - x.mean()) / spread if spread > 0 else np.zeros_like(x)

    score = z(levels)
    if chat:
        counts = np.bincount([min(int(t), n - 1) for t in chat if t >= 0], minlength=n)
        # People type after the moment happens, so look ~8s ahead of the audio.
        score = score + 1.5 * z(np.convolve(np.roll(counts, -8), np.ones(10) / 10, "same"))
    return np.convolve(score, np.ones(15) / 15, "same")


def top_peaks(curve, count, gap):
    """Highest points of the curve, at least `gap` seconds apart."""
    import numpy as np

    peaks = []
    for i in np.argsort(curve)[::-1]:
        if all(abs(int(i) - p) >= gap for p in peaks):
            peaks.append(int(i))
            if len(peaks) == count:
                break
    return peaks


# ---------------------------------------------------------------- transcript

def transcribe(wav, model_name, language):
    """([{start, end, text, words: [{start, end, word}]}], spoken language code)."""
    from faster_whisper import WhisperModel

    log.info("Transcribing with whisper '%s' (first run downloads the model)", model_name)
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segments, info = model.transcribe(str(wav), language=language, word_timestamps=True,
                                      vad_filter=True)
    out, last_log = [], time.time()
    for s in segments:
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                    "words": [{"start": w.start, "end": w.end, "word": w.word.strip()}
                              for w in (s.words or [])]})
        if time.time() - last_log > 30:
            log.info("  transcribed %.0f of %.0f min", s.end / 60, info.duration / 60)
            last_log = time.time()
    return out, info.language


# ---------------------------------------------------------------- choosing moments

SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "start": {"type": "number"}, "end": {"type": "number"},
            "title": {"type": "string"}, "hook": {"type": "string"},
            "what_happens": {"type": "string"}, "score": {"type": "integer"},
        },
        "required": ["start", "end", "title", "hook", "what_happens", "score"],
        "additionalProperties": False,
    }}},
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM = """You pick the moments of a long video or livestream that will work as standalone \
short vertical clips (Reels, Shorts, TikTok). The clips are cut and published automatically \
from the times you give, so nobody fixes them afterwards.

You get the transcript with a start time in seconds in front of every line, and for streams \
a list of hype signals: seconds where the audio got unusually loud or chat suddenly sped up. \
Those usually mark a reaction to something that happened a few seconds earlier, so the clip \
has to begin before the spike, at the setup, not at the scream.

What makes a clip work:
- It stands alone. Someone who never saw the stream understands it from its first second \
to its last, without needing what came before.
- It opens on a hook: a question, a bold claim, the start of a story, the moment the play \
begins. Never open mid-sentence or on filler.
- It has a payoff (the punchline, the win or fail, the answer, the reaction) and ends \
right after it, on a finished sentence.
- Strong candidates: funny or shocking moments, clutch plays and fails, big reactions, hot \
takes, a story with a twist, a genuinely useful tip explained in full.

Start and end must be start times of transcript lines (end = the start of the first line \
that is NOT in the clip). Respect the length limits you are given exactly; a great moment \
that needs more room than the maximum should be tightened to its best part. Clips must not \
overlap. Quality over quantity: return fewer clips when the video doesn't have enough \
strong moments. `score` is 1-100 for how likely the clip is to hold a stranger's attention; \
order the list best first. Write title, hook and what_happens in the language spoken in \
the video."""


def _transcript_text(segments):
    return "\n".join(f"[{s['start']:.0f}] {s['text']}" for s in segments)


def pick_with_ai(segments, peaks, chat_peaks, meta, duration, c, cfg):
    brief = [
        f"Video: {meta['title']!r} by {meta['channel'] or 'unknown'}, {duration:.0f} seconds long.",
        f"Find up to {c['count']} clips, each between {c['min_seconds']} and "
        f"{c['max_seconds']} seconds long.",
    ]
    if peaks:
        brief.append("Loud moments (seconds): " + ", ".join(map(str, sorted(peaks))))
    if chat_peaks:
        brief.append("Chat spikes (seconds): " + ", ".join(map(str, sorted(chat_peaks))))
    brief.append("Transcript:\n" + _transcript_text(segments))

    data, who = llm.ask_json(SYSTEM, "\n\n".join(brief), SCHEMA, cfg, effort="high")
    log.info("%s proposed %d moments", who, len(data["clips"]))
    return data["clips"]


def pick_by_signals(curve, meta, c):
    """No transcript or no Claude: take the hype peaks, with lead-in before each."""
    length = (c["min_seconds"] + c["max_seconds"]) / 2
    clips = []
    for rank, peak in enumerate(top_peaks(curve, c["count"], c["max_seconds"])):
        start = max(0.0, peak - length * 0.6)
        clips.append({"start": start, "end": start + length, "score": 50 - rank,
                      "title": f"{meta['title']} #{rank + 1}".strip(),
                      "hook": "", "what_happens": ""})
    return clips


def fit_clips(clips, segments, duration, c):
    """Force every clip into the allowed length on sentence boundaries, drop overlaps."""
    lo, hi = c["min_seconds"], c["max_seconds"]
    starts = [s["start"] for s in segments]
    ends = [s["end"] for s in segments]
    kept = []
    for clip in sorted(clips, key=lambda x: -x["score"]):
        start = max(0.0, float(clip["start"]))
        end = min(duration, float(clip["end"]))
        if starts:  # begin on the line that starts nearest the requested time
            start = min(starts, key=lambda t: abs(t - start))
            inside = [e for e in ends if start + lo <= e <= start + hi]
            wanted = [e for e in inside if e <= end + 2]
            if wanted:
                end = max(wanted)
            elif inside:
                end = min(inside)
        start = max(0.0, start - 0.15)  # don't clip the first syllable
        end = min(duration, max(end + 0.3, start + lo), start + hi)
        if end - start < lo:  # ran into the end of the video: pull the start back
            start = max(0.0, end - lo)
        if end - start < lo - 0.5:
            continue
        if any(start < k["end"] and end > k["start"] for k in kept):
            continue
        kept.append({**clip, "start": start, "end": end})
        if len(kept) == c["count"]:
            break
    return kept


# ---------------------------------------------------------------- caption translation

LANG_NAMES = {"ar": "Arabic", "en": "English"}

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["i", "text"],
        "additionalProperties": False,
    }}},
    "required": ["lines"],
    "additionalProperties": False,
}

TRANSLATE_SYSTEM = """You translate the spoken lines of a video into subtitles that get \
burned onto short social clips. Viewers read them in a second or two while watching, so \
translate the way a native speaker would say it out loud: natural, colloquial where the \
speaker is colloquial, and no longer than the original. Keep names, game terms and brand \
names as they are commonly written by speakers of the target language. Speech-recognition \
mistakes are common in the input; when a word is clearly a mishearing, translate what was \
meant. Return exactly one translated line for every numbered input line, with the same \
number, and never merge or split lines, because each line is shown at its own time."""


def caption_target(spoken, setting):
    """Which language the on-video captions should be in.
    'swap': English speech -> Arabic captions, Arabic speech -> English, others -> Arabic."""
    if setting == "original":
        return spoken
    if setting == "swap":
        return "en" if spoken == "ar" else "ar"
    return setting


def translate_segments(segments, target, cfg):
    """Same segments with text in `target`, and word timings spread across each line."""
    numbered = "\n".join(f"{i}: {s['text']}" for i, s in enumerate(segments))
    data, _ = llm.ask_json(
        TRANSLATE_SYSTEM, f"Translate into {LANG_NAMES.get(target, target)}:\n\n{numbered}",
        TRANSLATE_SCHEMA, cfg, max_tokens=64000)
    lines = {l["i"]: l["text"].strip() for l in data["lines"]}
    if len(lines) < len(segments) * 0.9:
        raise RuntimeError(f"translation returned {len(lines)} of {len(segments)} lines")

    out = []
    for i, seg in enumerate(segments):
        words = lines.get(i, "").split()
        if not words:
            continue  # a line the model dropped: show nothing rather than the wrong language
        # The translation has no word timings, so share the line's time by word length.
        total = sum(len(w) + 1 for w in words)
        t, span, timed = seg["start"], seg["end"] - seg["start"], []
        for w in words:
            dur = span * (len(w) + 1) / total
            timed.append({"start": t, "end": t + dur, "word": w})
            t += dur
        out.append({**seg, "text": " ".join(words), "words": timed})
    return out


# ---------------------------------------------------------------- rendering

def face_crop_x(src, start, end, meta):
    """x offset for a 9:16 crop centred on the speaker, or None when the frame
    isn't a talking head (gameplay with a small facecam, several people, no face)."""
    try:
        import cv2
    except ImportError:
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(src))
    centres, samples = [], 10
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + (end - start) * (i + 0.5) / samples) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        scale = 480 / frame.shape[0]
        gray = cv2.cvtColor(cv2.resize(frame, None, fx=scale, fy=scale), cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            if h >= 0.15 * gray.shape[0]:  # big enough to be the subject
                centres.append((x + w / 2) / scale)
    cap.release()
    if len(centres) < samples * 0.6:
        return None
    centres.sort()
    if centres[-1] - centres[0] > 0.25 * meta["width"]:
        return None  # the subject moves or there are several: a fixed crop would lose them
    crop_w = meta["height"] * W / H
    centre = centres[len(centres) // 2]
    return int(min(max(centre - crop_w / 2, 0), meta["width"] - crop_w))


def _ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def write_captions(segments, start, end, path, font, size):
    """Short word groups, timed to speech. Returns False when nothing is spoken."""
    words = [w for s in segments for w in s["words"]
             if w["word"] and w["end"] > start and w["start"] < end]
    if not words:
        return False
    tw, th = size
    per_line, hold = (3, 2.0) if th > tw else (6, 3.2)  # a wide frame fits a longer line
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) == per_line or w["start"] - cur[-1]["end"] > 0.6
                    or w["end"] - cur[0]["start"] > hold):
            groups.append(cur)
            cur = []
        cur.append(w)
    groups.append(cur)
    # Vertical: big text above the platform's own buttons. Landscape: lower third.
    fs, margin = (84, 430) if th > tw else (66, 70)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {tw}", f"PlayResY: {th}",
        "WrapStyle: 0", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{fs},&H00FFFFFF,&H00000000,&H80000000,-1,1,{fs // 14},2,2,"
        f"80,80,{margin},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i, g in enumerate(groups):
        t0 = g[0]["start"] - start
        nxt = groups[i + 1][0]["start"] - start if i + 1 < len(groups) else end - start
        t1 = min(max(g[-1]["end"] - start + 0.15, t0 + 0.3), nxt)
        text = " ".join(w["word"] for w in g).replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Cap,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def render(src, meta, clip, caption_segments, out, c, orientation):
    start, end = clip["start"], clip["end"]
    tw, th = video.target_size(meta, orientation)
    matches = abs(meta["width"] / meta["height"] - tw / th) <= 0.02
    crop_x = None
    if tw > th:
        layout = "landscape"
    elif matches:
        layout = "vertical"
    elif c["layout"] == "crop":
        crop_x = int((meta["width"] - meta["height"] * W / H) / 2)
    elif c["layout"] == "auto":
        try:
            crop_x = face_crop_x(src, start, end, meta)
        except Exception as e:  # framing is a nicety; never lose a clip over it
            log.warning("Face detection failed (%s); using blurred fill", str(e)[:120])
    if crop_x is not None and meta["width"] / meta["height"] > W / H:
        chain = f"[0:v]crop=ih*{W}/{H}:ih:{crop_x}:0,scale={W}:{H}"
        layout = "face crop" if c["layout"] == "auto" else "centre crop"
    else:
        chain = video.fit_filter(meta, tw, th)
        if not matches:
            layout = "blurred fill"
    ass = out.with_suffix(".ass")
    if c["captions"] and write_captions(caption_segments, start, end, ass,
                                       c["caption_font"], (tw, th)):
        chain += f",ass={ass.name}"
    chain += ",setsar=1[v]"

    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(Path(src).resolve())]
    if meta["acodec"]:
        graph = chain + ";[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        graph = chain + ";[1:a]anull[a]"
    cmd += ["-t", f"{end - start:.3f}", "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
            "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", out.name]
    # Run inside the output folder so the subtitle filter needs no path escaping.
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=out.parent)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")
    return layout


# ---------------------------------------------------------------- orchestration

def _note(clip, meta, segments):
    spoken = " ".join(s["text"] for s in segments
                      if s["end"] > clip["start"] and s["start"] < clip["end"])
    lines = [f"Clip title: {clip['title']}"]
    if clip["hook"]:
        lines.append(f"Hook: {clip['hook']}")
    if clip["what_happens"]:
        lines.append(f"What happens: {clip['what_happens']}")
    if meta["channel"]:
        lines.append(f"Clipped from {meta['channel']}'s video {meta['title']!r}. "
                     f"Credit {meta['channel']} in the caption.")
    if spoken:
        lines.append(f"What is said in the clip: {spoken[:1200]}")
    return "\n".join(lines)


def run(url, cfg, count=None, start=None, end=None, queue=True):
    """Returns the list of finished clip files (already in inbox/ when queue=True)."""
    c = dict(cfg["clipper"])
    if count:
        c["count"] = count
    job = time.strftime("%Y%m%d-%H%M%S")
    work = config.DATA / "clips" / job
    out_dir = work / "out"
    out_dir.mkdir(parents=True)

    src, meta, chat = download(url, work, c["max_height"], parse_time(start), parse_time(end))
    info = video.probe(src)
    if info["duration"] < c["min_seconds"]:
        raise RuntimeError(f"The video is only {info['duration']:.0f}s, shorter than one clip")
    log.info("Source: %s (%.1f min, %d chat messages)",
             meta["title"], info["duration"] / 60, len(chat))

    segments, curve, spoken = [], [], None
    if info["acodec"]:
        wav = extract_audio(src, work)
        curve = hype_curve(loudness(wav), chat)
        try:
            segments, spoken = transcribe(wav, c["whisper_model"], c["language"])
            log.info("Spoken language: %s, %d lines", spoken, len(segments))
            (work / "transcript.json").write_text(json.dumps(segments, ensure_ascii=False))
        except Exception as e:
            log.warning("Transcription failed (%s). Continuing on audio/chat signals only, "
                        "without captions.", str(e)[:200])
        wav.unlink(missing_ok=True)

    clips = []
    if segments:
        peaks = top_peaks(curve, 25, 30)
        chat_peaks = top_peaks(hype_curve([0.0] * len(curve), chat), 25, 30) if chat else []
        try:
            clips = pick_with_ai(segments, peaks, chat_peaks, meta, info["duration"], c, cfg)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            log.warning("AI moment selection unavailable (%s); "
                        "falling back to loudness/chat peaks", str(e)[:160])
    if not clips:
        if not len(curve):
            raise RuntimeError("No audio, transcript or chat to find moments with")
        clips = pick_by_signals(curve, meta, c)
    clips = fit_clips(clips, segments, info["duration"], c)
    if not clips:
        raise RuntimeError("No usable moments found in this video")
    if not info["acodec"]:
        log.warning("The source has no audio track; clips will be silent")

    # Captions go on in the other language (English speech -> Arabic text and the reverse).
    caption_segments = [s for s in segments
                        if any(s["end"] > k["start"] and s["start"] < k["end"] for k in clips)]
    target = caption_target(spoken, c["captions_language"])
    if c["captions"] and caption_segments and target != spoken:
        try:
            caption_segments = translate_segments(caption_segments, target, cfg)
            log.info("Captions translated %s -> %s", spoken, target)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            log.warning("Caption translation unavailable (%s); captions stay in the spoken "
                        "language", str(e)[:160])

    slug = re.sub(r"[^\w]+", "-", meta["title"], flags=re.UNICODE).strip("-")[:40] or "clip"
    inbox = config.ROOT / "inbox"
    finished = []
    for n, clip in enumerate(clips, 1):
        out = out_dir / f"{slug}-{job[-6:]}-{n:02d}.mp4"
        layout = render(src, info, clip, caption_segments, out, c, cfg["orientation"])
        log.info("Clip %d/%d: %.0fs-%.0fs (%.0fs, score %d, %s) %s", n, len(clips),
                 clip["start"], clip["end"], clip["end"] - clip["start"], clip["score"],
                 layout, clip["title"])
        note = _note(clip, meta, segments)
        if queue:
            inbox.mkdir(exist_ok=True)
            (inbox / out.with_suffix(".txt").name).write_text(note, encoding="utf-8")
            # Land the video under a name the watcher ignores, then rename in one step.
            part = inbox / (out.name + ".part")
            shutil.move(out, part)
            part.rename(inbox / out.name)
            finished.append(inbox / out.name)
        else:
            out.with_suffix(".txt").write_text(note, encoding="utf-8")
            finished.append(out)
    if src.is_relative_to(work):
        src.unlink()  # the download is large and the clips are what matter
    return finished
