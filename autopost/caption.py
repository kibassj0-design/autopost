"""Caption, title and hashtags per platform, written by the AI model from video stills."""
import json
import logging
import re
from pathlib import Path

from . import llm, video

log = logging.getLogger("autopost.caption")

SCHEMA = {
    "type": "object",
    "properties": {
        "youtube": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "description", "tags"],
            "additionalProperties": False,
        },
        "instagram": {
            "type": "object",
            "properties": {"caption": {"type": "string"}},
            "required": ["caption"],
            "additionalProperties": False,
        },
        "tiktok": {
            "type": "object",
            "properties": {"caption": {"type": "string"}},
            "required": ["caption"],
            "additionalProperties": False,
        },
    },
    "required": ["youtube", "instagram", "tiktok"],
    "additionalProperties": False,
}

SYSTEM = """You write the text that goes out with short vertical videos on YouTube Shorts, \
Instagram Reels and TikTok. Nobody reviews it before it is published, so every field must be \
ready to post exactly as written.

You are shown stills from the video plus whatever the creator said about it. Describe only \
what is actually there: a caption that promises something the video doesn't show costs the \
account its viewers' trust and its retention. If the stills are ambiguous, stay general.

What performs on each platform:
- YouTube: the title is what gets the click. Under 70 characters, concrete, with the main \
search phrase near the start. The description is 1-2 sentences followed by 3-5 hashtags, one \
of them #Shorts. Tags are 8-12 plain search phrases without the # sign.
- Instagram: the first line is all that shows before "more", so it carries the hook. Then a \
short body and a question or call to action that invites comments. End with 5-8 hashtags that \
mix a couple of broad ones with specific niche ones.
- TikTok: one or two punchy lines in the way people talk on the app, then 4-6 hashtags.

When the notes include what is said in the clip, build the caption from that speech: \
its key line, claim or punchline, translated into the requested language when it differs \
from the spoken one. Keep names as speakers of that language usually write them.

Pick hashtags people actually search for this subject. Generic filler tags add nothing unless \
the creator listed them as defaults. Write the three versions differently, since the same \
audience often follows all three accounts."""


def _clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _normalize(data, shorts=True):
    yt, ig, tt = data["youtube"], data["instagram"], data["tiktok"]
    title = _clip(yt["title"].replace("<", "").replace(">", "").strip(), 100)
    tags, used = [], 0
    for tag in yt["tags"]:
        tag = tag.strip().lstrip("#")
        if tag and used + len(tag) + 3 <= 480:  # YouTube caps the tag list at 500 chars
            tags.append(tag)
            used += len(tag) + 3
    description = yt["description"].strip()
    if shorts and "#shorts" not in description.lower():
        description += "\n#Shorts"
    caption = ig["caption"].strip()
    hashtags = re.findall(r"#\w+", caption)
    for extra in hashtags[30:]:  # Instagram rejects posts with more than 30
        caption = caption.replace(extra, "", 1)
    return {
        "youtube": {"title": title, "description": _clip(description, 4900), "tags": tags},
        "instagram": {"caption": _clip(caption.strip(), 2200)},
        "tiktok": {"caption": _clip(tt["caption"].strip(), 2000)},
    }


def _fallback(name, note, cfg, shorts=True):
    """Used when no AI model is available, so a post is never blocked on the caption."""
    first_line = (note or "").strip().splitlines()[0] if (note or "").strip() else ""
    base = first_line.removeprefix("Clip title: ") or re.sub(r"[_\-]+", " ", Path(name).stem).strip()
    lang = post_language(cfg, note)
    spoken = re.search(r"^Spoken language: (\w+)", note or "", re.M)
    if spoken and spoken.group(1) != lang:
        try:  # keep the language swap even without an AI model
            from . import translate
            base = translate.lines([base], lang)[0] or base
        except Exception as e:
            log.warning("Caption translation unavailable: %s", e)
    tags = " ".join(cfg["default_hashtags"])
    return _normalize({
        "youtube": {"title": base, "description": f"{base}\n{tags}",
                    "tags": [t.lstrip("#") for t in cfg["default_hashtags"]]},
        "instagram": {"caption": f"{base}\n\n{tags}"},
        "tiktok": {"caption": f"{base} {tags}"},
    }, shorts)


def fixed(text, path):
    """The creator's own caption, used as written on every platform."""
    meta = video.probe(path)
    text = text.strip()
    first = text.splitlines()[0] if text else Path(path).stem
    title = re.sub(r"#\w+", "", first).strip() or Path(path).stem
    tags = [t.lstrip("#") for t in re.findall(r"#\w+", text)]
    return _normalize({
        "youtube": {"title": title, "description": text, "tags": tags},
        "instagram": {"caption": text},
        "tiktok": {"caption": text},
    }, shorts=meta["height"] > meta["width"])


