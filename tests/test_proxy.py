import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import proxy


# ---- policy resolution ---------------------------------------------------

def _policy(model, body=None, headers=None):
    return proxy.resolve_policy(model, body or {}, headers or {})


@pytest.fixture
def config(monkeypatch):
    cfg = {**proxy.CONFIG, "defaults": {"require_tools": False, "mode": "balanced"}, "locked": set()}
    monkeypatch.setattr(proxy, "CONFIG", cfg)
    return cfg


@pytest.mark.parametrize("model, mode", [
    ("auto", "balanced"),
    ("auto:strict", "strict"),
    ("auto:relaxed", "relaxed"),
    ("auto:loose", "loose"),
    # pre-mode flags keep working
    ("auto:private", "strict"),
    ("auto:logs", "balanced"),
    ("auto:any", "loose"),
    ("smart", "strict"),
    ("gpt-4o", "balanced"),
])
def test_mode_flags(config, model, mode):
    p = _policy(model)
    assert p["kind"] == "auto" and p["mode"] == mode


def test_combined_flags_and_conflicts_pick_the_strictest(config):
    p = _policy("auto:tools,loose,private")
    assert p["mode"] == "strict" and p["require_tools"]


def test_unknown_flag_is_rejected_not_ignored(config):
    with pytest.raises(proxy.PolicyError):
        _policy("auto:privte")


def test_headers_and_tools_array(config):
    p = _policy("auto", body={"tools": [{"type": "function"}]}, headers={"X-Proxy-Privacy": "relaxed"})
    assert p["mode"] == "relaxed" and p["require_tools"]
    assert _policy("auto:notools", body={"tools": [{}]})["require_tools"]


def test_locked_mode_can_tighten_but_not_loosen(config):
    config["defaults"]["mode"] = "balanced"
    config["locked"] = {"mode"}
    assert _policy("auto:any")["mode"] == "balanced"
    assert _policy("auto:strict")["mode"] == "strict"


def test_legacy_config_booleans_map_to_modes(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"defaults": {"require_private": True}, "locked": ["allow_trains"]}))
    cfg = proxy.load_config(str(path))
    assert cfg["defaults"]["mode"] == "strict" and cfg["locked"] == {"mode"}
    path.write_text(json.dumps({"defaults": {"allow_trains": True}}))
    assert proxy.load_config(str(path))["defaults"]["mode"] == "loose"


def test_bad_config_fails_loudly(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(SystemExit):
        proxy.load_config(str(path))
    path.write_text(json.dumps({"defaults": {"mode": "yolo"}}))
    with pytest.raises(SystemExit):
        proxy.load_config(str(path))


def test_concrete_model_with_auto_fallback_flags(config):
    p = _policy("vendor/model,auto:strict")
    assert (p["kind"], p["model"], p["fallback_auto"], p["mode"]) == ("concrete", "vendor/model", True, "strict")
    assert not _policy("vendor/model")["fallback_auto"]
    with pytest.raises(proxy.PolicyError):
        _policy("vendor/model,other/model")


# ---- end-to-end against a fake OpenRouter --------------------------------

def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server


class FakeRanker:
    def __init__(self, ids):
        self.ids = ids

    def pick(self, mode, require_tools=False, force=False):
        return list(self.ids)


@pytest.fixture
def upstream(monkeypatch, config):
    """script: model id -> (status, body) or ("sse", [events]). Records calls."""
    script, calls = {}, []

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body["model"])
            kind, payload = script[body["model"]]
            if kind == "sse":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in payload:
                    self.wfile.write(event.encode())
                return
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(kind)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    up = _serve(Upstream)
    monkeypatch.setattr(proxy, "OPENROUTER_URL", f"http://127.0.0.1:{up.server_port}/")
    monkeypatch.setattr(proxy, "RANKER", FakeRanker(["m/one", "m/two", "m/three"]))
    px = _serve(proxy.Handler)
    yield script, calls, f"http://127.0.0.1:{px.server_port}"
    up.shutdown()
    px.shutdown()


OK = {"choices": [{"message": {"content": "hi"}}]}


def _chat(base, **body):
    return requests.post(f"{base}/v1/chat/completions", json={"model": "auto", **body},
                         headers={"Authorization": "Bearer k"}, timeout=10)


def test_cascades_past_rate_limits(upstream):
    script, calls, base = upstream
    script.update({"m/one": (429, {"error": "rl"}), "m/two": (200, OK)})
    r = _chat(base)
    assert r.status_code == 200 and r.headers["X-Proxy-Model"] == "m/two"
    assert r.headers["X-Proxy-Fallback"] == "true"
    assert calls == ["m/one", "m/two"]


@pytest.mark.parametrize("status", [400, 401, 402, 403])
def test_unfixable_errors_are_relayed_without_cascading(upstream, status):
    script, calls, base = upstream
    script["m/one"] = (status, {"error": {"message": "nope", "code": status}})
    r = _chat(base)
    assert r.status_code == status and r.json()["error"]["message"] == "nope"
    assert calls == ["m/one"]


def test_200_with_error_body_cascades(upstream):
    script, calls, base = upstream
    script.update({"m/one": (200, {"error": {"code": 502, "message": "provider down"}}),
                   "m/two": (200, "not json"), "m/three": (200, OK)})
    r = _chat(base)
    assert r.status_code == 200 and r.headers["X-Proxy-Model"] == "m/three"


def test_all_rate_limited_returns_429(upstream):
    script, _, base = upstream
    for m in ("m/one", "m/two", "m/three"):
        script[m] = (429, {"error": "rl"})
    r = _chat(base)
    assert r.status_code == 429 and set(r.json()["error"]["details"]) == {"m/one", "m/two", "m/three"}


def test_mixed_failures_return_502(upstream):
    script, _, base = upstream
    script.update({"m/one": (429, {}), "m/two": (500, {}), "m/three": (503, {})})
    assert _chat(base).status_code == 502


def test_stream_error_as_first_event_cascades(upstream):
    script, calls, base = upstream
    script["m/one"] = ("sse", [": OPENROUTER PROCESSING\n\n", 'data: {"error": {"code": 429}}\n\n'])
    script["m/two"] = ("sse", [": OPENROUTER PROCESSING\n\n", 'data: {"choices": []}\n\n', "data: [DONE]\n\n"])
    r = _chat(base, stream=True)
    assert r.status_code == 200 and r.headers["X-Proxy-Model"] == "m/two"
    assert r.text == ': OPENROUTER PROCESSING\n\ndata: {"choices": []}\n\ndata: [DONE]\n\n'
    assert calls == ["m/one", "m/two"]


def test_unknown_flag_is_a_400(upstream):
    _, calls, base = upstream
    r = _chat(base, model="auto:privte")
    assert r.status_code == 400 and "privte" in r.json()["error"]["message"]
    assert calls == []


def test_non_object_body_is_a_400(upstream):
    _, _, base = upstream
    r = requests.post(f"{base}/v1/chat/completions", data="[1]", headers={"Authorization": "k"}, timeout=10)
    assert r.status_code == 400


def test_rejected_request_body_does_not_poison_keepalive(upstream):
    script, _, base = upstream
    script["m/one"] = (200, OK)
    conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=10)
    body = json.dumps({"model": "auto"})
    conn.request("POST", "/v1/chat/completions", body)  # no auth → 401
    first = conn.getresponse()
    first.read()
    assert first.status == 401
    conn.request("POST", "/v1/chat/completions", body, {"Authorization": "k"})
    assert conn.getresponse().status == 200
