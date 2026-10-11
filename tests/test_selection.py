import datetime as dt

import pytest

from openrouter import TIER_LOGS, TIER_PRIVATE, TIER_TRAINS, TIER_UNKNOWN, Availability, Privacy
from selection import MODES, classify_models, describe, is_expired, order_for_mode

TODAY = dt.date(2026, 7, 17)


def _model(slug, tools=True, expires=None, created=100, context=256000):
    return {
        "id": f"{slug}:free",
        "name": slug,
        "context_length": context,
        "created": created,
        "expiration_date": expires,
        "supported_parameters": ["temperature"] + (["tools"] if tools else []),
    }


def _privacy_map(tiers):
    return lambda base_slug: Privacy(tier=tiers.get(base_slug, TIER_PRIVATE))


def _avail(down=(), missing=()):
    def lookup(model_id):
        if model_id in down:
            return Availability(False, "free endpoint down (uptime 0% 1d)", 0.0)
        if model_id in missing:
            return Availability(False, "no free endpoint currently offered", has_free_endpoint=False)
        return Availability(True, "uptime 99% (1d)", 99.0)
    return lookup


def _ids(cands):
    return [c.id.removesuffix(":free") for c in cands]


def _classify(models, order, tiers=None, **kw):
    return classify_models(models, order, _privacy_map(tiers or {}), kw.pop("avail", _avail()),
                           today=TODAY, **kw)


# Ranked a, b, c, d; b and d retain prompts, c trains.
TIERS = {"b/two": TIER_LOGS, "c/three": TIER_TRAINS, "d/four": TIER_LOGS}
ORDER = ["b/two", "c/three", "a/one", "d/four"]


@pytest.fixture
def mixed():
    models = [_model(s) for s in ("a/one", "b/two", "c/three", "d/four")]
    return _classify(models, ORDER, TIERS)


def test_is_expired_buffer():
    assert is_expired("2026-07-18", TODAY, buffer_days=2)
    assert is_expired("2026-07-17", TODAY)
    assert not is_expired("2026-07-25", TODAY, buffer_days=2)
    assert not is_expired(None, TODAY)
    assert is_expired("not-a-date", TODAY)


def test_collection_order_ranks():
    cands = _classify([_model("a/one"), _model("b/two"), _model("c/three")], ["c/three", "a/one", "b/two"])
    assert _ids(order_for_mode(cands, MODES["balanced"])) == ["c/three", "a/one", "b/two"]


def test_fallback_ranking_by_created_when_no_collection_order():
    cands = _classify([_model("a/old", created=1), _model("b/new", created=9)], None)
    assert _ids(order_for_mode(cands, MODES["balanced"])) == ["b/new", "a/old"]


def test_tools_filter_happens_at_pick_time():
    cands = _classify([_model("a/one", tools=False), _model("b/two")], ["a/one", "b/two"])
    assert _ids(order_for_mode(cands, MODES["balanced"])) == ["a/one", "b/two"]
    assert _ids(order_for_mode(cands, MODES["balanced"], require_tools=True)) == ["b/two"]
    assert "tool calling" in describe(cands[0], MODES["balanced"], require_tools=True)


def test_expiring_model_skipped():
    cands = _classify([_model("a/one", expires="2026-07-18"), _model("b/two")], ["a/one", "b/two"])
    assert _ids(order_for_mode(cands, MODES["loose"])) == ["b/two"]
    assert "expires" in next(c for c in cands if c.base_slug == "a/one").reason


def test_down_model_never_selected_even_if_top_ranked():
    cands = _classify([_model("a/one"), _model("b/two")], ["a/one", "b/two"], avail=_avail(down={"a/one:free"}))
    assert _ids(order_for_mode(cands, MODES["loose"])) == ["b/two"]


def test_availability_checked_before_privacy():
    scraped = []

    def privacy(slug):
        scraped.append(slug)
        return Privacy(tier=TIER_PRIVATE)

    classify_models([_model("a/one"), _model("b/two")], ["a/one", "b/two"], privacy,
                    _avail(missing={"a/one:free"}), today=TODAY)
    assert scraped == ["b/two"]


def test_unknown_tier_is_never_selectable():
    cands = _classify([_model("a/one")], ["a/one"], {"a/one": TIER_UNKNOWN})
    for mode in MODES.values():
        assert order_for_mode(cands, mode) == []
    assert "could not be determined" in describe(cands[0], MODES["loose"])


def test_classification_budget_caps_lookups():
    calls = []
    cands = classify_models(
        [_model(f"m/{i}") for i in range(6)], None,
        lambda s: calls.append(s) or Privacy(tier=TIER_PRIVATE), _avail(),
        today=TODAY, max_classifications=4,
    )
    assert len(calls) == 4
    assert len(order_for_mode(cands, MODES["balanced"])) == 4


def test_malformed_api_entry_is_skipped_not_fatal():
    cands = _classify([{"id": "x/bad:free", "context_length": "lots"}, _model("a/one")], None)
    assert _ids(cands) == ["a/one"]


# ---- modes ---------------------------------------------------------------

def test_strict_selects_only_private(mixed):
    assert _ids(order_for_mode(mixed, MODES["strict"])) == ["a/one"]


def test_balanced_puts_every_private_above_any_logs(mixed):
    # b/two is the most popular model but retains prompts.
    assert _ids(order_for_mode(mixed, MODES["balanced"])) == ["a/one", "b/two", "d/four"]
    held = next(c for c in mixed if c.base_slug == "b/two")
    assert describe(held, MODES["balanced"]).startswith("held: logs")


def test_relaxed_lets_a_higher_ranked_logs_model_win(mixed):
    assert _ids(order_for_mode(mixed, MODES["relaxed"])) == ["b/two", "a/one", "d/four"]


def test_loose_is_pure_rank_order_including_trains(mixed):
    assert _ids(order_for_mode(mixed, MODES["loose"])) == ["b/two", "c/three", "a/one", "d/four"]


def test_trains_excluded_outside_loose(mixed):
    trains = next(c for c in mixed if c.base_slug == "c/three")
    for name in ("strict", "balanced", "relaxed"):
        assert trains not in order_for_mode(mixed, MODES[name])
        assert f"not selectable in {name} mode" in describe(trains, MODES[name])
