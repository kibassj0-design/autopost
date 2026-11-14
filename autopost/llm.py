"""One door to the AI model: Claude when a key is set, otherwise Gemini (free tier).

Every caller wants the same thing: instructions + text (+ images) in, a JSON object
matching a schema out.
"""
import json
import logging
import os
import re
import time

import anthropic
import requests

log = logging.getLogger("autopost.ai")

CLAUDE_MODEL = "claude-opus-5-5"
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"


class LLMError(RuntimeError):
    pass


def provider(cfg):
    """'claude', 'gemini' or None, from the ai_provider setting and which keys exist."""
    # A value that isn't shaped like an Anthropic key (e.g. a Gemini key pasted into
    # the wrong field) must not switch the tool over to Claude.
    has = {
        "claude": bool(os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-")
                       or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
    }
    choice = cfg.get("ai_provider", "auto")
    if choice in has:
        return choice if has[choice] else None
    return "claude" if has["claude"] else "gemini" if has["gemini"] else None


def ask_json(system, text, schema, cfg, images=(), effort="medium", max_tokens=32000):
    """Returns (parsed object, provider name). Raises LLMError when no model can answer."""
    who = provider(cfg)
    if who is None:
        raise LLMError("no AI key is set (GEMINI_API_KEY or ANTHROPIC_API_KEY)")
    args = (system, text, schema, cfg, images, effort, max_tokens)
    if who == "claude":
        try:
            data = _claude(*args)
        except LLMError as e:
            # Out of credit, bad key, outage: use the free model if there is one.
            if not os.environ.get("GEMINI_API_KEY") or cfg.get("ai_provider") == "claude":
                raise
            log.warning("%s; using Gemini instead", str(e)[:160])
            who, data = "gemini", _gemini(*args)
    else:
        data = _gemini(*args)
    _validate(data, schema, "answer")
    return data, who


def _validate(value, schema, where):
    """Gemini's JSON is asked for in the prompt, not enforced, so check its shape."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise LLMError(f"{where}: expected an object")
        for key in schema.get("required", []):
            if key not in value:
                raise LLMError(f"{where}: missing '{key}'")
            _validate(value[key], schema["properties"][key], f"{where}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            raise LLMError(f"{where}: expected a list")
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{where}[{i}]")
    elif kind == "string" and not isinstance(value, str):
        raise LLMError(f"{where}: expected text")
    elif kind in ("number", "integer") and (isinstance(value, bool)
                                            or not isinstance(value, (int, float))):
        raise LLMError(f"{where}: expected a number")


# ---------------------------------------------------------------- Claude

def _claude(system, text, schema, cfg, images, effort, max_tokens):
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}
               for b64 in images]
    content.append({"type": "text", "text": text})
    try:
        # Streamed: a multi-hour transcript is a long request.
        with anthropic.Anthropic().beta.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # a safety decline is retried on another model server-side
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort,
                           "format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            response = stream.get_final_message()
    except anthropic.RateLimitError as e:
        raise LLMError(f"Claude rate limit: {e.message}")
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Claude network error: {e}")
    except (anthropic.AnthropicError, TypeError) as e:  # TypeError: no credentials
        raise LLMError(f"Claude: {e}")
    if response.stop_reason in ("refusal", "max_tokens"):
        raise LLMError(f"Claude stopped with {response.stop_reason}")
    try:
        return json.loads(next(b.text for b in response.content if b.type == "text"))
    except (StopIteration, ValueError) as e:
        raise LLMError(f"Claude returned no usable JSON ({e})")


# ---------------------------------------------------------------- Gemini

_gemini_models = None
_gemini_spent = set()  # models whose quota ran out during this run


def _version(name):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def gemini_models(cfg):
    """Text models this key can call, best first: newest Flash, then newest Flash-Lite
    (smaller, but with a much larger free daily quota). A model set in config goes first."""
    global _gemini_models
    if _gemini_models is None:
        r = requests.get(f"{GEMINI_API}/models", params={"pageSize": 1000}, timeout=30,
                         headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        if r.status_code >= 400:
            raise LLMError(f"Gemini key rejected: HTTP {r.status_code} {r.text[:200]}")
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        skip = ("image", "tts", "audio", "live", "embedding", "preview", "exp", "robotics",
                "computer", "latest")
        flash = sorted((n for n in names if "flash" in n and not any(s in n for s in skip)),
                       key=_version, reverse=True)
        full = [n for n in flash if "lite" not in n]
        lite = [n for n in flash if "lite" in n]
        _gemini_models = full[:1] + lite[:1] + full[1:2] + lite[1:2]
        if not _gemini_models:
            raise LLMError("this Gemini key has no Flash model available")
    chosen = cfg.get("gemini_model")
    return ([chosen] if chosen else []) + [m for m in _gemini_models if m != chosen]


def _extract_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _gemini(system, text, schema, cfg, images, effort, max_tokens):
    instructions = (
        f"{system}\n\nReply with one JSON object and nothing else: no explanation and no "
        f"code fences. It must match this JSON Schema exactly:\n{json.dumps(schema)}")
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": b64}} for b64 in images]
    parts.append({"text": text})
    body = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    problems = []
    for model in gemini_models(cfg):
        if model in _gemini_spent:
            continue
        url = f"{GEMINI_API}/models/{model}:generateContent"
        for attempt in range(2):
            try:
                r = requests.post(url, json=body, headers=headers, timeout=600)
            except requests.RequestException as e:
                problems.append(f"{model}: network error {e}")
                break
            if r.status_code == 400 and "generationConfig" in body:
                # An API revision that rejects the JSON hint still answers in JSON
                # because the instructions ask for it.
                body = {k: v for k, v in body.items() if k != "generationConfig"}
                continue
            if r.status_code == 429 and attempt == 0 and "PerMinute" in r.text:
                log.info("Gemini %s per-minute limit reached, waiting 60s", model)
                time.sleep(60)
                continue
            if r.status_code >= 400:  # daily quota used up, model retired, ...: next model
                if r.status_code == 429:
                    _gemini_spent.add(model)
                    log.info("Gemini %s quota is used up; trying the next model", model)
                problems.append(f"{model}: HTTP {r.status_code} {r.text[:120]}")
                break
            try:
                candidate = r.json()["candidates"][0]
                answer = "".join(p.get("text", "") for p in candidate["content"]["parts"]
                                 if not p.get("thought"))
                return _extract_json(answer)
            except (KeyError, IndexError, ValueError) as e:
                problems.append(f"{model}: unusable answer ({type(e).__name__})")
                break
    raise LLMError("Gemini could not answer: " + (" | ".join(problems)[:500] or
                                                   "the free quota of every model is used up"))
