#!/usr/bin/env python3
"""The one place a model is called.

Two backends, one interface:

* **ollama** — ``/api/generate`` with ``format: json``. The default, because
  it is what a homelab with a GPU already runs.
* **openai** — ``/v1/chat/completions`` with ``response_format:
  {"type": "json_object"}``. Covers LM Studio, llama.cpp's server, vLLM,
  OpenRouter and OpenAI itself, which is every other way people run a model.

``keep_alive`` matters more than it looks: cold-loading a 14B model measured
109 seconds against 7 seconds of actual generation. Without it, a 22-user run
pays that load cost 22 times.
"""

import json
import re

from .. import http
from ..log import get

log = get("llm")


class LLMError(Exception):
    pass


# Where a self-hoster's model almost always is. Probed in this order so the
# person never has to know what an "endpoint" is: same box, the Docker host,
# then whatever machine Plex is on — which in a homelab is usually the one
# with the GPU.
PROBE_PORTS = (
    ("ollama", 11434, "/api/tags"),
    ("openai", 1234, "/v1/models"),      # LM Studio
    ("openai", 8080, "/v1/models"),      # llama.cpp server
    ("openai", 5000, "/v1/models"),      # text-generation-webui
    ("openai", 8000, "/v1/models"),      # vLLM
)

# Substrings that mark a model as a poor fit for this job. Nothing here is
# broken — they are just the wrong tool, and silently picking one produces
# bad rows that look like a Playlisterr bug.
POOR_FITS = {
    "coder": "tuned for code, not for taste",
    "embed": "an embedding model, it cannot write reasons",
    "vision": "a vision model; the extra weights buy nothing here",
    "guard": "a safety classifier, not a chat model",
    "-base": "a base model — it will not follow the JSON format",
}

# Rough preference among things people actually have pulled. Instruct-tuned
# mid-size models follow the schema most reliably.
GOOD_SIGNS = ("instruct", "qwen", "llama3", "mistral", "gemma", "phi")


def rate_model(name):
    """Return (verdict, note) for a model id: good / usable / poor."""
    lower = name.lower()
    for marker, why in POOR_FITS.items():
        if marker in lower:
            return "poor", why
    if any(sign in lower for sign in GOOD_SIGNS):
        if "instruct" in lower or "-it" in lower:
            return "good", "instruct-tuned; follows the JSON format reliably"
        return "usable", "should work; instruct-tuned variants do better"
    return "usable", "unknown model — try it and see"


