"""Link in -> best moments found -> vertical 30-80s clips with captions -> inbox/.

Works on YouTube videos, Twitch VODs/clips (anything yt-dlp can fetch) and local files.
Moments are chosen by the AI model (Claude or Gemini) from the transcript plus two hype signals that matter for
streams: loudness spikes and chat-message spikes.
"""
import json
import logging
import os
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


class Cancelled(RuntimeError):
    pass


SECTIONS_FROM = 15 * 60  # longer remote videos: fetch only the clips' sections


def _ydl_opts(work, tell, **extra):
    return {"noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
            "js_runtimes": js_runtimes(), "outtmpl": str(work / "source.%(ext)s"),
            "progress_hooks": [lambda d: _download_progress(d, tell)], **extra}


def probe_url(url):
    """yt-dlp's description of a link, without downloading anything."""
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts(Path("."), lambda *a: None)) as ydl:
        return ydl.extract_info(url, download=False, process=False)


def is_live(info):
    return bool(info.get("is_live")) or info.get("live_status") in ("is_live", "is_upcoming")


def _video_format(max_height):
    # The highest resolution up to the limit, then the highest frame rate.
    return {"format": "bv*+ba/b", "format_sort": [f"res:{max_height}", "fps"],
            "merge_output_format": "mp4"}


def download(url, work, max_height, start=None, end=None, tell=lambda *a: None, info=None):
    """Returns (media path, {title, channel, url, duration, remote}, chat timestamps).

    A long remote video is fetched as audio only (a small fraction of the size): the
    moments are found from it, and only their sections are fetched in video later
    (see fetch_sections). Shorter videos and local files come whole."""
    local = Path(url).expanduser()
    if local.exists():
        return local, {"title": local.stem, "channel": "", "url": "", "remote": False}, []

    import yt_dlp

    info = info or probe_url(url)
    if is_live(info):
        raise RuntimeError("live")  # handled by run_live
    duration = float(info.get("duration") or 0)
    if start is not None or end is not None:
        duration = (end or duration) - (start or 0)
    audio_only = duration >= SECTIONS_FROM and start is None and end is None
    opts = _ydl_opts(work, tell, **({"format": "bestaudio/best"} if audio_only
                                    else _video_format(max_height)))
    if start is not None or end is not None:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(
            None, [(start or 0, end or float("inf"))])
    log.info("Downloading %s (%s)", url, "audio only; video sections come later"
             if audio_only else "video")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.process_ie_result(info, download=True)
    path = next(p for p in work.glob("source.*") if not p.name.endswith((".part", ".ytdl")))
    meta = {"title": info.get("title") or "", "url": info.get("webpage_url") or url,
            "channel": info.get("uploader") or info.get("channel") or "",
            "remote": audio_only, "max_height": max_height, "info": info}

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


def fetch_sections(meta, clips, work, tell=lambda *a: None):
    """Download just the clips' stretches in video. Returns {clip index: (file, offset)}
    where offset is the source time at which that file starts."""
    import yt_dlp

    ranges = [(max(0.0, k["start"] - 1), k["end"] + 1) for k in clips]
    secdir = work / "sections"
    secdir.mkdir(exist_ok=True)
    opts = _ydl_opts(work, tell, **_video_format(meta["max_height"]))
    opts.update({
        "outtmpl": str(secdir / "s%(section_start)09.3f.%(ext)s"),
        "download_ranges": yt_dlp.utils.download_range_func(None, ranges),
        # Exact cut points, so caption timing lines up with the clip.
        "force_keyframes_at_cuts": True,
    })
    log.info("Downloading %d video section(s) instead of the whole video", len(ranges))
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.process_ie_result(meta["info"], download=True)
    files = {}
    for path in secdir.iterdir():
        m = re.match(r"s(\d+\.\d+)\.", path.name)
        if m and not path.name.endswith((".part", ".ytdl")):
            files[round(float(m.group(1)), 1)] = path
    out = {}
    for i, (start, _) in enumerate(ranges):
        hit = files.get(round(start, 1))
        if hit:
            out[i] = (hit, start)
    if len(out) < len(ranges):
        raise RuntimeError(f"only {len(out)} of {len(ranges)} sections downloaded")
    return out


