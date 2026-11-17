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
import threading as _threading
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
            "remote": audio_only, "max_height": max_height, "info": info,
            "offset": float(start or 0)}

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


def fetch_section(meta, clip, work, n, tell=lambda *a: None):
    """Download just one clip's stretch in video. Returns (file, offset), offset being
    the source time at which that file starts."""
    import yt_dlp

    start, end = max(0.0, clip["start"] - 1), clip["end"] + 1
    secdir = work / "sections"
    secdir.mkdir(exist_ok=True)
    opts = _ydl_opts(work, tell, **_video_format(meta["max_height"]))
    opts.update({
        "outtmpl": str(secdir / f"s{n:03d}.%(ext)s"),
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start, end)]),
        # Exact cut points, so caption timing lines up with the clip.
        "force_keyframes_at_cuts": True,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.process_ie_result(meta["info"], download=True)
    file = next((p for p in secdir.glob(f"s{n:03d}.*")
                 if not p.name.endswith((".part", ".ytdl"))), None)
    if file is None:
        raise RuntimeError(f"section {n} was not downloaded")
    return file, start


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


def top_peaks(curve, count, gap, floor=None, strict=False):
    """Highest points of the curve, at least `gap` seconds apart."""
    import numpy as np

    peaks = []
    for i in np.argsort(curve)[::-1]:
        if floor is not None and (peaks or strict) and curve[i] < floor:
            break  # the rest is ordinary footage, not a moment
        if all(abs(int(i) - p) >= gap for p in peaks):
            peaks.append(int(i))
            if len(peaks) == count:
                break
    return peaks


# ---------------------------------------------------------------- transcript

SPEED_MODELS = {"fast": "base", "balanced": "small", "accurate": "medium"}


def whisper_model(cfg):
    """The speech model for the chosen speed: 'fast' (default) is several times quicker
    than 'balanced', and the AI cleans up its occasional mishearing when translating."""
    return SPEED_MODELS.get(cfg.get("speed_mode"), cfg["clipper"].get("whisper_model", "base"))


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


_MODELS = {}


def _model(name, device="cpu", compute_type="int8"):
    """Speech models stay loaded between links: loading one takes seconds each time."""
    from faster_whisper import WhisperModel

    key = (name, device, compute_type)
    if key not in _MODELS:
        log.info("Loading speech model '%s' (the first time downloads it)", name)
        _MODELS[key] = WhisperModel(name, device=device, compute_type=compute_type,
                                    cpu_threads=os.cpu_count() or 4)
    return _MODELS[key]


# English speech gets a model trained for English only: far fewer misheard words than
# "small", which matter most when the words are then translated into Arabic.
ENGLISH_CAPTION_MODEL = "distil-large-v3"


def caption_model(cfg, spoken):
    if spoken == "en" and cfg.get("speed_mode") in ("fast", "balanced", None):
        return ENGLISH_CAPTION_MODEL
    return CAPTION_MODELS.get(cfg.get("speed_mode"), "small")


def transcribe_clip(audio, start, end, model_name, language, device="cpu", hint=""):
    """Careful second pass over one clip with a bigger model and a full beam: these are
    the words that end up on screen. Times come back on the source's clock."""
    rate = 16000
    a, b = max(0, int((start - 0.5) * rate)), int((end + 0.5) * rate)
    compute = "int8" if device != "gpu" else "float16"
    model = _model(model_name, "cuda" if device == "gpu" else "cpu", compute)
    segments, info = model.transcribe(
        audio[a:b], language=language, beam_size=5, word_timestamps=True, vad_filter=True,
        condition_on_previous_text=False,
        # the title spells the names, games and slang the streamer is likely to say
        initial_prompt=hint[:200] or None)
    offset = a / rate
    out = []
    for x in segments:
        # Whisper invents words over music and game noise; drop what it isn't sure is speech.
        if x.no_speech_prob > 0.6 and x.avg_logprob < -0.8:
            continue
        out.append({"start": x.start + offset, "end": x.end + offset, "text": x.text.strip(),
                    "words": [{"start": w.start + offset, "end": w.end + offset,
                               "word": w.word.strip()} for w in (x.words or [])]})
    return out


def subtitle_units(segments, start, end):
    """Cut speech into subtitle-sized sentences (a pause, a sentence end, about 4 seconds
    or 9 words), each shown whole at its own time. Translating whole sentences in context
    reads right; translating 3-word fragments does not."""
    words = [w for x in segments for w in x["words"]
             if w["word"] and w["end"] > start and w["start"] < end]
    units, cur = [], []

    def close():
        if cur:
            units.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                          "text": " ".join(w["word"] for w in cur), "words": [],
                          "phrase": True})
            cur.clear()

    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > 0.5 or w["end"] - cur[0]["start"] > 4.2
                    or len(cur) >= 9):
            close()
        cur.append(w)
        if len(cur) >= 3 and re.search(r"[.?!؟]$", w["word"]):
            close()
    close()
    if not units:  # no word timings: fall back to whole recognised lines
        units = [{"start": x["start"], "end": x["end"], "text": x["text"], "words": [],
                  "phrase": True} for x in segments
                 if x["end"] > start and x["start"] < end and x["text"]]
    return units


