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
# Google's own aliases for its newest Flash models, used when the model list can't be read.
GEMINI_ALIASES = ["gemini-flash-latest", "gemini-flash-lite-latest"]


class LLMError(RuntimeError):
    pass


# Free OpenAI-compatible services: name -> (key variable, API base, model preferences).
# Preferences are tried in order against the models the key can actually use.
COMPAT = {
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1",
                [r"^mistral-large-latest$", r"^mistral-medium-latest$",
                 r"^mistral-small-latest$", r"mistral-(large|medium|small)"]),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", [r":free$"]),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1",
             [r"llama-3\.3-70b", r"llama.*70b", r"gpt-oss-120b", r"qwen", r"llama"]),
    "cerebras": ("CEREBRAS_API_KEY", "https://api.cerebras.ai/v1",
                 [r"llama-?3\.3-70b", r"llama.*70b", r"gpt-oss-120b", r"qwen", r"llama"]),
}
ORDER = ["groq", "gemini", "claude", "mistral", "openrouter", "cerebras"]
_dead = set()  # providers that failed in a way that won't fix itself during this run


def has_key(name):
    if name == "claude":
        # A value that isn't shaped like an Anthropic key (e.g. another service's key
        # pasted into the wrong field) must not switch the tool over to Claude.
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-")
                    or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if name == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    return bool(os.environ.get(COMPAT[name][0]))


def providers(cfg):
    """The services to try, in order. `ai_provider` set to a name puts that one first."""
    names = [n for n in ORDER if has_key(n)]
    choice = cfg.get("ai_provider", "groq")
    if choice in names:
        names.remove(choice)
        names.insert(0, choice)
    # a service whose key was refused goes last, so the next one is used at once
    return [n for n in names if n not in _dead] + [n for n in names if n in _dead]


def provider(cfg):
    """The service that will be asked first, or None when no key is set."""
    names = providers(cfg)
    return names[0] if names else None


def ask_json(system, text, schema, cfg, images=(), effort="medium", max_tokens=32000):
    """Returns (parsed object, provider name). Each configured service is tried in turn,
    so one running out of free quota just hands over to the next."""
    names = providers(cfg)
    if not names:
        raise LLMError("no AI key is set")
    problems = []
    for who in names:
        if who in _dead:
            continue
        try:
            if who == "claude":
                data = _claude(system, text, schema, cfg, images, effort, max_tokens)
            elif who == "gemini":
                data = _gemini(system, text, schema, cfg, images, effort, max_tokens)
            else:
                data = _compat(who, system, text, schema, cfg)
            _validate(data, schema, "answer")
            return data, who
        except LLMError as e:
            problems.append(f"{who}: {e}")
            if who != names[-1]:
                log.warning("%s could not answer (%s); trying the next AI service",
                            who, str(e)[:160])
    raise LLMError(" || ".join(problems)[:600] or "every AI service is unavailable")


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
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            if "API key not valid" in r.text or "API_KEY_INVALID" in r.text:
                _dead.add("gemini")  # use the other AI keys meanwhile
                shape = ("" if key.startswith(("AIza", "AQ.")) else
                         f" ملاحظة: المحفوظ الآن يبدأ بغير AIza أو AQ. ، ومفاتيح Gemini تبدأ "
                         "بأحدهما، فالأغلب أن شيئا آخر لُصق في هذه الخانة.")
                raise LLMError(
                    "مفتاح Gemini غير صالح: Google لا تعرفه (ربما حُذف، أو نُسخ ناقصا، أو "
                    "لُصق مفتاح آخر مكانه). الحل: aistudio.google.com/apikey ← Create API key "
                    "← انسخه كاملا والصقه في خانة Gemini ← حفظ." + shape)
            if "SERVICE_DISABLED" in r.text or "has not been used" in r.text:
                _dead.add("gemini")
                raise LLMError(
                    "هذا مفتاح Google Cloud لم تُفعَّل فيه خدمة Gemini. الأسهل: أنشئ مفتاحا من "
                    "aistudio.google.com/apikey والصقه بدلا منه.")
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
                # Wait only as long as Gemini asks (usually seconds), at most 30s.
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', r.text)
                delay = min(float(m.group(1)) if m else 20.0, 30.0)
                log.info("Gemini %s per-minute limit reached, waiting %.0fs", model, delay)
                time.sleep(delay)
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