def js_runtimes():
    """YouTube needs a JavaScript runtime for yt-dlp to see its full-quality formats.
    The `deno` package ships one; Node.js is used too when it is installed."""
    runtimes = {}
    try:
        from deno import find_deno_bin
        runtimes["deno"] = {"path": find_deno_bin()}
    except Exception:
        if shutil.which("deno"):
            runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    return runtimes or {"deno": {}}


def _download_progress(d, tell):
    if d.get("status") == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            tell("download", d.get("downloaded_bytes", 0) / total)


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


def top_peaks(curve, count, gap, floor=None):
    """Highest points of the curve, at least `gap` seconds apart."""
    import numpy as np

    peaks = []
    for i in np.argsort(curve)[::-1]:
        if floor is not None and peaks and curve[i] < floor:
            break  # the rest is ordinary footage, not a moment
        if all(abs(int(i) - p) >= gap for p in peaks):
            peaks.append(int(i))
            if len(peaks) == count:
                break
    return peaks


# ---------------------------------------------------------------- transcript

def transcribe(wav, model_name, language, tell=lambda *a: None, device="cpu"):
    """([{start, end, text, words: [{start, end, word}]}], spoken language code).
    The processor is the default: on Windows a graphics card without its CUDA
    libraries can crash the whole program instead of raising an error."""
    if device != "gpu":
        return _transcribe(wav, model_name, language, tell, "cpu", "int8")
    try:
        return _transcribe(wav, model_name, language, tell, "cuda", "float16")
    except Exception as e:
        # The usual cause on Windows: a graphics card is detected but its CUDA
        # libraries are not installed. The processor always works, just slower.
        log.warning("Transcription on the default device failed (%s); retrying on the "
                    "processor", str(e)[:160])
        return _transcribe(wav, model_name, language, tell, "cpu", "int8")


def read_wav(wav):
    """16 kHz mono PCM WAV -> float32 samples in [-1, 1], as Whisper expects."""
    import numpy as np

    with wave.open(str(wav)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError("audio must be 16 kHz mono 16-bit")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _transcribe(wav, model_name, language, tell, device, compute_type):
    from faster_whisper import WhisperModel

    log.info("Transcribing with whisper '%s' (first run downloads the model)", model_name)
    model = WhisperModel(model_name, device=device, compute_type=compute_type,
                         cpu_threads=os.cpu_count() or 4)
    # Hand over the samples ourselves: faster-whisper's own file reader uses PyAV,
    # and newer PyAV releases break it ("unexpected keyword argument 'metadata_errors'").
    audio = read_wav(wav)
    try:
        # Batched decoding of the speech parts, one beam: several times faster than
        # the default, with nearly the same text.
        from faster_whisper import BatchedInferencePipeline
        segments, info = BatchedInferencePipeline(model=model).transcribe(
            audio, language=language, word_timestamps=True, beam_size=1,
            batch_size=8 if device == "cpu" else 16)
        return _collect(segments, info, tell), info.language
    except Cancelled:
        raise
    except Exception as e:  # an older or newer faster-whisper: the plain way works too
        log.info("Batched transcription unavailable (%s); using the standard one",
                 str(e)[:120])
    segments, info = model.transcribe(audio, language=language, beam_size=1,
                                      word_timestamps=True, vad_filter=True)
    return _collect(segments, info, tell), info.language


def _collect(segments, info, tell):
    out, last_log = [], time.time()
    for s in segments:
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                    "words": [{"start": w.start, "end": w.end, "word": w.word.strip()}
                              for w in (s.words or [])]})
        if info.duration:
            tell("transcribe", min(s.end / info.duration, 1.0))
        if time.time() - last_log > 30:
            log.info("  transcribed %.0f of %.0f min", s.end / 60, info.duration / 60)
            last_log = time.time()
    return out


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
    if c.get("auto"):
        brief.append(
            "How many is up to the video: return only moments strong enough to pull views "
            "on their own (hype, shock, big laughs, clutch plays, heated or surprising "
            f"lines), each scored honestly. A score under {c['min_score']} will be dropped, "
            "so don't pad the list and don't leave a strong one out: a dull video may have "
            "just one or two, a packed one many.")
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
    floor = 1.0 if c.get("auto") else None  # a clear spike above the video's norm
    for rank, peak in enumerate(top_peaks(curve, c["count"], c["max_seconds"], floor)):
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