CAPTION_MODELS = {"fast": "small", "balanced": "medium", "accurate": "large-v3"}


def _transcribe(wav, model_name, language, tell, device, compute_type):
    from faster_whisper import WhisperModel

    model = _model(model_name, device, compute_type)
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

You get the transcript with a start time in seconds in front of every line. Lines can carry \
signal marks measured on the video itself:
  🔊 the audio got unusually loud (🔊🔊 / 🔊🔊🔊 = much louder): shouting, hype, a big moment
  💬 chat suddenly sped up (💬💬 / 💬💬💬 = a flood)
  🔁 viewers replayed this part the most (YouTube's "most replayed")
Signals mark the REACTION to something that happened a few seconds earlier, so a clip has \
to begin before the spike, at the setup (the play starting, the question, the first line of \
the story), never at the scream itself. Lines with several marks together are the \
strongest evidence of a moment, but a quiet line can still be great (a funny line, a story, \
a hot take): judge the words too.

What makes a clip work:
- It stands alone. Someone who never saw the stream understands it from its first second \
to its last, without needing what came before.
- It opens on a hook: a question, a bold claim, the start of a story, the moment the play \
begins. Never open mid-sentence or on filler.
- It has a payoff (the punchline, the win or fail, the answer, the reaction) and ends \
right after it, on a finished sentence.
- Strong candidates: funny or shocking moments, clutch plays and fails, big reactions, hot \
takes, a story with a twist, a genuinely useful tip explained in full, an argument or a \
rage moment, a reveal, an unexpected twist, a wholesome moment with the audience.
- Weak candidates (score them low or leave them out): menus, loading, waiting, reading \
donations one after another, routine gameplay talk, "let me check something", greetings and \
goodbyes, sponsor reads, anything that needs earlier context to be understood.
- Score as an editor who knows TikTok: 90+ only for something people would share or \
rewatch, 70-89 a solid clip, 50-69 fine but forgettable, under 50 not worth posting.

Start and end must be start times of transcript lines (end = the start of the first line \
that is NOT in the clip). Respect the length limits you are given exactly; a great moment \
that needs more room than the maximum should be tightened to its best part. Clips must not \
overlap. Quality over quantity: return fewer clips when the video doesn't have enough \
strong moments. `score` is 1-100 for how likely the clip is to hold a stranger's attention; \
order the list best first. Write title, hook and what_happens in the language spoken in \
the video."""


def _transcript_text(segments, marks=None):
    lines = []
    for s in segments:
        tag = (marks or {}).get(round(s["start"], 2), "")
        lines.append(f"[{s['start']:.0f}]{(' ' + tag) if tag else ''} {s['text']}")
    return "\n".join(lines)


def signal_marks(segments, levels, chat, heatmap):
    """Per transcript line, emoji marks for how strongly loudness, chat and replays
    spiked around it (so the AI sees the evidence next to the words)."""
    import numpy as np

    def z(x):
        x = np.asarray(x, dtype=np.float32)
        return (x - x.mean()) / x.std() if len(x) and x.std() > 0 else np.zeros_like(x)

    n = len(levels)
    curves = {}
    if n:
        curves["🔊"] = np.convolve(z(levels), np.ones(3) / 3, "same")
        if chat:
            counts = np.bincount([min(int(t), n - 1) for t in chat if t >= 0], minlength=n)
            curves["💬"] = z(np.convolve(counts, np.ones(10) / 10, "same"))
        if heatmap is not None and len(heatmap) == n:
            curves["🔁"] = z(heatmap)
    marks = {}
    for seg in segments:
        a, b = max(0, int(seg["start"]) - 1), min(n, int(seg["end"]) + 9)  # reactions lag
        tag = ""
        for sym, curve in curves.items():
            peak = float(curve[a:b].max()) if b > a else 0.0
            level = 3 if peak > 3 else 2 if peak > 2.2 else 1 if peak > 1.5 else 0
            tag += sym * level
        if tag:
            marks[round(seg["start"], 2)] = tag
    return marks


def heatmap_curve(meta, duration, offset=0.0):
    """YouTube's "most replayed" graph as one value per second, or None."""
    import numpy as np

    heat = (meta.get("info") or {}).get("heatmap") or []
    if not heat:
        return None
    out = np.zeros(int(duration) + 1, dtype=np.float32)
    for h in heat:
        a = int(max(0, h.get("start_time", 0) - offset))
        b = int(min(len(out), h.get("end_time", 0) - offset + 1))
        if b > a:
            out[a:b] = np.maximum(out[a:b], float(h.get("value") or 0))
    return out if out.any() else None


WINDOW = 20 * 60   # long videos are read in parts of this many seconds...
OVERLAP = 90       # ...overlapping a little, so a moment on a border is not cut in two


def _windows(segments, duration):
    if duration <= WINDOW * 1.25:
        return [segments]
    out, start = [], 0.0
    while start < duration:
        end = start + WINDOW
        part = [x for x in segments if x["end"] > start and x["start"] < end]
        if part:
            out.append(part)
        start = end - OVERLAP
    return out


def _ask_moments(part, marks, meta, duration, c, cfg, where=""):
    brief = [
        f"Video: {meta['title']!r} by {meta['channel'] or 'unknown'}, {duration:.0f} seconds long.",
        f"Find clips between {c['min_seconds']} and {c['max_seconds']} seconds long.",
    ]
    if where:
        brief.append(where)
    if c.get("auto"):
        brief.append(
            "How many is up to the video: return only moments strong enough to pull views "
            "on their own, each scored honestly. A score under "
            f"{c['min_score']} will be dropped, so don't pad the list and don't leave a "
            "strong one out: a dull part may have none, a packed one many.")
    brief.append("Transcript:\n" + _transcript_text(part, marks))
    data, who = llm.ask_json(SYSTEM, "\n\n".join(brief), SCHEMA, cfg, effort="high")
    return data["clips"], who


RERANK_SYSTEM = """You are the final editor choosing which short clips get posted on \
TikTok, Reels and Shorts. You get every candidate found in one video, each with its \
transcript and the signals measured on the video (🔊 loud, 💬 chat spike, 🔁 most replayed).

Compare them against each other and give each a final `score` 1-100 on ONE scale for the \
whole video: how likely is it to stop a stranger's scroll and be watched to the end? \
90+ only for something people would share or rewatch; under 50 for something not worth \
posting. Set `keep` false for duplicates of a better candidate, for clips that need \
earlier context, and for clips with no real payoff. You may move `start` earlier (to \
include the setup) or `end` (to stop right after the payoff), using times of the given \
lines and staying within the length limits. Return every candidate by its index `i`."""

RERANK_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "score": {"type": "integer"},
                       "keep": {"type": "boolean"}, "start": {"type": "number"},
                       "end": {"type": "number"}},
        "required": ["i", "score", "keep", "start", "end"],
    }}},
    "required": ["clips"],
}