# ---------------------------------------------------------------- OpenAI-compatible

_compat_models = {}
_compat_spent = set()  # (provider, model) pairs whose quota ran out during this run


def compat_models(name, cfg):
    """Models to try for this service, best first."""
    key_var, base, prefs = COMPAT[name]
    chosen = (cfg.get("ai_models") or {}).get(name)
    if name not in _compat_models:
        try:
            r = requests.get(f"{base}/models", timeout=30,
                             headers={"Authorization": f"Bearer {os.environ[key_var]}"})
        except requests.RequestException as e:
            raise LLMError(f"network error {e}")
        if r.status_code in (401, 403):
            _dead.add(name)
            raise LLMError(f"key rejected (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise LLMError(f"HTTP {r.status_code} {r.text[:160]}")
        body = r.json()
        items = body.get("data", body) if isinstance(body, dict) else body
        skip = ("guard", "whisper", "tts", "embed", "moderation", "ocr", "vision-preview",
                "image", "audio", "transcribe", "rerank")
        models = [m for m in items if isinstance(m, dict) and m.get("id")
                  and not any(w in m["id"].lower() for w in skip)]
        # A model that can read more at once copes with longer transcripts.
        models.sort(key=lambda m: -(m.get("context_length") or m.get("context_window")
                                    or m.get("max_context_length") or 0))
        ranked = []
        for pattern in prefs:
            for m in models:
                if re.search(pattern, m["id"], re.I) and m["id"] not in ranked:
                    ranked.append(m["id"])
        if not ranked:
            raise LLMError("no suitable model is available for this key")
        _compat_models[name] = ranked[:4]
    return ([chosen] if chosen else []) + [m for m in _compat_models[name] if m != chosen]


def _compat(name, system, text, schema, cfg):
    key_var, base, _ = COMPAT[name]
    instructions = (
        f"{system}\n\nReply with one JSON object and nothing else: no explanation and no "
        f"code fences. It must match this JSON Schema exactly:\n{json.dumps(schema)}")
    headers = {"Authorization": f"Bearer {os.environ[key_var]}"}
    problems = []
    for model in compat_models(name, cfg):
        if (name, model) in _compat_spent:
            continue
        body = {"model": model, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": instructions},
                             {"role": "user", "content": text}]}
        for attempt in range(2):
            try:
                r = requests.post(f"{base}/chat/completions", json=body, headers=headers,
                                  timeout=600)
            except requests.RequestException as e:
                problems.append(f"{model}: network error {e}")
                break
            if r.status_code == 400 and "response_format" in body and attempt == 0 \
                    and "response_format" in r.text:
                body.pop("response_format")  # a model without JSON mode still follows the prompt
                continue
            if r.status_code in (401, 403):
                _dead.add(name)
                raise LLMError(f"key rejected (HTTP {r.status_code})")
            if r.status_code == 429 and attempt == 0 and not re.search(
                    r"per day|TPD|RPD|daily", r.text, re.I):
                # A per-minute limit (Groq's free plan has small ones): wait as long as
                # the service asks, then ask again, instead of dropping the model.
                m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", r.text)
                delay = (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else \
                    float(r.headers.get("retry-after") or 20)
                if delay <= 65:
                    log.info("%s %s per-minute limit reached, waiting %.0fs", name, model, delay)
                    time.sleep(delay + 0.5)
                    continue
            if r.status_code >= 400:  # quota used up, input too long for this model, retired...
                if r.status_code == 429:
                    _compat_spent.add((name, model))
                problems.append(f"{model}: HTTP {r.status_code} {r.text[:120]}")
                break
            try:
                return _extract_json(r.json()["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError, ValueError) as e:
                problems.append(f"{model}: unusable answer ({type(e).__name__})")
                break
    raise LLMError(" | ".join(problems)[:400] or "the free quota of every model is used up")
