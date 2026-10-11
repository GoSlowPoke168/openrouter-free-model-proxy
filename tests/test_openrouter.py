import json
from pathlib import Path

import pytest

import openrouter as _or
from openrouter import (
    assess_availability,
    TIER_LOGS,
    TIER_PRIVATE,
    TIER_TRAINS,
    TIER_UNKNOWN,
    decode_rsc_payload,
    parse_collection_order,
    parse_privacy,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _group_payload(*endpoints: dict) -> str:
    group = {"variant": "free", "endpoints": list(endpoints)}
    return f'noise{{"pad":1}} {json.dumps(group)} trailing'


def _ep(training=False, retains=False, provider="TestProv", eid="e1"):
    # Real free endpoints carry is_free:true alongside their data_policy.
    return {
        "id": eid,
        "provider_display_name": provider,
        "is_free": True,
        "variant": "free",
        "data_policy": {"training": training, "retainsPrompts": retains},
    }


def test_real_page_snippet_is_private():
    snippet = (FIXTURES / "hy3_rsc_snippet.txt").read_text()
    privacy = parse_privacy(snippet)
    assert privacy.tier == TIER_PRIVATE
    assert privacy.endpoint_provider == "NovitaAI"


def test_flat_endpoint_page_shape_is_private():
    # qwen-style page: endpoint objects carry data_policy directly (not nested
    # under a variant group). Regression for the enclosing-object matcher.
    fixture = (FIXTURES / "qwen_free_endpoint.json.txt").read_text()
    privacy = parse_privacy(fixture)
    assert privacy.tier == TIER_PRIVATE
    assert privacy.endpoint_provider == "Venice (Beta)"


def test_private_endpoint():
    privacy = parse_privacy(_group_payload(_ep()))
    assert privacy.tier == TIER_PRIVATE
    assert privacy.endpoint_provider == "TestProv"


def test_logs_endpoint():
    assert parse_privacy(_group_payload(_ep(retains=True))).tier == TIER_LOGS


def test_trains_endpoint():
    assert parse_privacy(_group_payload(_ep(training=True))).tier == TIER_TRAINS


def test_training_openrouter_counts_as_trains():
    ep = _ep()
    ep["data_policy"]["trainingOpenRouter"] = True
    assert parse_privacy(_group_payload(ep)).tier == TIER_TRAINS


def test_worst_case_across_multiple_free_endpoints():
    payload = _group_payload(
        _ep(eid="e1"), _ep(retains=True, provider="Retainer", eid="e2")
    )
    privacy = parse_privacy(payload)
    assert privacy.tier == TIER_LOGS
    assert privacy.endpoint_provider == "Retainer"


def test_missing_policy_is_unknown():
    payload = _group_payload({"id": "e1", "provider_display_name": "NoPolicy"})
    assert parse_privacy(payload).tier == TIER_UNKNOWN


def test_garbage_payload_is_unknown():
    assert parse_privacy('"variant":"free" not json at all').tier == TIER_UNKNOWN


def test_decode_rsc_payload():
    html = 'x self.__next_f.push([1,"ab\\"cd\\n"]) y self.__next_f.push([1,"ef"]) z'
    assert decode_rsc_payload(html) == 'ab"cd\nef'


def test_parse_collection_order_filters_and_dedupes():
    payload = (
        '"slug":"tencent/hy3" "slug":"tencent/hy3" "slug":"novita" '
        '"slug":"nvidia/nemotron-3-ultra-550b-a55b"'
    )
    assert parse_collection_order(payload) == [
        "tencent/hy3",
        "nvidia/nemotron-3-ultra-550b-a55b",
    ]
    assert parse_collection_order("no slugs here") is None


# ---- assess_availability (pure) ------------------------------------------

def test_assess_availability_healthy():
    data = {"endpoints": [{"provider_name": "Novita", "status": 0, "uptime_last_1d": 99.9}]}
    a = assess_availability(data)
    assert a.ok and a.best_uptime_1d == 99.9 and a.endpoint_provider == "Novita"


def test_assess_availability_down():
    data = {"endpoints": [{"provider_name": "Venice", "status": -5, "uptime_last_1d": 0}]}
    a = assess_availability(data)
    assert not a.ok and "down" in a.reason


def test_assess_availability_no_endpoints():
    a = assess_availability({"endpoints": []})
    assert not a.ok and "no free endpoint" in a.reason


def test_assess_availability_best_of_several():
    data = {"endpoints": [
        {"provider_name": "Down", "status": -5, "uptime_last_1d": 0},
        {"provider_name": "Up", "status": 0, "uptime_last_1d": 98.0},
    ]}
    a = assess_availability(data)
    assert a.ok and a.best_uptime_1d == 98.0


def test_assess_availability_unknown_uptime_but_status_ok():
    data = {"endpoints": [{"provider_name": "New", "status": 0, "uptime_last_1d": None}]}
    a = assess_availability(data)
    assert a.ok  # brand-new endpoint, status ok → not blocked


@pytest.mark.parametrize("status", ["ok", {}, [], "degraded"])
def test_assess_availability_survives_a_non_numeric_status(status):
    # This branch already means "no real signal"; it must not invent a
    # negative one — and must not raise, since a raise here runs per candidate
    # and would abort the whole sync.
    data = {"endpoints": [{"provider_name": "Odd", "status": status, "uptime_last_1d": None}]}
    assert assess_availability(data).ok


def test_assess_availability_still_trusts_a_numeric_negative_status():
    data = {"endpoints": [{"provider_name": "Down", "status": -1, "uptime_last_1d": None}]}
    assert not assess_availability(data).ok


def test_fetch_availability_fails_open_on_an_unparseable_payload(monkeypatch):
    # Runs once per candidate, so an unexpected shape must degrade to
    # "assume available", never take the sync down with it.
    monkeypatch.setattr(_or, "_http_get", lambda *a, **k: '{"data": {"endpoints": "nonsense"}}')
    monkeypatch.setattr(
        _or, "assess_availability", lambda *a, **k: (_ for _ in ()).throw(TypeError("boom"))
    )
    a = _or.fetch_availability("x:free")
    assert a.ok and a.fetched is False
    assert "unexpected endpoints payload" in a.reason


def test_fetch_availability_fails_open_when_the_api_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        _or, "_http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("no network"))
    )
    a = _or.fetch_availability("x:free")
    assert a.ok and a.fetched is False
    assert "unreachable" in a.reason
