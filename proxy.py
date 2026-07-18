#!/usr/bin/env python3
"""proxy.py — a local OpenAI-compatible proxy that auto-picks the best free
OpenRouter model.

Point any program's base_url at this server (default http://127.0.0.1:8787/v1)
and keep using its OWN OpenRouter key. The proxy:

  * forwards the client's Authorization header to OpenRouter untouched
    (pass-through — the proxy stores no key of its own),
  * resolves model:"auto" (and aliases/flags) to the currently best free model
    via ranker.py, cascading to the next-best on 429/5xx,
  * passes a concrete model id through unchanged (free or paid, your key),
  * exposes GET /models + /v1/models (no key needed) to browse the ranked free
    models, and GET /status for health.

Dependency: requests. Server: stdlib ThreadingHTTPServer. Localhost by default.
Secrets (the client's key) are forwarded but NEVER logged.
"""

from __future__ import annotations

import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

import ranker as ranker_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | proxy | %(levelname)s | %(message)s",
)
log = logging.getLogger("proxy")


DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8787,
    "ttl_seconds": 3600,
    "cascade_depth": 5,
    "request_timeout": 120,
    "defaults": {"require_tools": False, "require_private": False},
    "locked": [],
    "denylist": {"models": [], "providers": []},
    "aliases": {"smart": "auto:private", "fast": "auto"},
    "last_resort_model": None,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        cfg.update(user)
        # shallow-merge nested dicts so partial overrides keep defaults
        for key in ("defaults", "denylist"):
            merged = dict(DEFAULT_CONFIG[key])
            merged.update(user.get(key, {}) or {})
            cfg[key] = merged
    except (OSError, ValueError):
        pass
    return cfg


CONFIG = load_config()
RANKER = ranker_mod.Ranker(
    ttl_seconds=CONFIG["ttl_seconds"],
    denylist=CONFIG["denylist"],
    logger=log,
)


# ---------------------------------------------------------------------------
# Model spec resolution
# ---------------------------------------------------------------------------