def param_size(name):
    """Billions of parameters, from the model id. 0 when it doesn't say."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?:[-_.:]|$)", name)
    return float(match.group(1)) if match else 0.0


# Bigger is not better here. A 70B at q4 is ~42GB and will not fit a 24GB
# card, so it spills to CPU and turns a 30-second job into a 10-minute one —
# for a task a 14B already does correctly. Prefer the largest model that
# plausibly fits a consumer GPU.
COMFORTABLE_MAX_B = 34.0


def recommend(names):
    """Pick the model a first-time user should start with."""
    rated = [(n, *rate_model(n)) for n in names]

    def rank(entry):
        size = param_size(entry[0])
        # Unknown size sorts as mid-range rather than as zero: plenty of
        # good models don't put their parameter count in the id.
        effective = size or 8.0
        return (effective <= COMFORTABLE_MAX_B, effective)

    for verdict in ("good", "usable"):
        pool = [r for r in rated if r[1] == verdict]
        if pool:
            fits = [r for r in pool if param_size(r[0]) <= COMFORTABLE_MAX_B
                    or not param_size(r[0])]
            return max(fits or pool, key=rank)[0]
    return names[0] if names else ""


def probe(hosts, timeout=2):
    """Look for a model server on the usual hosts and ports.

    Fast and quiet: a couple of seconds per candidate, failures ignored. The
    goal is to answer "is there anything already running?" without asking
    someone to know their own network.
    """
    found, seen = [], set()
    for host in hosts:
        for provider, port, path in PROBE_PORTS:
            url = f"http://{host}:{port}"
            try:
                data = http.get_json(http.url_join(url, path),
                                     timeout=timeout, retries=0)
            except Exception:
                continue
            if provider == "ollama":
                names = [m.get("name", "") for m in data.get("models", [])]
            else:
                names = [m.get("id", "") for m in data.get("data", [])]
            names = sorted(n for n in names if n)
            if not names:
                continue
            # localhost and host.docker.internal are usually the same daemon.
            # Offering it twice makes the user choose between identical
            # options, which is a worse experience than not finding it.
            fingerprint = (provider, tuple(names))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            found.append({"provider": provider, "url": url, "models": names,
                          "recommended": recommend(names),
                          "ratings": {n: rate_model(n)[0] for n in names}})
    return found


class LLM:
    def __init__(self, provider="ollama", url="", model="", api_key="",
                 temperature=0.4, keep_alive="10m", timeout=600):
        self.provider = provider
        self.url = (url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.keep_alive = keep_alive
        self.timeout = timeout
        http.register_secret(api_key)

    # -- introspection ---------------------------------------------------
    @property
    def openai_style(self):
        """Everything that is not Ollama speaks the OpenAI wire format."""
        return self.provider != "ollama"

    def models(self):
        """Model ids the endpoint offers, for the Settings dropdown."""
        try:
            if self.provider == "ollama":
                data = http.get_json(http.url_join(self.url, "/api/tags"),
                                     timeout=30)
                return sorted(m.get("name", "") for m in data.get("models", []))
            data = http.get_json(http.url_join(self.url, "/v1/models"),
                                 headers=self._auth(), timeout=30)
            return sorted(m.get("id", "") for m in data.get("data", []))
        except http.HttpError as exc:
            raise LLMError(str(exc)) from exc

    def ping(self):
        names = self.models()
        return {"models": len(names),
                "has_configured_model": self.model in names,
                "sample": names[:5]}

    def _auth(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key \
            else {}

    # -- generation ------------------------------------------------------
    def complete_json(self, system, prompt, max_tokens=1500, schema=None):
        """Ask for JSON and return the parsed object plus timing stats.

        When a ``schema`` is given it is enforced by the server rather than
        requested in the prompt. This matters: a 14B model asked nicely for
        ``{"picks": [...]}`` will cheerfully return
        ``{"selectedFilmsAndTVShows": [{"id": 96}]}`` — ids with no reasons,
        under a key it invented. Constrained decoding makes that impossible.
        Ollama has supported it since 0.5; OpenAI-compatible servers vary, so
        a rejected schema falls back to plain json mode.

        Raises LLMError rather than returning junk: the caller has a
        rule-based fallback and needs a clean signal to switch to it.
        """
        if not self.openai_style:
            payload = {
                "model": self.model,
                "system": system,
                "prompt": prompt,
                "stream": False,
                "format": schema or "json",
                "keep_alive": self.keep_alive,
                "options": {"temperature": self.temperature,
                            "num_predict": max_tokens},
            }
            data = http.post_json(
                http.url_join(self.url, "/api/generate"), payload,
                timeout=self.timeout, retries=0)
            text = data.get("response", "")
            stats = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "load_s": round(data.get("load_duration", 0) / 1e9, 1),
                "total_s": round(data.get("total_duration", 0) / 1e9, 1),
            }
        else:
            payload = {
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": prompt}],
                "temperature": self.temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            if schema:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "playlisterr_picks", "strict": True,
                                    "schema": schema}}
            endpoint = http.url_join(self.url, "/v1/chat/completions")
            try:
                data = http.post_json(endpoint, payload, headers=self._auth(),
                                      timeout=self.timeout, retries=0)
            except http.HttpError as exc:
                if not schema or exc.status not in (400, 422):
                    raise
                # This server does not do json_schema. Ask for plain JSON.
                log.debug("endpoint rejected json_schema, retrying as "
                          "json_object")
                payload["response_format"] = {"type": "json_object"}
                data = http.post_json(endpoint, payload, headers=self._auth(),
                                      timeout=self.timeout, retries=0)
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError) as exc:
                raise LLMError(f"unexpected response shape: {exc}") from exc
            usage = data.get("usage", {})
            stats = {"prompt_tokens": usage.get("prompt_tokens", 0),
                     "output_tokens": usage.get("completion_tokens", 0),
                     "load_s": 0, "total_s": 0}

        log.debug("model returned %d chars: %s", len(text or ""),
                  (text or "")[:400].replace("\n", " "))
        return _parse_json(text), stats


def _parse_json(text):
    """Models sometimes wrap JSON in prose or a fenced block. Cope, once."""
    text = (text or "").strip()
    if not text:
        raise LLMError("model returned an empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "```" in text:
        chunk = text.split("```")[1]
        chunk = chunk.split("\n", 1)[1] if chunk.startswith("json") else chunk
        try:
            return json.loads(chunk.strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError(f"model did not return JSON (got {text[:120]!r})")