def _rerank(cands, segments, marks, meta, c, cfg):
    """One more look at all candidates together: one scale for the whole video, setup
    included, weak and duplicate ones out."""
    blocks = []
    for i, k in enumerate(cands):
        lines = [x for x in segments if x["end"] > k["start"] - 20 and x["start"] < k["end"] + 10]
        blocks.append(f"## Candidate {i}: {k['start']:.0f}s-{k['end']:.0f}s, first score "
                      f"{k['score']}\nTitle: {k['title']}\nWhat happens: {k['what_happens']}\n"
                      f"Transcript (with 20 s before it, in case the setup is there):\n"
                      + _transcript_text(lines, marks))
    brief = (f"Video: {meta['title']!r} by {meta['channel'] or 'unknown'}.\n"
             f"Clips must be {c['min_seconds']}-{c['max_seconds']} seconds long.\n\n"
             + "\n\n".join(blocks))
    data, who = llm.ask_json(RERANK_SYSTEM, brief, RERANK_SCHEMA, cfg, effort="high")
    out = []
    for r in data["clips"]:
        i = r.get("i")
        if not isinstance(i, int) or not 0 <= i < len(cands) or not r.get("keep", True):
            continue
        k = dict(cands[i])
        k["score"] = int(r["score"])
        start, end = float(r.get("start", k["start"])), float(r.get("end", k["end"]))
        # accept the edit only when it stays close to the moment that was found
        if abs(start - k["start"]) <= 45 and abs(end - k["end"]) <= 45 and end > start:
            k["start"], k["end"] = start, end
        out.append(k)
    log.info("%s ranked %d candidates, kept %d", who, len(cands), len(out))
    return out


def _dedupe(cands):
    kept = []
    for k in sorted(cands, key=lambda x: -x["score"]):
        overlap = [o for o in kept if min(o["end"], k["end"]) - max(o["start"], k["start"])
                   > 0.5 * min(o["end"] - o["start"], k["end"] - k["start"])]
        if not overlap:
            kept.append(k)
    return kept


def pick_with_ai(segments, marks, meta, duration, c, cfg):
    """Candidates from each part of the video, then one final ranking of all of them."""
    windows = _windows(segments, duration)
    cands, problems = [], []
    for n, part in enumerate(windows, 1):
        where = (f"This is part {n} of {len(windows)} of the video "
                 f"({part[0]['start']:.0f}s-{part[-1]['end']:.0f}s); look only at this part."
                 if len(windows) > 1 else "")
        if _cancelled():
            raise Cancelled()
        try:
            got, who = _ask_moments(part, marks, meta, duration, c, cfg, where)
            cands += got
            log.info("%s proposed %d moments%s", who, len(got),
                     f" in part {n}/{len(windows)}" if len(windows) > 1 else "")
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            problems.append(str(e))
            if len(windows) == 1:
                raise
    if not cands and problems:
        raise RuntimeError(problems[0])
    cands = _dedupe(cands)
    if len(cands) > 1:
        try:
            cands = _rerank(cands, segments, marks, meta, c, cfg)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            log.info("Final ranking unavailable (%s); keeping the first scores", str(e)[:120])
    return cands


