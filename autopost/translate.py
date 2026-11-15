"""Keyless machine translation, the last resort when no AI service can answer, so
the language swap (English speech -> Arabic text, Arabic -> English) still happens."""
import logging

import requests

log = logging.getLogger("autopost.translate")

URL = "https://translate.googleapis.com/translate_a/single"
CHUNK = 1800  # characters per request


def _one(text, target):
    r = requests.post(URL, timeout=30,
                      params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t"},
                      data={"q": text})
    if r.status_code >= 400:
        raise RuntimeError(f"translation service: HTTP {r.status_code}")
    return "".join(part[0] for part in r.json()[0] if part and part[0])


def lines(texts, target):
    """Translate a list of lines, keeping one output line per input line."""
    out, batch, size = [], [], 0

    def flush():
        if not batch:
            return
        got = _one("\n".join(batch), target).split("\n")
        if len(got) != len(batch):  # line breaks got merged: do these one by one
            got = [_one(t, target) for t in batch]
        out.extend(g.strip() for g in got)
        batch.clear()

    for t in texts:
        t = " ".join(t.split()) or "."
        if size + len(t) > CHUNK:
            flush()
            size = 0
        batch.append(t)
        size += len(t) + 1
    flush()
    return out