def post_language(cfg, note):
    """Language of the post text. With 'swap' it is the opposite of what is spoken in
    the clip (English speech -> Arabic, Arabic -> English, anything else -> Arabic);
    with 'original' the spoken one. Videos with no known speech use `language`."""
    setting = cfg.get("post_language", "both")
    if setting not in ("swap", "original"):
        return setting  # "both", "ar", "en", ...
    m = re.search(r"^Spoken language: (\w+)", note or "", re.M)
    if not m:
        return cfg["language"]
    spoken = m.group(1)
    if setting == "original":
        return spoken
    return "en" if spoken == "ar" else "ar"


LANG_NAMES = {"ar": "Modern Standard Arabic (الفصحى)", "en": "standard English"}

# ---------------------------------------------------------------- bilingual captions

PIECES = {"type": "object", "properties": {
    "hook_ar": {"type": "string"}, "hook_en": {"type": "string"},
    "line_ar": {"type": "string"}, "line_en": {"type": "string"},
    "question_ar": {"type": "string"}, "question_en": {"type": "string"},
    "title_ar": {"type": "string"}, "title_en": {"type": "string"},
    "hashtags_ar": {"type": "array", "items": {"type": "string"}},
    "hashtags_en": {"type": "array", "items": {"type": "string"}},
    "tags": {"type": "array", "items": {"type": "string"}},
}, "required": ["hook_ar", "hook_en", "line_ar", "line_en", "question_ar", "question_en",
                "title_ar", "title_en", "hashtags_ar", "hashtags_en", "tags"]}

PIECES_SYSTEM = """You write the post text for one short video (TikTok, Instagram Reels, YouTube \
Shorts) in two languages: Arabic and English. It is published automatically, exactly as you \
write it, so it must be flawless.

Describe only what really happens in the clip (from the notes and stills). Fields:
- hook: the first line people see. One short, gripping sentence about the moment (no \
"watch this", no clickbait lies), may end with ONE fitting emoji.
- line: one more short sentence with the key detail or the funniest/most shocking line \
said in the clip.
- question: one short question that makes people comment.
- title: a YouTube title, under 55 characters, concrete.
- hashtags: 4-5 per language that people really search for this game/streamer/moment \
(Arabic ones in Arabic letters joined with underscores; English ones in English). No spaces \
inside a hashtag, no duplicates.
- tags: 8-12 plain YouTube search phrases (no #), mixed Arabic and English.

Arabic: correct Modern Standard Arabic (الفصحى) in the natural style of a good Arabic \
social media page: simple, lively, grammatically perfect, no dialect, no literal \
translation, no English words except names (write names the way Arabic pages write them, \
e.g. ألانزوكا، جي تي إيه). Watch prepositions and agreement: "شاركنا رأيك" (not \
"شاركنا برأيك"), "ما رأيكم؟", "أخبرونا في التعليقات".
English: natural, fluent, how English creators write, correct spelling and grammar.
The two languages say the same thing, each written as a native speaker would, not \
translated word by word."""

PROOF_SYSTEM = """You are a meticulous proofreader for Arabic (Modern Standard) and English \
social media text. You get a JSON object of short texts that will be published as is. Fix \
every spelling, grammar, preposition, agreement, punctuation and word-choice mistake, and \
any awkward or literal phrasing, so each text reads as written by an educated native \
speaker. Keep the meaning, length, emojis and names. Arabic stays Modern Standard Arabic \
with no dialect and no English words except names. Hashtags: Arabic ones in Arabic letters \
joined with underscores, English ones in English, no spaces. Return the same JSON keys."""


def _hashtags(items):
    out = []
    for h in items or []:
        h = re.sub(r"\s+", "_", str(h).strip().lstrip("#")).strip("_")
        h = re.sub(r"[^\w\u0600-\u06FF]", "", h)
        if h and h.lower() not in {x.lower() for x in out}:
            out.append(h)
    return ["#" + h for h in out]


def _fix_arabic(text):
    """Mistakes AI models often make in Arabic post text, fixed without asking anyone."""
    fixes = [(r"شاركنا\s+برأيك", "شاركنا رأيك"), (r"شاركونا\s+برأيكم", "شاركونا رأيكم"),
             (r"\s+([،؟!.:])", r"\1"), (r"([،؟!])(?=[^\s\d#])", r"\1 "), (r"\?", "؟"),
             (r"\s{2,}", " ")]
    for a, b in fixes:
        text = re.sub(a, b, text)
    return text.strip()


