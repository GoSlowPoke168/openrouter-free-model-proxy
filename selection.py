"""Pure selection logic — no I/O, unit-testable.

Pipeline: hard filter (expiry buffer) → rank (collection order, falling back
to API metadata) → availability + privacy classification in rank order (via
injected lookups, so callers control caching/rate limits).

`classify_models` runs once per rebuild. `order_for_mode` then turns that one
classified list into a cascade for any `Mode` / tool requirement without
touching the network, so a request never waits on a per-policy rebuild.

A mode carries two independent rules — which tiers may be selected, and
whether tier order outranks popularity — so the four are a table, not a
scale. "unknown" is selectable in none of them: it means the privacy scrape
failed, not that a model is a particular kind.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Callable

from openrouter import TIER_LOGS, TIER_PRIVATE, TIER_TRAINS, TIER_UNKNOWN, Availability, Privacy

EXPIRY_BUFFER_DAYS = 1  # skip a model once it's within 1 day of expiring
# Safety valve, not a tuning knob: bounds one rebuild's work if OpenRouter
# ever ships hundreds of free models.
MAX_CLASSIFICATIONS = 40


@dataclass(frozen=True)
class Mode:
    name: str
    tiers: tuple[str, ...]  # selectable, best-privacy first
    tier_dominant: bool     # True: every tier beats the next. False: rank decides.
    summary: str


# Ordered strictest → loosest; a locked mode can only be tightened along this order.
MODES = {
    m.name: m
    for m in (
        Mode("strict", (TIER_PRIVATE,), True, "private only"),
        Mode("balanced", (TIER_PRIVATE, TIER_LOGS), True, "private first, logs as fallback"),
        Mode("relaxed", (TIER_PRIVATE, TIER_LOGS), False, "private + logs, best rank wins"),
        Mode("loose", (TIER_PRIVATE, TIER_LOGS, TIER_TRAINS), False,
             "adds models that train on prompts, best rank wins"),
    )
}
DEFAULT_MODE = "balanced"


@dataclass
class Candidate:
    id: str                      # "tencent/hy3:free"
    base_slug: str               # "tencent/hy3"
    name: str = ""
    context_length: int = 0
    created: int = 0
    expiration_date: str | None = None
    supports_tools: bool = False
    rank: int | None = None      # 1-based among non-expired models
    tier: str | None = None      # privacy tier once classified
    endpoint_provider: str | None = None
    uptime_1d: float | None = None   # best free-endpoint uptime %, once checked
    available: bool | None = None    # availability verdict; None = not checked
    reason: str = ""             # set only when the model is out regardless of mode

    @classmethod
    def from_api(cls, m: dict[str, Any]) -> "Candidate":
        model_id = str(m.get("id", ""))
        return cls(
            id=model_id,
            base_slug=model_id.removesuffix(":free"),
            name=str(m.get("name", model_id)),
            context_length=int(m.get("context_length") or 0),
            created=int(m.get("created") or 0),
            expiration_date=m.get("expiration_date") or None,
            supports_tools="tools" in (m.get("supported_parameters") or []),
        )


def _parse_date(value: str) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def is_expired(
    expiration_date: str | None, today: _dt.date, buffer_days: int = 0
) -> bool:
    """True when the model expires on/before today + buffer_days.

    An unparseable date is treated as expiring (a model whose lifecycle we
    can't reason about shouldn't become the default).
    """
    if not expiration_date:
        return False
    expiry = _parse_date(expiration_date)
    if expiry is None:
        return True
    return expiry <= today + _dt.timedelta(days=buffer_days)


def rank_candidates(
    candidates: list[Candidate], collection_order: list[str] | None
) -> list[Candidate]:
    """Order candidates best-first and stamp 1-based ranks."""
    order_index = {slug: i for i, slug in enumerate(collection_order or [])}
    # Models missing from the collection page sort after listed ones, newest
    # first, largest context as the tiebreak.
    ranked = sorted(
        candidates,
        key=lambda c: (
            order_index.get(c.base_slug, len(order_index)),
            -c.created,
            -c.context_length,
        ),
    )
    for i, c in enumerate(ranked):
        c.rank = i + 1
    return ranked


def classify_models(
    api_models: list[dict[str, Any]],
    collection_order: list[str] | None,
    privacy_of: Callable[[str], Privacy],
    availability_of: Callable[[str], Availability],
    *,
    today: _dt.date,
    expiry_buffer_days: int = EXPIRY_BUFFER_DAYS,
    max_classifications: int = MAX_CLASSIFICATIONS,
) -> list[Candidate]:
    """Every free model, ranked ones first in rank order, classified."""
    candidates: list[Candidate] = []
    for m in api_models:
        try:
            candidates.append(Candidate.from_api(m))
        except (TypeError, ValueError, AttributeError):
            continue  # one malformed API entry must not abort the rebuild

    eligible: list[Candidate] = []
    for c in candidates:
        if is_expired(c.expiration_date, today, expiry_buffer_days):
            c.reason = f"skipped: expires {c.expiration_date} (within {expiry_buffer_days}d buffer)"
        else:
            eligible.append(c)

    ranked = rank_candidates(eligible, collection_order)
    for i, c in enumerate(ranked):
        if i >= max_classifications:
            c.reason = "skipped: lookup budget exhausted"
            continue

        # Availability first — a cheap JSON call that skips down endpoints
        # before we spend a page scrape on privacy.
        availability = availability_of(c.id)
        c.uptime_1d = availability.best_uptime_1d
        c.available = availability.ok
        c.endpoint_provider = availability.endpoint_provider
        if not availability.ok:
            c.reason = f"skipped: {availability.reason}"
            continue

        privacy = privacy_of(c.base_slug)
        c.tier = privacy.tier
        c.endpoint_provider = privacy.endpoint_provider or availability.endpoint_provider

    return ranked + [c for c in candidates if c.rank is None]


def is_selectable(c: Candidate, mode: Mode, require_tools: bool = False) -> bool:
    return (
        not c.reason
        and c.available is True
        and c.tier in mode.tiers
        and (c.supports_tools or not require_tools)
    )


def order_for_mode(
    candidates: list[Candidate], mode: Mode, require_tools: bool = False
) -> list[Candidate]:
    """The selectable candidates, best first, as *mode* orders them."""
    picks = [c for c in candidates if is_selectable(c, mode, require_tools)]
    if mode.tier_dominant:
        # Tier beats popularity: a retains-prompts model can never outrank a
        # private one. Rank order is preserved within each tier.
        return sorted(picks, key=lambda c: (mode.tiers.index(c.tier), c.rank))
    return sorted(picks, key=lambda c: c.rank)


def describe(c: Candidate, mode: Mode, require_tools: bool = False) -> str:
    """Human-readable reason a model is or isn't in *mode*'s cascade."""
    if c.reason:
        return c.reason
    if require_tools and not c.supports_tools:
        return "skipped: free endpoint does not support tool calling"
    if c.tier in (None, TIER_UNKNOWN):
        return "skipped: privacy tier could not be determined"
    if c.tier not in mode.tiers:
        return f"skipped: {c.tier} tier — not selectable in {mode.name} mode"
    if mode.tier_dominant and c.tier != mode.tiers[0]:
        better = " or ".join(mode.tiers[: mode.tiers.index(c.tier)])
        return f"held: {c.tier} tier — used after every {better} model"
    return f"selectable: {c.tier} tier"