LANG_NAMES = {"ar": "Modern Standard Arabic (الفصحى)", "en": "standard English"}

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
keep them short and clear, no longer than the original. Use the standard written form of \
the target language for every line: Arabic in Modern Standard Arabic (الفصحى), never a \
dialect, even when the speaker is casual or swears; English in plain standard English. \
Every line must be entirely in the target language: no words left in the source \
language and no mixing, except names, game titles and brands, written the way readers of \
the target language usually write them. Speech-recognition \
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
    try:
        data, _ = llm.ask_json(
            TRANSLATE_SYSTEM, f"Translate into {LANG_NAMES.get(target, target)}:\n\n{numbered}",
            TRANSLATE_SCHEMA, cfg, max_tokens=64000)
        lines = {l["i"]: l["text"].strip() for l in data["lines"]}
        if len(lines) < len(segments) * 0.9:
            raise RuntimeError(f"translation returned {len(lines)} of {len(segments)} lines")
    except (RuntimeError, ValueError, KeyError, TypeError) as e:
        # No AI available: a plainer keyless translation still flips the language.
        log.warning("AI translation unavailable (%s); using the basic translator",
                    str(e)[:160])
        from . import translate
        lines = dict(enumerate(translate.lines([s["text"] for s in segments], target)))

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


def render(src, meta, clip, caption_segments, out, c, orientation, fps=60):
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
        chain = f"[0:v]crop=ih*{W}/{H}:ih:{crop_x}:0,scale={tw}:{th}"
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
            "-r", str(video.out_fps(meta, fps)), *video.encoder()[1],
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", out.name]
    # Run inside the output folder so the subtitle filter needs no path escaping.
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=out.parent)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")
    return layout


# ---------------------------------------------------------------- orchestration

def _note(clip, meta, segments, spoken_lang=None):
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
    if spoken_lang:
        lines.append(f"Spoken language: {spoken_lang}")
    return "\n".join(lines)


def _audio_info(path):
    """Duration and audio codec of any media file (it may have no video)."""
    data = json.loads(video._run(["ffprobe", "-v", "error", "-print_format", "json",
                                  "-show_format", "-show_streams", str(path)]))
    audio = next((x for x in data["streams"] if x["codec_type"] == "audio"), None)
    return {"duration": float(data["format"].get("duration") or 0),
            "acodec": audio.get("codec_name") if audio else None}


def _shift(segments, offset):
    """Segments with times moved by -offset (for a file that starts at `offset`)."""
    return [{**x, "start": x["start"] - offset, "end": x["end"] - offset,
             "words": [{**w, "start": w["start"] - offset, "end": w["end"] - offset}
                       for w in x["words"]]} for x in segments]


def run(url, cfg, count=None, start=None, end=None, queue=True, progress=None, stop=None):
    """Returns the list of finished clip files (already in inbox/ when queue=True).
    `progress(stage, fraction, detail)` is called as the work moves along. A live
    channel is followed until it ends or `stop` (a threading.Event) is set."""
    live = {"on": False}

    def tell(stage, fraction=None, detail=""):
        # Cancelling a normal job stops it at the next progress report (downloads
        # report many times a second). A live job finishes its last chunk instead.
        if stop is not None and stop.is_set() and not live["on"]:
            raise Cancelled("cancelled")
        if progress:
            progress(stage, fraction, detail)

    job = time.strftime("%Y%m%d-%H%M%S")
    work = config.DATA / "clips" / job
    work.mkdir(parents=True)
    max_height = 1440 if str(cfg.get("resolution")) == "1440" else 1080

    info = None
    if not Path(url).expanduser().exists():
        tell("download", 0)
        info = probe_url(url)
        if is_live(info):
            live["on"] = True
            return run_live(info, cfg, work, tell, stop, queue)

    tell("download", 0)
    try:
        src, meta, chat = download(url, work, max_height, parse_time(start), parse_time(end),
                                   tell, info)
        return process(src, meta, chat, cfg, work, tell, queue)
    except Exception as e:
        if stop is not None and stop.is_set():
            log.info("Cancelled: %s", url)
            raise Cancelled("cancelled") from e
        raise
    finally:
        # Downloads are large; the finished clips are already in inbox/ (or, when not
        # queued, in this job's out/ folder, which is kept).
        for item in work.iterdir() if work.exists() else []:
            if queue or item.name != "out":
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink()
        if queue:
            shutil.rmtree(work, ignore_errors=True)