def pick_by_signals(curve, meta, c):
    """No transcript or no Claude: take the hype peaks, with lead-in before each."""
    length = (c["min_seconds"] + c["max_seconds"]) / 2
    clips = []
    floor = 1.0 if c.get("auto") else None  # a clear spike above the video's norm
    for rank, peak in enumerate(top_peaks(curve, c["count"], c["max_seconds"], floor,
                                          strict=c.get("strict", False))):
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
        if starts:
            # Begin on the last line that starts at or just before the requested time, so
            # the setup is kept; the nearest line could be the one after it.
            before = [t for t in starts if t <= start + 0.75]
            start = max(before) if before else min(starts, key=lambda t: abs(t - start))
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

TRANSLATE_SYSTEM = """You are a professional subtitle translator for short gaming and \
streaming clips. You get the full speech of one clip as numbered subtitle lines, in order, \
plus what the video is. Translate every line so a native reader understands exactly what \
the speaker means at that moment.

Rules:
- Read the whole clip first. Translate the meaning in context, never word by word: slang, \
gaming talk and idioms become what a native speaker of the target language would say for \
the same thing ("I got him" = I killed / took him down; "he's one shot" = one hit from \
dying; "let's go" when cheering = a cheer, not "let us leave"; "no way" = disbelief).
- Arabic must be correct Modern Standard Arabic (الفصحى), in the simple, natural style of \
professional Arabic subtitles (like Netflix Arabic subtitles), not stiff written style: no \
dialect, no literal calques, and no needless formal openers (إنني، إنه لـ، لقد قمت بـ، \
يا له من) when a plain sentence says it. Short spoken lines stay short and spoken. Examples:
  "I hope it keeps going" -> أتمنى أن يستمر  (not: إنني آمل الاستمرار)
  "Let's go!" (cheering) -> هيا! / رائع!   (not: لنذهب)
  "No way!" -> مستحيل!  /  "Oh my God" -> يا إلهي!
  "That's crazy" -> هذا جنوني  /  "I'm dead" (laughing) -> سأموت من الضحك
  "He's one shot" -> بقيت له ضربة واحدة  /  "GG" -> مباراة جيدة
  "Corsair" (the brand) -> كورسير ; a question about it keeps its meaning, never a \
dictionary word like ملحقات for something that is not said.
- English must be natural standard English.
- Every line entirely in the target language. Keep names of people, games, weapons, maps \
and brands as readers of that language usually write them.
- Speech recognition makes mistakes: when a word is clearly misheard, translate what was \
meant from context. If a line is only a sound or filler (uh, um, ha), give a short natural \
equivalent or an empty string.
- Subtitle only the streamer (the main person talking to their audience). Lines that \
are clearly not the streamer, such as in-game character or announcer voices, game menu \
or kill-feed callouts, music lyrics, ads, text-to-speech donations or another player's \
voice chat, get an empty string so they are not shown.
- Each line is shown alone on screen for a couple of seconds: keep it short and clear, \
never longer than needed. Do not merge, split, reorder or skip lines: exactly one output \
line per input number. A line may continue the previous one's sentence: translate it as \
that continuation, so the two read correctly one after the other.
- Before answering, reread your translation as a native reader of the target language \
and fix anything awkward, wrong in grammar, or not what the speaker meant."""


def caption_target(spoken, setting):
    """Which language the on-video captions should be in.
    'swap': English speech -> Arabic captions, Arabic speech -> English, others -> Arabic."""
    if setting == "original":
        return spoken
    if setting == "swap":
        return "en" if spoken == "ar" else "ar"
    return setting


def _mostly_arabic(texts):
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return True
    arabic = sum(1 for t in texts if re.search(r"[\u0600-\u06FF]", t))
    return arabic >= len(texts) * 0.7


