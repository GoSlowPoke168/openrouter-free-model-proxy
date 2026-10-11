import threading
import time

import pytest

import openrouter
import ranker as ranker_mod
from openrouter import TIER_LOGS, TIER_PRIVATE, Availability, Privacy
from selection import MODES


@pytest.fixture
def net(monkeypatch, tmp_path):
    """Fake OpenRouter: a/one is the most popular but logs-tier."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    state = {"builds": 0, "fail": False, "delay": 0.0}
    tiers = {"a/one": TIER_LOGS, "b/two": TIER_PRIVATE, "c/three": TIER_PRIVATE}

    def models():
        state["builds"] += 1
        time.sleep(state["delay"])
        if state["fail"]:
            raise OSError("network down")
        return [{"id": f"{s}:free", "supported_parameters": ["tools"] if s != "c/three" else []}
                for s in tiers]

    monkeypatch.setattr(openrouter, "fetch_free_models", models)
    monkeypatch.setattr(openrouter, "fetch_collection_order", lambda: list(tiers))
    monkeypatch.setattr(openrouter, "fetch_availability", lambda mid: Availability(True, "up", 99.0))
    monkeypatch.setattr(openrouter, "fetch_privacy",
                        lambda slug: Privacy(tier=tiers[slug], endpoint_provider=f"P-{slug}"))
    return state


def test_private_outranks_a_more_popular_logs_model(net):
    # Regression: rows used to be sorted by rank alone, so a/one (logs) led.
    r = ranker_mod.Ranker()
    assert r.pick(MODES["balanced"]) == ["b/two:free", "c/three:free", "a/one:free"]
    assert r.pick(MODES["strict"], require_tools=True) == ["b/two:free"]


def test_one_build_serves_every_policy(net):
    r = ranker_mod.Ranker()
    for mode in MODES.values():
        for tools in (False, True):
            r.pick(mode, tools)
    assert net["builds"] == 1


def test_list_all_leads_with_the_cascade_and_explains_the_rest(net):
    rows = ranker_mod.Ranker().list_all(MODES["strict"])
    assert [r["id"] for r in rows if r["selectable"]] == ["b/two:free", "c/three:free"]
    assert rows[-1]["id"] == "a/one:free" and "not selectable in strict" in rows[-1]["reason"]


def test_denylist_by_model_and_provider(net):
    r = ranker_mod.Ranker(denylist={"models": ["b/two"], "providers": ["p-c/three"]})
    assert r.pick(MODES["balanced"]) == ["a/one:free"]
    reasons = {row["id"]: row["reason"] for row in r.list_all(MODES["balanced"])}
    assert reasons["b/two:free"] == "denylisted (model)"
    assert reasons["c/three:free"].startswith("denylisted (provider")


def test_failed_rebuild_keeps_last_good_list(net):
    r = ranker_mod.Ranker()
    first = r.pick(MODES["balanced"])
    net["fail"] = True
    assert r.pick(MODES["balanced"], force=True) == first


def test_failed_first_build_raises(net):
    net["fail"] = True
    with pytest.raises(OSError):
        ranker_mod.Ranker().pick(MODES["balanced"])


def test_concurrent_cold_requests_share_one_build(net):
    net["delay"] = 0.2
    r = ranker_mod.Ranker()
    threads = [threading.Thread(target=r.pick, args=(MODES["balanced"],)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert net["builds"] == 1


def test_status_never_builds(net):
    r = ranker_mod.Ranker()
    assert r.status(MODES["balanced"])["top_auto_pick"] is None
    assert net["builds"] == 0
    r.pick(MODES["balanced"])
    assert r.status(MODES["balanced"])["top_auto_pick"] == "b/two:free"


def test_privacy_cache_persists_known_tiers(net, monkeypatch):
    ranker_mod.Ranker().pick(MODES["balanced"])
    monkeypatch.setattr(openrouter, "fetch_privacy", lambda slug: pytest.fail("should be cached"))
    assert ranker_mod.Ranker().pick(MODES["balanced"])[0] == "b/two:free"
