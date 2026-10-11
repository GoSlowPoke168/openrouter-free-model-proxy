#!/usr/bin/env python3
"""proxy.py — a local OpenAI-compatible proxy that auto-picks the best free
OpenRouter model.

Point any program's base_url at this server (default http://127.0.0.1:8787/v1)
and keep using its OWN OpenRouter key. The proxy:

  * forwards the client's Authorization header to OpenRouter untouched
    (pass-through — the proxy stores no key of its own),
  * resolves model:"auto" (and aliases/flags) to the currently best free model
    via ranker.py, cascading to the next-best on 429/5xx/timeouts,
  * relays errors another model can't fix (bad key, no credits, bad request)
    straight back to the client,
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
import selection

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
    "ttl_seconds": 86400,       # on-demand safety net only; see daily_refresh_at
    "daily_refresh_at": "00:01",  # HH:MM UTC the ranked list is rebuilt
    "cascade_depth": 5,
    "request_timeout": 120,
    "defaults": {"require_tools": False},  # "mode" is filled in by load_config
    "locked": [],
    "denylist": {"models": [], "providers": []},
    "aliases": {"smart": "auto:strict", "fast": "auto"},
    "last_resort_model": None,
}

# Model-string flags (and X-Proxy-Privacy values) → mode. The non-mode names
# are the pre-mode flags, kept so existing clients behave as before.
MODE_FLAGS = {
    **{name: name for name in selection.MODES},
    "private": "strict",
    "logs": "balanced",
    "anyprivacy": "balanced",
    "any": "loose",
}
_STRICTNESS = list(selection.MODES)


def _parse_hhmm(s: str, default: tuple[int, int] = (0, 1)) -> tuple[int, int]:
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return default


def load_config(path: str = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except (OSError, ValueError) as e:
        # Falling back to defaults could silently drop a locked privacy policy.
        raise SystemExit(f"cannot read {path}: {e}")
    cfg.update(user)
    # shallow-merge nested dicts so partial overrides keep defaults
    for key in ("defaults", "denylist"):
        merged = dict(DEFAULT_CONFIG[key])
        merged.update(user.get(key, {}) or {})
        cfg[key] = merged

    d = cfg["defaults"]
    if "mode" not in d:  # pre-mode config: map the old booleans
        d["mode"] = (
            "strict" if d.get("require_private")
            else "loose" if d.get("allow_trains")
            else selection.DEFAULT_MODE
        )
    if d["mode"] not in selection.MODES:
        raise SystemExit(
            f"{path}: unknown defaults.mode {d['mode']!r} (choose from {', '.join(selection.MODES)})"
        )
    cfg["locked"] = {
        "mode" if k in ("require_private", "allow_trains") else k for k in cfg.get("locked") or []
    }
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

class PolicyError(ValueError):
    """The request's model string or flags can't be honoured (→ 400)."""


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _auto_policy(flagstr: str, body: dict, headers) -> dict:
    """Resolve auto flags + headers + body signals against config defaults.

    A request may tighten any policy; a policy named in config["locked"] can
    only be tightened, never loosened.
    """
    d = CONFIG["defaults"]
    locked = CONFIG["locked"]
    flags = [f.strip().lower() for f in flagstr.split(",") if f.strip()]

    # Header mirrors (for SDKs that can only set the model string).
    privacy_header = str(headers.get("X-Proxy-Privacy", "")).strip().lower()
    if privacy_header:
        flags.append(privacy_header)
    # A tools array implies the caller needs tool support.
    if _truthy(headers.get("X-Proxy-Require-Tools")) or (
        isinstance(body.get("tools"), list) and body["tools"]
    ):
        flags.append("tools")

    unknown = [f for f in flags if f not in MODE_FLAGS and f not in ("tools", "notools")]
    if unknown:
        # A typo like "auto:privte" must not silently fall back to a looser mode.
        raise PolicyError(
            f"unknown auto flag(s) {unknown}; valid: {sorted([*MODE_FLAGS, 'tools', 'notools'])}"
        )

    require_tools = bool(d.get("require_tools"))
    if "notools" in flags and "require_tools" not in locked:
        require_tools = False
    if "tools" in flags:
        require_tools = True

    requested = [MODE_FLAGS[f] for f in flags if f in MODE_FLAGS]
    # Conflicting privacy flags resolve to the strictest one.
    mode = min(requested, key=_STRICTNESS.index) if requested else d["mode"]
    if "mode" in locked:
        mode = min(mode, d["mode"], key=_STRICTNESS.index)

    return {"mode": mode, "require_tools": require_tools}


def resolve_policy(model_field, body: dict, headers) -> dict:
    """Turn the request's model string + flags + headers into a routing plan.

    Returns a dict:
      {"kind": "auto"|"concrete", "model": <slug or None>,
       "mode": <selection.MODES key>, "require_tools": bool,
       "fallback_auto": bool}   # concrete models: opt-in cascade to auto
    """
    raw = str(model_field or "auto").strip()
    raw = CONFIG.get("aliases", {}).get(raw, raw)  # e.g. "smart" -> "auto:strict"

    # A concrete slug (has "/" and isn't the auto sentinel) passes through.
    # A trailing ",auto[:flags]" opts it into cascade-to-auto on failure.
    head, _, tail = raw.partition(",")
    head, tail = head.strip(), tail.strip()
    if "/" in head and not head.startswith("auto"):
        if tail and tail.split(":")[0] != "auto":
            raise PolicyError(f"unknown fallback {tail!r}; only ',auto[:flags]' is supported")
        policy = _auto_policy(tail.partition(":")[2], body, headers)
        return {**policy, "kind": "concrete", "model": head, "fallback_auto": bool(tail)}

    # Anything else (including names with no "/", e.g. "gpt-4o") routes as auto.
    flagstr = raw.partition(":")[2] if raw.startswith("auto") else ""
    return {**_auto_policy(flagstr, body, headers), "kind": "auto", "model": None, "fallback_auto": False}


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_cascade(policy: dict) -> list[str]:
    """Ordered list of real model ids to try for this request."""
    auto: list[str] = []
    if policy["kind"] == "auto" or policy["fallback_auto"]:
        auto = RANKER.pick(selection.MODES[policy["mode"]], policy["require_tools"])
        auto = auto[: CONFIG["cascade_depth"]]
    if policy["kind"] == "concrete":
        return _dedupe([policy["model"], *auto])
    # No free model qualifies → 503 by default, unless a last-resort model is
    # configured (opt-in; may be paid, so it's off unless explicitly set).
    if not auto and CONFIG.get("last_resort_model"):
        return [CONFIG["last_resort_model"]]
    return auto


def is_retryable(status: int) -> bool:
    """Failures another model might not have. Bad key / no credits /
    moderation (401/402/403) fail everywhere; 400 can be model-specific
    (OpenRouter returns it for a removed model or an over-long prompt)."""
    return status not in (401, 402, 403)


def _first_data_line(buf: bytes) -> bytes | None:
    """Payload of the first complete SSE ``data:`` line in *buf*, if any."""
    for line in buf.split(b"\n")[:-1]:  # the last piece may be incomplete
        if line.startswith(b"data:"):
            return line[5:].strip()
    return None


def _event_error(data: bytes) -> str | None:
    try:
        event = json.loads(data)
    except ValueError:
        return None  # e.g. "[DONE]"
    if isinstance(event, dict) and event.get("error"):
        return f"upstream error: {json.dumps(event['error'])[:200]}"
    return None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openrouter-free-model-proxy/1.0"

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    # -- helpers -----------------------------------------------------------
    def _send_raw(self, status, body: bytes, content_type="application/json", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload, extra_headers=None):
        self._send_raw(status, json.dumps(payload).encode(), extra_headers=extra_headers)

    def _send_error(self, status, message, **extra):
        # OpenAI-style error shape, so SDKs surface `message` properly.
        self._send_json(status, {"error": {"message": message, "type": "proxy_error", "code": status, **extra}})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length < 0:
            raise ValueError("negative Content-Length")
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _query_mode(self, qs) -> selection.Mode:
        if "mode" in qs:
            name = MODE_FLAGS.get(qs["mode"][0].strip().lower())
            if not name:
                raise PolicyError(f"unknown mode {qs['mode'][0]!r}; valid: {list(selection.MODES)}")
            return selection.MODES[name]
        if _truthy(qs.get("any", ["0"])[0]):
            return selection.MODES["loose"]
        return selection.MODES[CONFIG["defaults"]["mode"]]

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            mode = self._query_mode(qs)
        except PolicyError as e:
            self._send_error(400, str(e))
            return
        require_tools = _truthy(qs.get("tools", ["0"])[0])
        force = _truthy(qs.get("refresh", ["0"])[0])

        if path == "/status":
            self._send_json(200, RANKER.status(mode))
            return
        if path in ("/models", "/v1/models"):
            try:
                rows = RANKER.list_all(mode, require_tools=require_tools, force=force)
            except Exception as e:
                self._send_error(503, f"could not fetch models: {e}")
                return
            if path == "/v1/models":
                data = [{"id": r["id"], "object": "model"} for r in rows if r["selectable"]]
                self._send_json(200, {"object": "list", "data": data})
            else:
                self._send_json(200, {"object": "list", "mode": mode.name, "models": rows})
            return
        self._send_error(404, "not found")

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        # Always consume the body first: an unread body left on a keep-alive
        # connection would be parsed as the next request.
        try:
            body = self._read_body()
        except ValueError:
            self.close_connection = True
            self._send_error(400, "invalid JSON body")
            return

        if urlparse(self.path).path != "/v1/chat/completions":
            self._send_error(404, "not found")
            return

        auth = self.headers.get("Authorization")
        if not auth:
            self._send_error(401, "missing Authorization header (send your OpenRouter key)")
            return
        if not isinstance(body, dict):
            self._send_error(400, "request body must be a JSON object")
            return

        try:
            policy = resolve_policy(body.get("model"), body, self.headers)
        except PolicyError as e:
            self._send_error(400, str(e))
            return
        try:
            cascade = build_cascade(policy)
        except Exception as e:
            self._send_error(503, f"could not resolve model: {e}")
            return
        if not cascade:
            self._send_error(503, "no qualifying free model right now (all down / filtered / denylisted)")
            return

        stream = bool(body.get("stream"))
        attempt = self._stream_one if stream else self._complete_one
        started = time.monotonic()
        errors: dict[str, str] = {}
        statuses: list[int | None] = []
        self._last_upstream = None
        fwd_headers = {"Authorization": auth, "Content-Type": "application/json"}
        for idx, model_name in enumerate(cascade):
            payload = {**body, "model": model_name}
            try:
                done, status = attempt(model_name, payload, fwd_headers, idx > 0, errors)
            except requests.RequestException as e:
                errors[model_name] = str(e)
                done, status = False, None
            if done:
                self._log_done(model_name, idx, started, "stream" if stream else "json", status)
                return
            statuses.append(status)

        log.warning("all %d candidate model(s) failed: %s", len(cascade), errors)
        last = self._last_upstream
        # Every model failed the same way (all 429, or a request no model accepts):
        # relay OpenRouter's own error so the client sees the real reason.
        if last and all(st == last[0] for st in statuses):
            self._send_raw(*last)
            return
        self._send_error(502, "all candidate models failed", details=errors)

    def _complete_one(self, model_name, payload, headers, is_fallback, errors):
        """Returns (done, status). done=True means a response was sent."""
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=CONFIG["request_timeout"])
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                errors[model_name] = "HTTP 200 with a non-JSON body"
                return False, 502
            # OpenRouter reports some upstream provider failures as 200 + error body.
            if isinstance(data, dict) and data.get("error"):
                errors[model_name] = f"upstream error: {json.dumps(data['error'])[:200]}"
                code = data["error"].get("code") if isinstance(data["error"], dict) else None
                return False, code if isinstance(code, int) else 502
            extra = {"X-Proxy-Model": model_name}
            if is_fallback:
                extra["X-Proxy-Fallback"] = "true"
            self._send_json(200, data, extra)
            return True, 200
        return self._upstream_failure(model_name, resp, errors)

    def _upstream_failure(self, model_name, resp, errors):
        errors[model_name] = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if is_retryable(resp.status_code):
            self._last_upstream = (
                resp.status_code, resp.content, resp.headers.get("Content-Type", "application/json"),
            )
            return False, resp.status_code
        # Another model can't fix this one — hand the client OpenRouter's own error.
        self._send_raw(
            resp.status_code, resp.content,
            resp.headers.get("Content-Type", "application/json"),
            {"X-Proxy-Model": model_name},
        )
        return True, resp.status_code

    def _stream_one(self, model_name, payload, headers, is_fallback, errors):
        """Returns (done, status). done=True means a response was sent."""
        resp = requests.post(
            OPENROUTER_URL, headers=headers, json=payload,
            timeout=CONFIG["request_timeout"], stream=True,
        )
        try:
            if resp.status_code != 200:
                return self._upstream_failure(model_name, resp, errors)

            # Hold output until the first data event: OpenRouter can answer 200
            # and then report the failure as its first event, and only before
            # any bytes reach the client can we still cascade.
            chunks = resp.iter_content(chunk_size=None)
            buf, first = b"", None
            for chunk in chunks:
                buf += chunk
                first = _first_data_line(buf)
                if first is not None:
                    break
            failure = "stream ended before any data" if first is None else _event_error(first)
            if failure:
                errors[model_name] = failure
                return False, 502

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Proxy-Model", model_name)
            if is_fallback:
                self.send_header("X-Proxy-Fallback", "true")
            self.end_headers()
            try:
                self.wfile.write(buf)
                self.wfile.flush()
                for chunk in chunks:
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (requests.RequestException, OSError) as e:
                # Headers are sent, so there's no cascading now — just end the stream.
                log.warning("stream from %s cut short: %s", model_name, e)
            return True, 200
        finally:
            resp.close()

    def _log_done(self, model_name, idx, started, kind, status):
        ms = int((time.monotonic() - started) * 1000)
        log.info(
            "served model=%s status=%s fallback=%s attempts=%d %s %dms",
            model_name, status, idx > 0, idx + 1, kind, ms,
        )


def main():
    hour, minute = _parse_hhmm(CONFIG.get("daily_refresh_at", "00:01"))
    RANKER.start_background_refresh(hour, minute)
    server = ThreadingHTTPServer((CONFIG["host"], CONFIG["port"]), Handler)
    log.info(
        "openrouter-free-model-proxy on http://%s:%s  (base_url .../v1, mode %s, daily refresh at %02d:%02d UTC)",
        CONFIG["host"], CONFIG["port"], CONFIG["defaults"]["mode"], hour, minute,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