def translate_units(units, target, cfg, context=""):
    """Translate one clip's subtitle units in one request, with the clip's context.
    Returns (translated units, who translated). Falls back to the keyless translator."""
    if not units:
        return units, None
    numbered = "\n".join(f"{i}: {u['text']}" for i, u in enumerate(units))
    brief = (f"Video: {context}\n" if context else "") + \
        f"Translate into {LANG_NAMES.get(target, target)}:\n\n{numbered}"
    who, lines = None, None
    for attempt in range(2):
        try:
            data, who = llm.ask_json(TRANSLATE_SYSTEM, brief, TRANSLATE_SCHEMA, cfg,
                                     effort="high", max_tokens=32000)
            lines = {l["i"]: l["text"].strip() for l in data["lines"]}
            if len([i for i in range(len(units)) if i in lines]) < len(units) * 0.9:
                raise RuntimeError(f"translation returned {len(lines)} of {len(units)} lines")
            if target == "ar" and not _mostly_arabic(lines.values()):
                raise RuntimeError("the translation is not in Arabic")
            break
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            lines = None
            if attempt == 0 and llm.providers(cfg):
                # Free AI quotas run out per minute: wait one minute and ask again
                # rather than fall back to a word-by-word translation. (Rendering
                # carries on meanwhile, so this costs little.)
                log.info("AI translation failed (%s); asking again in a minute", str(e)[:120])
                for _ in range(60):
                    if _cancelled():
                        raise Cancelled()
                    time.sleep(1)
                continue
            log.warning("AI translation unavailable (%s); using the basic translator",
                        str(e)[:160])
    if lines is None:
        from . import translate
        try:
            lines = dict(enumerate(translate.lines_in_context([u["text"] for u in units],
                                                              target)))
            who = "basic"
        except Exception as e2:  # nothing reachable: keep the spoken words
            log.warning("Basic translator unavailable too (%s)", str(e2)[:120])
            return units, "none"
    out = [{**u, "text": lines.get(i, "")} for i, u in enumerate(units)]
    # A line the model dropped shows nothing rather than the wrong language.
    return [u for u in out if u["text"]], who


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


def _ass_header(font, fs, margin, tw, th, outline, shadow):
    return [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {tw}", f"PlayResY: {th}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,"
        f"100,100,0,0,1,{outline},{shadow},2,60,60,{margin},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


RLM = "\u200f"


def _clean(text):
    return text.replace("{", "(").replace("}", ")").replace("\\", "/").replace("\n", " ")


def _phrase_words(unit, t1):
    """A translated sentence has no word timings: share its time by word length."""
    words = unit["text"].split()
    total = sum(len(w) + 1 for w in words) or 1
    t, span, out = unit["start"], max(t1 - unit["start"], 0.3), []
    for w in words:
        d = span * (len(w) + 1) / total
        out.append({"start": t, "end": t + d, "word": w})
        t += d
    return out


def _chunks(words, size, hold):
    """Groups of a few words, CapCut style: split at pauses, sentence ends and size,
    and balanced so a 5-word sentence shows 3 + 2, not 4 + 1."""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) == size or w["start"] - cur[-1]["end"] > 0.6
                    or w["end"] - cur[0]["start"] > hold):
            groups.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.?!؟،,]$", w["word"]) and len(cur) >= 2:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    out = []
    for g in groups:  # rebalance a lone trailing word into its neighbour's line
        if out and len(g) == 1 and len(out[-1]) >= 3 and not re.search(r"[.?!؟]$", out[-1][-1]["word"]):
            g.insert(0, out[-1].pop())
        out.append(g)
    return [g for g in out if g]