def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def resolve_policy(model_field: str, body: dict, headers) -> dict:
    """Turn the request's model string + flags + headers into a routing plan.

    Returns a dict:
      {"mode": "auto"|"concrete", "model": <slug or None>,
       "require_tools": bool, "require_private": bool,
       "fallback_auto": bool}   # concrete models: opt-in cascade to auto

    Precedence: request override > config default, except any policy named in
    config["locked"] which the request cannot loosen.
    """
    raw = (model_field or "auto").strip()

    # Alias expansion (e.g. "smart" -> "auto:private").
    alias = CONFIG.get("aliases", {}).get(raw)
    if alias:
        raw = alias

    d = CONFIG["defaults"]
    locked = set(CONFIG.get("locked", []))
    require_tools = bool(d.get("require_tools"))
    require_private = bool(d.get("require_private"))

    # A concrete slug (has "/" and isn't the auto sentinel) passes through.
    # A trailing ",auto" opts a concrete model into cascade-to-auto on failure.
    head = raw.split(",")[0].strip()
    if "/" in head and not head.startswith("auto"):
        parts = [p.strip() for p in raw.split(",")]
        fallback_auto = "auto" in parts[1:]
        return {
            "mode": "concrete",
            "model": head,
            "require_tools": require_tools,
            "require_private": require_private,
            "fallback_auto": fallback_auto,
        }

    # auto[:flag[,flag]]
    flags = set()
    if head.startswith("auto"):
        _, _, flagstr = head.partition(":")
        flags = {f.strip() for f in flagstr.split(",") if f.strip()}

    # Body signal: a tools array implies the caller needs tool support.
    if isinstance(body.get("tools"), list) and body["tools"]:
        flags.add("tools")

    # Header mirrors (for SDKs that can only set the model string).
    if _truthy(headers.get("X-Proxy-Require-Tools")):
        flags.add("tools")
    if str(headers.get("X-Proxy-Privacy", "")).strip().lower() == "private":
        flags.add("private")

    # Apply request overrides in EITHER direction, unless the policy is locked
    # (a locked policy keeps the config default and ignores any request flag).
    #   tighten: "tools", "private"      loosen: "notools", "logs"/"anyprivacy"
    if "require_tools" not in locked:
        if "tools" in flags:
            require_tools = True
        elif "notools" in flags:
            require_tools = False
    if "require_private" not in locked:
        if "private" in flags:
            require_private = True
        elif "logs" in flags or "anyprivacy" in flags:
            require_private = False

    return {
        "mode": "auto",
        "model": None,
        "require_tools": require_tools,
        "require_private": require_private,
        "fallback_auto": False,
    }


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_cascade(policy: dict, force: bool = False) -> list[str]:
    """Ordered list of real model ids to try for this request."""
    if policy["mode"] == "concrete":
        chain = [policy["model"]]
        if policy["fallback_auto"]:
            chain += RANKER.pick(policy["require_tools"], policy["require_private"], force=force)
        return _dedupe(chain)
    chain = RANKER.pick(policy["require_tools"], policy["require_private"], force=force)
    chain = chain[: CONFIG["cascade_depth"]]
    # No free model qualifies → 503 by default, unless a last-resort model is
    # configured (opt-in; may be paid, so it's off unless explicitly set).
    if not chain and CONFIG.get("last_resort_model"):
        chain = [CONFIG["last_resort_model"]]
    return chain


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openrouter-free-model-proxy/1.0"

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    # -- helpers -----------------------------------------------------------
    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        require_tools = _truthy(qs.get("tools", ["0"])[0])
        force = _truthy(qs.get("refresh", ["0"])[0])

        if path == "/status":
            self._send_json(200, RANKER.status())
            return
        if path in ("/models", "/v1/models"):
            try:
                rows = RANKER.list_all(require_tools=require_tools, force=force)
            except Exception as e:
                self._send_json(503, {"error": f"could not fetch models: {e}"})
                return
            if path == "/v1/models":
                data = [{"id": r["id"], "object": "model"} for r in rows if r["selectable"]]
                self._send_json(200, {"object": "list", "data": data})
            else:
                self._send_json(200, {"object": "list", "models": rows})
            return
        self._send_json(404, {"error": "not found"})

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        if urlparse(self.path).path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return

        auth = self.headers.get("Authorization")
        if not auth:
            self._send_json(401, {"error": "missing Authorization header (send your OpenRouter key)"})
            return

        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        policy = resolve_policy(body.get("model", "auto"), body, self.headers)
        try:
            cascade = build_cascade(policy)
        except Exception as e:
            self._send_json(503, {"error": f"could not resolve model: {e}"})
            return
        if not cascade:
            self._send_json(
                503,
                {"error": "no qualifying free model right now (all down / filtered / denylisted)"},
            )
            return

        stream = bool(body.get("stream"))
        started = time.monotonic()
        errors: dict[str, str] = {}
        for idx, model_name in enumerate(cascade):
            payload = dict(body)
            payload["model"] = model_name
            fwd_headers = {"Authorization": auth, "Content-Type": "application/json"}
            try:
                if stream:
                    if self._stream_one(model_name, payload, fwd_headers, idx > 0, errors):
                        self._log_done(model_name, idx, started, "stream")
                        return
                else:
                    if self._complete_one(model_name, payload, fwd_headers, idx > 0, errors):
                        self._log_done(model_name, idx, started, "json")
                        return
            except requests.RequestException as e:
                errors[model_name] = str(e)

        log.warning("all %d candidate model(s) failed: %s", len(cascade), list(errors))
        self._send_json(502, {"error": "all candidate models failed", "details": errors})

    def _complete_one(self, model_name, payload, headers, is_fallback, errors) -> bool:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=CONFIG["request_timeout"])
        if resp.status_code == 200:
            extra = {"X-Proxy-Model": model_name}
            if is_fallback:
                extra["X-Proxy-Fallback"] = "true"
            self._send_json(200, resp.json(), extra)
            return True
        errors[model_name] = f"HTTP {resp.status_code}: {resp.text[:200]}"
        return False

    def _stream_one(self, model_name, payload, headers, is_fallback, errors) -> bool:
        # Settle the upstream status BEFORE emitting any bytes, so a failed
        # top pick can still cascade to the next model.
        resp = requests.post(
            OPENROUTER_URL, headers=headers, json=payload,
            timeout=CONFIG["request_timeout"], stream=True,
        )
        if resp.status_code != 200:
            errors[model_name] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            resp.close()
            return False
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Proxy-Model", model_name)
        if is_fallback:
            self.send_header("X-Proxy-Fallback", "true")
        self.end_headers()
        for chunk in resp.iter_content(chunk_size=None):
            if chunk:
                self.wfile.write(chunk)
                self.wfile.flush()
        return True

    def _log_done(self, model_name, idx, started, kind):
        ms = int((time.monotonic() - started) * 1000)
        log.info(
            "served model=%s fallback=%s attempts=%d %s %dms",
            model_name, idx > 0, idx + 1, kind, ms,
        )


def main():
    RANKER.start_background_refresh()
    server = ThreadingHTTPServer((CONFIG["host"], CONFIG["port"]), Handler)
    log.info(
        "openrouter-free-model-proxy on http://%s:%s  (base_url .../v1)",
        CONFIG["host"], CONFIG["port"],
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
