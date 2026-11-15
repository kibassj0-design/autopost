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


def post_language(cfg, note):
    """Language of the post text. With 'swap' it is the opposite of what is spoken in
    the clip (English speech -> Arabic, Arabic -> English, anything else -> Arabic);
    with 'original' the spoken one. Videos with no known speech use `language`."""
    setting = cfg.get("post_language", "swap")
    if setting not in ("swap", "original"):
        return setting
    m = re.search(r"^Spoken language: (\w+)", note or "", re.M)
    if not m:
        return cfg["language"]
    spoken = m.group(1)
    if setting == "original":
        return spoken
    return "en" if spoken == "ar" else "ar"


LANG_NAMES = {"ar": "Modern Standard Arabic (الفصحى)", "en": "standard English"}


def generate(path, cfg, note="", boost_of=None, name=None):
    name = name or Path(path).name
    meta = video.probe(path)
    landscape = meta["width"] > meta["height"]
    lang = post_language(cfg, note)
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