import threading as _threading

# One clip job uses the whole processor; two at once would each take twice as long.
# Downloads and live recording go on in parallel, only this part waits its turn.
PROCESS_LOCK = _threading.Lock()


def process(src, meta, chat, cfg, work, tell, queue=True, part=""):
    """Find the strong moments in `src` and turn each into a finished clip."""
    if not PROCESS_LOCK.acquire(blocking=False):
        tell("waiting")
        PROCESS_LOCK.acquire()
    try:
        return _process(src, meta, chat, cfg, work, tell, queue, part)
    finally:
        PROCESS_LOCK.release()


def _process(src, meta, chat, cfg, work, tell, queue, part):
    c = dict(cfg["clipper"])
    # Always as many clips as the video has strong moments: a 30-minute video with
    # two great moments gives two clips, one with ten gives ten.
    c["auto"] = True
    out_dir = work / "out"
    out_dir.mkdir(exist_ok=True)
    remote = meta.get("remote")
    info = _audio_info(src) if remote else video.probe(src)
    # No real limit: as many as could fit without overlapping (100 at most).
    c["count"] = int(min(100, max(1, info["duration"] // c["min_seconds"])))
    if info["duration"] < c["min_seconds"]:
        raise RuntimeError(f"The video is only {info['duration']:.0f}s, shorter than one clip")
    log.info("Source: %s%s (%.1f min, %d chat messages)",
             meta["title"], part, info["duration"] / 60, len(chat))
    tell("audio", None, meta["title"])
    if not remote and min(info["width"], info["height"]) < 1000:
        log.warning("The best available source is only %dx%d; clips keep that size",
                    info["width"], info["height"])
        tell("warning", None, f"low_source: {info['width']}x{info['height']}")

    segments, curve, spoken = [], [], None
    if info["acodec"]:
        wav = extract_audio(src, work)
        curve = hype_curve(loudness(wav), chat)
        try:
            tell("transcribe", 0, meta["title"])
            segments, spoken = transcribe(wav, c["whisper_model"], c["language"], tell,
                                          c.get("whisper_device", "cpu"))
            log.info("Spoken language: %s, %d lines", spoken, len(segments))
        except Exception as e:
            log.warning("Transcription failed (%s). Continuing on audio/chat signals only, "
                        "without captions.", str(e)[:200])
            tell("warning", None, f"no_captions: {str(e)[:200]}")
        wav.unlink(missing_ok=True)

    clips = []
    tell("pick")
    if segments:
        peaks = top_peaks(curve, 25, 30)
        chat_peaks = top_peaks(hype_curve([0.0] * len(curve), chat), 25, 30) if chat else []
        try:
            clips = pick_with_ai(segments, peaks, chat_peaks, meta, info["duration"], c, cfg)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            log.warning("AI moment selection unavailable (%s); "
                        "falling back to loudness/chat peaks", str(e)[:160])
            tell("warning", None, f"no_ai_pick: {str(e)[:200]}")
    if not clips:
        if not len(curve):
            raise RuntimeError("No audio, transcript or chat to find moments with")
        clips = pick_by_signals(curve, meta, c)
    if len(clips) > 1:
        strong = [k for k in clips if k["score"] >= c["min_score"]]
        if len(strong) < len(clips):
            log.info("Keeping %d strong moment(s) of %d (score >= %d)",
                     max(len(strong), 1), len(clips), c["min_score"])
        clips = strong or sorted(clips, key=lambda k: -k["score"])[:1]
    clips = fit_clips(clips, segments, info["duration"], c)
    if not clips:
        log.info("No strong moment in %s%s", meta["title"], part)
        return []
    if not info["acodec"]:
        log.warning("The source has no audio track; clips will be silent")

    # Captions go on in the other language (English speech -> Arabic text and the reverse).
    caption_segments = [x for x in segments
                        if any(x["end"] > k["start"] and x["start"] < k["end"] for k in clips)]
    target = caption_target(spoken, c["captions_language"])
    if c["captions"] and caption_segments and target != spoken:
        tell("translate")
        try:
            caption_segments = translate_segments(caption_segments, target, cfg)
            log.info("Captions translated %s -> %s", spoken, target)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            log.warning("Caption translation unavailable (%s); captions stay in the spoken "
                        "language", str(e)[:160])
            tell("warning", None, f"no_translation: {str(e)[:200]}")

    sections = {}
    if remote:
        tell("download", None, "sections")
        sections = fetch_sections(meta, clips, work, tell)

    slug = re.sub(r"[^\w]+", "-", meta["title"], flags=re.UNICODE).strip("-")[:40] or "clip"
    stamp = work.name[-6:] + part.replace(" #", "-p")
    inbox = config.ROOT / "inbox"
    enc = video.encoder()[0]
    finished = []
    for n, clip in enumerate(clips, 1):
        out = out_dir / f"{slug}-{stamp}-{n:02d}.mp4"
        tell("render", (n - 1) / len(clips), f"{n}/{len(clips)}")
        began = time.time()
        if remote:  # render from this clip's own section file
            file, offset = sections[n - 1]
            c_src, c_info = file, video.probe(file)
            c_clip = {**clip, "start": clip["start"] - offset, "end": clip["end"] - offset}
            c_caps = _shift(caption_segments, offset)
            if n == 1 and min(c_info["width"], c_info["height"]) < 1000:
                tell("warning", None, f"low_source: {c_info['width']}x{c_info['height']}")
        else:
            c_src, c_info, c_clip, c_caps = src, info, clip, caption_segments
        layout = render(c_src, c_info, c_clip, c_caps, out, c, cfg["orientation"], cfg["fps"])
        # YouTube can take the other orientation of a landscape moment: a vertical
        # render uploads as a Short, a landscape one as a regular video.
        alt = None
        wide_source = c_info["width"] > c_info["height"] * 1.1
        main_is_wide = wide_source and cfg["orientation"] == "original"
        mode = cfg["youtube_mode"]
        if wide_source and "youtube" in cfg["platforms"] and (
                mode == "both" or (mode == "short" and main_is_wide)):
            alt = out.with_suffix(".alt.mp4")
            render(c_src, c_info, c_clip, c_caps, alt, c,
                   "vertical" if main_is_wide else "original", cfg["fps"])
        log.info("Clip %d/%d: %.0fs-%.0fs (%.0fs, score %d, %s) %s [%s, %.0fs]", n,
                 len(clips), clip["start"], clip["end"], clip["end"] - clip["start"],
                 clip["score"], layout, clip["title"], enc, time.time() - began)
        note = _note(clip, meta, segments, spoken)
        if queue:
            inbox.mkdir(exist_ok=True)
            (inbox / out.with_suffix(".txt").name).write_text(note, encoding="utf-8")
            # Land the video under a name the watcher ignores, then rename in one step.
            if alt:
                shutil.move(alt, inbox / alt.name)
            partial = inbox / (out.name + ".part")
            shutil.move(out, partial)
            partial.rename(inbox / out.name)
            finished.append(inbox / out.name)
        else:
            out.with_suffix(".txt").write_text(note, encoding="utf-8")
            finished.append(out)
    if sections:
        shutil.rmtree(work / "sections", ignore_errors=True)
    return finished


# ---------------------------------------------------------------- live streams

LIVE_CHUNK = 5 * 60  # seconds of stream per batch of clips: the first clips come ~5 min in


def _twitch_chat(channel, started, stamps, stop):
    """Read-only anonymous Twitch chat: the time of every message, for hype spikes."""
    import socket
    try:
        sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
        sock.sendall(f"NICK justinfan{int(time.time()) % 100000}\r\nJOIN #{channel}\r\n"
                     .encode())
        sock.settimeout(5)
        buf = b""
        while not stop.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            buf += data
            *lines, buf = buf.split(b"\r\n")
            for line in lines:
                if line.startswith(b"PING"):
                    sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                elif b" PRIVMSG #" in line:
                    stamps.append(time.time() - started)
        sock.close()
    except OSError as e:
        log.info("Twitch chat not available (%s)", e)


def run_live(info, cfg, work, tell, stop, queue=True):
    """Record a live stream in chunks and make clips from each chunk while the
    stream goes on, until it ends or `stop` is set."""
    import threading

    import yt_dlp

    stop = stop or threading.Event()
    max_height = 1440 if str(cfg.get("resolution")) == "1440" else 1080
    with yt_dlp.YoutubeDL(_ydl_opts(work, tell, **{
            "format": f"best[height<={max_height}]/best"})) as ydl:
        chosen = ydl.process_ie_result(info, download=False)
    stream = chosen.get("url") or info.get("url")
    if not stream:
        raise RuntimeError("Could not open the live stream")
    title = info.get("title") or info.get("uploader") or "live"
    meta = {"title": title, "channel": info.get("uploader") or info.get("channel") or "",
            "url": info.get("webpage_url") or "", "remote": False}
    log.info("Following live stream %s; clips every %d min", title, LIVE_CHUNK // 60)
    tell("title", None, title)

    rec = work / "live"
    rec.mkdir()
    started = time.time()
    headers = "".join(f"{k}: {v}\r\n" for k, v in (chosen.get("http_headers") or {}).items())
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", stream, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
            "-segment_time", str(LIVE_CHUNK), "-reset_timestamps", "1",
            str(rec / "chunk%04d.ts")]
    errlog = open(work / "recorder.log", "wb")  # a file, so a chatty ffmpeg can't stall
    recorder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errlog)

    stamps = []
    host = (info.get("extractor_key") or "").lower()
    if "twitch" in host and info.get("uploader_id"):
        threading.Thread(target=_twitch_chat, args=(info["uploader_id"].lower(), started,
                                                    stamps, stop), daemon=True).start()

    finished, done_chunks = [], 0

    def handle(path, index):
        offset = index * LIVE_CHUNK
        chat = [t - offset for t in stamps if offset <= t < offset + LIVE_CHUNK]
        try:
            finished.extend(process(path, meta, chat, cfg, work, tell, queue,
                                    part=f" #{index + 1}"))
        except Exception as e:
            if path.exists() and video.probe(path)["duration"] < cfg["clipper"]["min_seconds"]:
                log.info("Live chunk %d is too short for a clip", index + 1)
            else:
                log.error("Live chunk %d: %s", index + 1, e)
        path.unlink(missing_ok=True)

    while True:
        # Check the recorder first: listing first could miss a chunk written just
        # before it exits.
        running = recorder.poll() is None
        chunks = sorted(rec.glob("chunk*.ts"))
        recorded = time.time() - started
        tell("live", None, f"{int(recorded // 60)} min · {len(finished)} clips")
        # A chunk is complete once the next one has started (or recording stopped).
        ready = chunks if not running else chunks[:-1]
        for path in ready:  # processed chunks are deleted, so these are all new
            handle(path, int(path.stem.removeprefix("chunk")))
            done_chunks += 1
        if not running:
            break
        if stop.is_set():
            try:  # "q" makes ffmpeg close the current chunk properly
                recorder.communicate(b"q", timeout=20)
            except subprocess.TimeoutExpired:
                recorder.kill()
            continue
        time.sleep(5)
    errlog.close()
    if recorder.returncode not in (0, 255, None) and not finished and done_chunks == 0:
        err = (work / "recorder.log").read_text(errors="replace")[-300:]
        raise RuntimeError(f"Recording the stream failed: {err}")
    stop.set()
    log.info("Live stream done: %d clip(s)", len(finished))
    return finished