def write_captions(segments, start, end, path, font, size, style="capcut"):
    """Burned-in subtitles timed to speech. 'capcut': a few big bold words at a time that
    pop in, with the word being said highlighted. 'sentence': one whole sentence at a
    time. Returns False when nothing is spoken."""
    tw, th = size
    vertical = th > tw
    shown = [x for x in segments if x["end"] > start and x["start"] < end]
    phrases = [x for x in shown if x.get("phrase")]

    if phrases and style == "sentence":
        fs, margin = (68, 420) if vertical else (56, 60)
        lines = _ass_header(font, fs, margin, tw, th, max(3, fs // 14), 2)
        for i, u in enumerate(phrases):
            t0 = max(0.0, u["start"] - start)
            nxt = phrases[i + 1]["start"] - start if i + 1 < len(phrases) else end - start
            t1 = min(max(u["end"] - start + 0.35, t0 + 1.2), nxt, end - start)
            if t1 > t0:
                text = _clean(u["text"])
                if re.search(r"[\u0590-\u08ff]", text):
                    text = RLM + text + RLM
                lines.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Cap,,0,0,0,,{text}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    if phrases:
        words = []
        for i, u in enumerate(phrases):
            nxt = phrases[i + 1]["start"] if i + 1 < len(phrases) else end
            words += _phrase_words(u, min(max(u["end"] + 0.3, u["start"] + 1.0), nxt))
    else:
        words = [w for x in shown for w in x["words"]
                 if w["word"] and w["end"] > start and w["start"] < end]
    if not words:
        return False

    # Big, bold, thick outline, in the lower middle where CapCut puts it.
    fs, margin = (96, 560) if vertical else (76, 150)
    per, hold = (3, 2.4) if vertical else (5, 3.0)
    lines = _ass_header(font, fs, margin, tw, th, max(5, fs // 12), 3)
    highlight = "&H0000E5FF&"  # warm yellow (ASS colours are blue-green-red)
    pop = r"{\fscx72\fscy72\t(0,90,\fscx108\fscy108)\t(90,160,\fscx100\fscy100)}"
    groups = _chunks(words, per, hold)
    rtl = any(re.search(r"[\u0590-\u08ff]", w["word"]) for w in words)
    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else end
        g_end = min(max(g[-1]["end"] + 0.25, g[0]["start"] + 0.5), g_end, end)
        for wi, w in enumerate(g):
            t0 = max(0.0, (g[0]["start"] if wi == 0 else w["start"]) - start)
            t1 = (g[wi + 1]["start"] if wi + 1 < len(g) else g_end) - start
            if t1 <= t0:
                continue
            if rtl:
                # Colour tags inside right-to-left text make the subtitle renderer
                # reorder the words, so Arabic lines stay one unbroken run: the group
                # pops in whole instead of highlighting word by word.
                if wi:
                    continue
                t1 = g_end - start
                # Right-to-left marks around the line: without them the renderer takes
                # the line as left-to-right and puts "!" or "." on the wrong side.
                text = RLM + " ".join(_clean(x["word"]) for x in g) + RLM
            else:
                text = " ".join(
                    (f"{{\\c{highlight}}}{_clean(x['word'])}{{\\c&H00FFFFFF&}}" if xi == wi
                     else _clean(x["word"])) for xi, x in enumerate(g))
            lines.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Cap,,0,0,0,,"
                         f"{pop if wi == 0 else ''}{text}")
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
                                       c["caption_font"], (tw, th),
                                       c.get("caption_style", "capcut")):
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
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", out.name]
    # Run inside the output folder so the subtitle filter needs no path escaping.
    code, err = _ffmpeg(cmd, out.parent)
    enc = video.encoder()
    if code != 0 and enc[0] != "libx264":
        # The graphics card refused this one (driver, size, format): use the processor
        # from now on rather than fail clips.
        log.warning("%s failed (%s); switching to the processor encoder", enc[0], err[-200:])
        video._encoder = video._CPU
        cmd = [x for x in cmd]
        i = cmd.index(enc[1][0])
        cmd[i:i + len(enc[1])] = video._CPU[1]
        code, err = _ffmpeg(cmd, out.parent)
    if code != 0:
        raise RuntimeError(f"ffmpeg failed: {err[-600:]}")
    return layout


def _ffmpeg(cmd, cwd):
    """Run ffmpeg, killing it at once if the job is cancelled. Returns (code, stderr)."""
    errfile = Path(cwd) / f".ffmpeg-{os.getpid()}-{_threading.get_ident()}.log"
    with open(errfile, "wb") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stderr=err)
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if _cancelled():
                    proc.kill()
                    proc.wait()
                    errfile.unlink(missing_ok=True)
                    raise Cancelled("cancelled")
    text = errfile.read_text(errors="replace").strip()
    errfile.unlink(missing_ok=True)
    return proc.returncode, text


# ---------------------------------------------------------------- orchestration

def _note(clip, meta, segments, spoken_lang=None):
    spoken = " ".join(s["text"] for s in segments
                      if s["end"] > clip["start"] and s["start"] < clip["end"])
    lines = [f"Clip title: {clip['title']}", f"Moment score: {int(clip.get('score') or 0)}"]
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


_ctx = _threading.local()  # the running job's cancel event, for this thread


def _cancelled():
    cancel = getattr(_ctx, "cancel", None)
    return cancel is not None and cancel.is_set()


def run(url, cfg, count=None, start=None, end=None, queue=True, progress=None, stop=None,
        cancel=None):
    """Returns the list of finished clip files (already in inbox/ when queue=True).
    `progress(stage, fraction, detail)` is called as the work moves along.
    `cancel` (a threading.Event) ends the job at once, killing any running step.
    `stop` ends a live stream gracefully: recording stops and what was recorded is
    still clipped. For a normal link, `stop` cancels as well."""
    live = {"on": False}
    cancel = cancel or _threading.Event()
    stop = stop or _threading.Event()
    _ctx.cancel = cancel

    def tell(stage, fraction=None, detail=""):
        # Downloads report many times a second, so this ends them quickly too.
        if cancel.is_set() or (stop.is_set() and not live["on"]):
            cancel.set()
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
            try:
                return run_live(info, cfg, work, tell, stop, queue, cancel)
            finally:
                shutil.rmtree(work, ignore_errors=True)

    tell("download", 0)
    try:
        src, meta, chat = download(url, work, max_height, parse_time(start), parse_time(end),
                                   tell, info)
        return process(src, meta, chat, cfg, work, tell, queue)
    except Exception as e:
        if cancel.is_set():
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

# Transcribing and rendering use the whole processor; two at once would each take
# twice as long. Only those steps take turns: downloads, live recording and waiting
# on the AI service never hold anyone up.
PROCESS_LOCK = _threading.Lock()


class _heavy:
    def __init__(self, tell, stage):
        self.tell, self.stage = tell, stage

    def __enter__(self):
        if not PROCESS_LOCK.acquire(blocking=False):
            self.tell("waiting")
            while not PROCESS_LOCK.acquire(timeout=0.5):
                if _cancelled():  # cancelling must not wait for another link's turn
                    raise Cancelled("cancelled")
            self.tell(self.stage)

    def __exit__(self, *exc):
        PROCESS_LOCK.release()


def process(src, meta, chat, cfg, work, tell, queue=True, part=""):
    """Find the strong moments in `src` and turn each into a finished clip."""
    return _process(src, meta, chat, cfg, work, tell, queue, part)


def _process(src, meta, chat, cfg, work, tell, queue, part):
    c = dict(cfg["clipper"])
    # Always as many clips as the video has strong moments: a 30-minute video with
    # two great moments gives two clips, one with ten gives ten.
    c["auto"] = True
    c["strict"] = bool(part)  # live chunks: only real highlights
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

    segments, curve, spoken, wav, levels, heat = [], [], None, None, [], None
    if info["acodec"]:
        wav = extract_audio(src, work)
        levels = loudness(wav)
        heat = heatmap_curve(meta, len(levels) - 1, meta.get("offset", 0.0))
        curve = hype_curve(levels, chat)
        if heat is not None and len(heat) == len(curve):
            import numpy as np
            hz = (heat - heat.mean()) / heat.std() if heat.std() > 0 else heat * 0
            curve = curve + 0.8 * np.convolve(hz, np.ones(15) / 15, "same")
            log.info("Using YouTube's most-replayed graph as a signal")
        try:
            tell("transcribe", 0, meta["title"])
            with _heavy(tell, "transcribe"):
                segments, spoken = transcribe(wav, whisper_model(cfg), c["language"], tell,
                                              c.get("whisper_device", "cpu"))
            log.info("Spoken language: %s, %d lines", spoken, len(segments))
        except Cancelled:
            raise
        except Exception as e:
            log.warning("Transcription failed (%s). Continuing on audio/chat signals only, "
                        "without captions.", str(e)[:200])
            tell("warning", None, f"no_captions: {str(e)[:200]}")

    clips = []
    tell("pick")
    if segments:
        marks = signal_marks(segments, levels, chat, heat)
        try:
            clips = pick_with_ai(segments, marks, meta, info["duration"], c, cfg)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            log.warning("AI moment selection unavailable (%s); "
                        "falling back to loudness/chat peaks", str(e)[:160])
            tell("warning", None, f"no_ai_pick: {str(e)[:200]}")
    if not clips:
        if not len(curve):
            raise RuntimeError("No audio, transcript or chat to find moments with")
        clips = pick_by_signals(curve, meta, c)
    strong = [k for k in clips if k["score"] >= c["min_score"]]
    if len(strong) < len(clips):
        log.info("%d strong moment(s) of %d (score >= %d)", len(strong), len(clips),
                 c["min_score"])
    # A live chunk gives nothing when nothing exciting happened in it. A whole video
    # still gives its best moment, so pasting a link never ends empty-handed.
    clips = strong or ([] if c["strict"] else sorted(clips, key=lambda k: -k["score"])[:1])
    clips = fit_clips(clips, segments, info["duration"], c)
    if not clips:
        log.info("No strong moment in %s%s", meta["title"], part)
        return []
    if not info["acodec"]:
        log.warning("The source has no audio track; clips will be silent")

    # From here three things overlap: each clip's words are written down carefully and
    # translated, the clips' video sections download one after another, and each clip
    # renders as soon as its section is in.
    import queue as _queue

    arrived = _queue.Queue()

    def download_sections():
        for n, clip in enumerate(clips, 1):
            try:
                arrived.put((n, fetch_section(meta, clip, work, n, tell), None))
            except Exception as e:  # handed to the renderer, which stops the job
                arrived.put((n, None, e))
                return

    if remote:
        log.info("Downloading %d video section(s) instead of the whole video", len(clips))
        _threading.Thread(target=download_sections, daemon=True).start()

    clip_words = [[x for x in segments if x["end"] > k["start"] and x["start"] < k["end"]]
                  for k in clips]
    clip_caps = [list(w) for w in clip_words]
    if c["captions"] and segments and wav is not None:
        model_name = caption_model(cfg, spoken)
        hint = meta["title"] + "."
        tell("transcribe_clips")
        try:
            audio = read_wav(wav)
            with _heavy(tell, "transcribe_clips"):
                for i, k in enumerate(clips):
                    tell("transcribe_clips", i / len(clips))
                    try:
                        better = transcribe_clip(audio, k["start"], k["end"], model_name,
                                                 spoken, c.get("whisper_device", "cpu"), hint)
                    except Cancelled:
                        raise
                    except Exception as e:
                        if model_name != ENGLISH_CAPTION_MODEL:
                            raise
                        # the English model could not be loaded: use the general one
                        log.warning("English speech model unavailable (%s)", str(e)[:120])
                        model_name = CAPTION_MODELS.get(cfg.get("speed_mode"), "small")
                        better = transcribe_clip(audio, k["start"], k["end"], model_name,
                                                 spoken, c.get("whisper_device", "cpu"), hint)
                    if better:
                        clip_words[i] = better
            del audio
            log.info("Clip speech re-transcribed with '%s'", model_name)
        except Cancelled:
            raise
        except Exception as e:  # the first pass is still there to use
            log.warning("Careful transcription unavailable (%s); using the first pass",
                        str(e)[:160])
        clip_caps = [list(w) for w in clip_words]
        target = caption_target(spoken, c["captions_language"])
        if target != spoken:
            tell("translate")
            context = meta["title"] + (f" by {meta['channel']}" if meta.get("channel") else "")
            used = set()
            for i, k in enumerate(clips):
                tell("translate", i / len(clips))
                units = subtitle_units(clip_words[i], k["start"], k["end"])
                done, who = translate_units(units, target, cfg, context)
                clip_caps[i] = done
                used.add(who)
            log.info("Captions translated %s -> %s by %s", spoken, target,
                     ", ".join(sorted(x for x in used if x)) or "-")
            if "none" in used:
                tell("warning", None, "no_translation: no translation service answered")
            elif "basic" in used:
                tell("warning", None, "basic_translation: no AI service answered")
    if wav is not None:
        wav.unlink(missing_ok=True)

    slug = re.sub(r"[^\w]+", "-", meta["title"], flags=re.UNICODE).strip("-")[:40] or "clip"
    stamp = work.name[-6:] + part.replace(" #", "-p")
    inbox = config.ROOT / "inbox"
    enc = video.encoder()[0]
    finished = []
    for n, clip in enumerate(clips, 1):
        out = out_dir / f"{slug}-{stamp}-{n:02d}.mp4"
        tell("render", (n - 1) / len(clips), f"{n}/{len(clips)}")
        began = time.time()
        if remote:  # render from this clip's own section file, once it has arrived
            got, section, error = arrived.get()
            if error:
                raise error
            file, offset = section
            c_src, c_info = file, video.probe(file)
            c_clip = {**clip, "start": clip["start"] - offset, "end": clip["end"] - offset}
            c_caps = _shift(clip_caps[n - 1], offset)
            if n == 1 and min(c_info["width"], c_info["height"]) < 1000:
                tell("warning", None, f"low_source: {c_info['width']}x{c_info['height']}")
        else:
            c_src, c_info, c_clip, c_caps = src, info, clip, clip_caps[n - 1]
        with _heavy(tell, "render"):
            layout = render(c_src, c_info, c_clip, c_caps, out, c, cfg["orientation"],
                            cfg["fps"])
        # YouTube can take the other orientation of a landscape moment: a vertical
        # render uploads as a Short, a landscape one as a regular video.
        alt = None
        wide_source = c_info["width"] > c_info["height"] * 1.1
        main_is_wide = wide_source and cfg["orientation"] == "original"
        mode = cfg["youtube_mode"]
        if wide_source and "youtube" in cfg["platforms"] and (
                mode == "both" or (mode == "short" and main_is_wide)):
            alt = out.with_suffix(".alt.mp4")
            with _heavy(tell, "render"):
                render(c_src, c_info, c_clip, c_caps, alt, c,
                       "vertical" if main_is_wide else "original", cfg["fps"])
        log.info("Clip %d/%d: %.0fs-%.0fs (%.0fs, score %d, %s) %s [%s, %.0fs]", n,
                 len(clips), clip["start"], clip["end"], clip["end"] - clip["start"],
                 clip["score"], layout, clip["title"], enc, time.time() - began)
        note = _note(clip, meta, clip_words[n - 1], spoken)
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
    if remote:
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


def run_live(info, cfg, work, tell, stop, queue=True, cancel=None):
    """Record a live stream in chunks and make clips from each chunk while the
    stream goes on, until it ends or `stop` is set."""
    import threading

    import yt_dlp

    stop = stop or threading.Event()
    cancel = cancel or threading.Event()
    _ctx.cancel = cancel
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

    def end_recording():
        # Stop recording the moment the user asks, even while a chunk is being clipped.
        stop.wait()
        if recorder.poll() is None:
            try:  # "q" makes ffmpeg close the current chunk properly
                recorder.stdin.write(b"q")
                recorder.stdin.flush()
                recorder.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                recorder.kill()

    threading.Thread(target=end_recording, daemon=True).start()

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
        except Cancelled:
            raise
        except Exception as e:
            if path.exists() and video.probe(path)["duration"] < cfg["clipper"]["min_seconds"]:
                log.info("Live chunk %d is too short for a clip", index + 1)
            else:
                log.error("Live chunk %d: %s", index + 1, e)
        path.unlink(missing_ok=True)

    while True:
        if cancel.is_set():
            stop.set()  # also ends the recorder
            recorder.kill()
            raise Cancelled("cancelled")
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
            tell("live", None, f"stopping · {len(finished)} clips")
        time.sleep(1 if stop.is_set() else 5)
    errlog.close()
    if recorder.returncode not in (0, 255, None) and not finished and done_chunks == 0:
        err = (work / "recorder.log").read_text(errors="replace")[-300:]
        raise RuntimeError(f"Recording the stream failed: {err}")
    stop.set()
    log.info("Live stream done: %d clip(s)", len(finished))
    return finished