def _assemble(d, lang, cfg, credit, shorts):
    ar = {k: _fix_arabic(d.get(k + "_ar", "")) for k in ("hook", "line", "question", "title")}
    en = {k: d.get(k + "_en", "").strip() for k in ("hook", "line", "question", "title")}
    tags_ar, tags_en = _hashtags(d.get("hashtags_ar")), _hashtags(d.get("hashtags_en"))
    defaults = _hashtags(cfg.get("default_hashtags") or [])

    def block(x):
        return "\n".join(t for t in (x["hook"], x["line"]) if t)

    parts, tags = [], []
    if lang in ("both", "ar"):
        parts.append(block(ar))
        tags += tags_ar
    if lang in ("both", "en"):
        parts.append(block(en))
        tags += tags_en
    question = " / ".join(q for q, on in ((ar["question"], lang in ("both", "ar")),
                                         (en["question"], lang in ("both", "en"))) if q and on)
    tags += [t for t in defaults if t.lower() not in {x.lower() for x in tags}]
    credit_line = ""
    if credit:
        credit_line = {"ar": f"🎥 من بث {credit}", "en": f"🎥 Stream: {credit}"}.get(
            lang, f"🎥 من بث {credit} | Stream: {credit}")
    body = "\n\n".join(p for p in parts if p)
    ig = "\n\n".join(x for x in (body, question, credit_line, " ".join(tags[:12])) if x)
    tt = "\n\n".join(x for x in (body, credit_line, " ".join(tags[:7])) if x)
    if lang == "ar":
        title = ar["title"]
    elif lang == "en":
        title = en["title"]
    else:
        title = f"{ar['title']} | {en['title']}" if ar["title"] and en["title"] else \
            ar["title"] or en["title"]
    description = "\n\n".join(x for x in (body, credit_line, " ".join(tags[:5])) if x)
    return _normalize({
        "youtube": {"title": title, "description": description,
                    "tags": [t.strip().lstrip("#") for t in d.get("tags") or []] +
                            [t.lstrip("#") for t in tags]},
        "instagram": {"caption": ig}, "tiktok": {"caption": tt},
    }, shorts)


def _generate_pieces(path, cfg, note, boost_of, name, lang, landscape, meta):
    brief = [f"File name: {name}", f"Length: {meta['duration']:.0f} seconds"]
    if cfg["niche"]:
        brief.append(f"Account niche: {cfg['niche']}")
    if cfg["brand_voice"]:
        brief.append(f"Voice: {cfg['brand_voice']}")
    if note:
        brief.append(f"Notes about this clip: {note}")
    if boost_of:
        brief.append(f"This follows an earlier post that is taking off ({boost_of!r}): write "
                     "it as a continuation with a fresh hook, not a repeat.")
    data, who = llm.ask_json(PIECES_SYSTEM, "\n".join(brief), PIECES, cfg,
                             images=video.frames_b64(path), max_tokens=8000)
    texts = {k: v for k, v in data.items() if isinstance(v, (str, list))}
    try:  # a second, careful read for mistakes
        fixed, _ = llm.ask_json(PROOF_SYSTEM, json.dumps(texts, ensure_ascii=False), PIECES,
                                cfg, max_tokens=8000)
        if all(fixed.get(k) for k in ("hook_ar", "hook_en")):
            data = fixed
    except (llm.LLMError, KeyError, TypeError, ValueError) as e:
        log.info("Proofreading skipped (%s)", str(e)[:120])
    credit = re.search(r"Clipped from (.+?)'s video", note or "")
    return _assemble(data, lang, cfg, credit.group(1) if credit else "", not landscape), who


def generate(path, cfg, note="", boost_of=None, name=None):
    name = name or Path(path).name
    meta = video.probe(path)
    landscape = meta["width"] > meta["height"]
    lang = post_language(cfg, note)
    if lang in ("both", "ar", "en"):
        try:
            return _generate_pieces(path, cfg, note, boost_of, name, lang, landscape, meta)
        except (llm.LLMError, KeyError, TypeError, ValueError) as e:
            log.warning("Caption pieces unavailable (%s)", str(e)[:160])
    brief = [
        f"Write every field in {LANG_NAMES.get(lang, lang)}, whatever language the "
        f"video or these notes are in, and only in it: no dialect and no words from "
        f"another language except names and brands.",
        f"File name: {name}",
        f"Length: {meta['duration']:.0f} seconds",
    ]
    if landscape:
        brief.append("This video is landscape (16:9). On YouTube it is a regular video, "
                     "not a Short, so leave out #Shorts.")
    if cfg["niche"]:
        brief.append(f"Account niche: {cfg['niche']}")
    if cfg["brand_voice"]:
        brief.append(f"Voice: {cfg['brand_voice']}")
    if cfg["default_hashtags"]:
        brief.append("Creator's default hashtags (use the ones that fit): "
                     + " ".join(cfg["default_hashtags"]))
    if note:
        brief.append(f"Creator's note about this video: {note}")
    if boost_of:
        brief.append(
            "This is a follow-up cut from a video that is taking off right now. The earlier "
            f"post's title was: {boost_of!r}. Write it as a continuation that someone who saw "
            "the first one wants to watch, with a fresh hook, not a repeat of that text.")

    try:
        data, who = llm.ask_json(SYSTEM, "\n".join(brief), SCHEMA, cfg,
                                 images=video.frames_b64(path), max_tokens=16000)
        return _normalize(data, shorts=not landscape), who
    except (llm.LLMError, KeyError, TypeError) as e:
        reason = str(e)
    log.warning("AI caption unavailable (%s); using the basic fallback caption", reason[:200])
    return _fallback(name, boost_of or note, cfg, shorts=not landscape), "fallback"
