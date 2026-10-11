"""ranker.py — build and cache the ranked list of usable free models.

Wraps the openrouter.py fetchers + selection.py logic into a single cached
"what are the best free models right now" service the proxy can query per
request without re-scraping OpenRouter every time.

Caching model:
- One classified list per rebuild serves every request policy (mode, tools);
  per-policy ordering is pure and happens at pick time.
- A background thread builds once at startup, then force-rebuilds daily at a
  fixed UTC time (default 00:01, shortly after OpenRouter's free-tier quota
  resets at 00:00 UTC), so it's freshly scraped daily with no drift.
- ``ttl_seconds`` is an on-demand safety net: a request that finds the cache
  missing or older than that rebuilds synchronously rather than serving
  nothing. Only one rebuild runs at a time; concurrent callers wait for it.
- Privacy scrapes (the expensive per-model page fetch) are cached on disk.
- A rebuild that fails (network blip, parse error) keeps serving the last
  good list — it is never wiped by a transient error.

Everything lives under ~/.cache/openrouter-free-model-proxy/ (honouring
XDG_CACHE_HOME).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import openrouter
import selection

# Under 24h so each daily rebuild re-scrapes instead of reusing yesterday's entry.
PRIVACY_TTL_HOURS = 20
DEFAULT_REFRESH_HOUR = 0
DEFAULT_REFRESH_MINUTE = 1


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _next_daily_run(hour: int, minute: int) -> _dt.datetime:
    """The next occurrence of hour:minute UTC, today or tomorrow.

    UTC rather than local time so this stays pinned to OpenRouter's daily
    quota reset (00:00 UTC) year-round, with no DST drift.
    """
    now = _now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=1)
    return target


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "openrouter-free-model-proxy"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Ranker:
    """Thread-safe, TTL-cached provider of the ranked free-model list.

    One instance is shared by all request threads.
    """

    def __init__(self, ttl_seconds: int = 3600, denylist: dict[str, list[str]] | None = None, logger=None):
        self.ttl = ttl_seconds
        self.denylist = denylist or {"models": [], "providers": []}
        self.log = logger
        self._build_lock = threading.Lock()
        # {"built": datetime, "candidates": [...]}; replaced wholesale, never mutated.
        self._snapshot: dict[str, Any] | None = None
        self._privacy_cache = self._load_privacy_cache()

    # -- privacy cache (persisted) -----------------------------------------
    def _privacy_path(self) -> Path:
        return cache_dir() / "privacy.json"

    def _load_privacy_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self._privacy_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save_privacy_cache(self) -> None:
        tmp = self._privacy_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._privacy_cache, indent=2, sort_keys=True))
        os.replace(tmp, self._privacy_path())

    def _cached_privacy(self, base_slug: str) -> openrouter.Privacy | None:
        entry = self._privacy_cache.get(base_slug)
        if not isinstance(entry, dict):
            return None
        try:
            checked = _dt.datetime.fromisoformat(entry["checked"])
        except (KeyError, ValueError, TypeError):
            return None
        if _now() - checked > _dt.timedelta(hours=PRIVACY_TTL_HOURS):
            return None
        return openrouter.Privacy.from_dict(entry)

    def _privacy_lookup(self, base_slug: str) -> openrouter.Privacy:
        cached = self._cached_privacy(base_slug)
        if cached is not None:
            return cached
        privacy = openrouter.fetch_privacy(base_slug)
        if privacy.tier != openrouter.TIER_UNKNOWN:
            self._privacy_cache[base_slug] = {**privacy.to_dict(), "checked": _now_iso()}
        return privacy

    # -- denylist ----------------------------------------------------------
    def _denied(self, candidate: selection.Candidate) -> str | None:
        models = {m.lower() for m in self.denylist.get("models", [])}
        providers = {p.lower() for p in self.denylist.get("providers", [])}
        if candidate.id.lower() in models or candidate.base_slug.lower() in models:
            return "denylisted (model)"
        prov = (candidate.endpoint_provider or "").lower()
        if prov and prov in providers:
            return f"denylisted (provider {candidate.endpoint_provider})"
        return None

    # -- build -------------------------------------------------------------
    def _build(self) -> list[selection.Candidate]:
        candidates = selection.classify_models(
            openrouter.fetch_free_models(),
            openrouter.fetch_collection_order(),
            self._privacy_lookup,
            openrouter.fetch_availability,
            today=_now().date(),
        )
        try:
            self._save_privacy_cache()
        except OSError as e:
            if self.log:
                self.log.warning("could not save privacy cache: %s", e)
        return candidates

    def _fresh(self, snap: dict[str, Any] | None) -> bool:
        return bool(snap) and (_now() - snap["built"]).total_seconds() < self.ttl

    def _candidates(self, force: bool = False) -> list[selection.Candidate]:
        snap = self._snapshot
        if not force and self._fresh(snap):
            return snap["candidates"]

        requested = _now()
        with self._build_lock:
            snap = self._snapshot
            # Another thread may have finished a rebuild while we waited.
            if self._fresh(snap) and (not force or snap["built"] >= requested):
                return snap["candidates"]
            try:
                candidates = self._build()
            except Exception as e:  # keep serving last good list
                if self.log:
                    self.log.warning("ranker rebuild failed: %s", e)
                if snap:
                    return snap["candidates"]
                raise
            self._snapshot = {"built": _now(), "candidates": candidates}
            return candidates

    # -- public API --------------------------------------------------------
    def _ordered(
        self, candidates: list[selection.Candidate], mode: selection.Mode, require_tools: bool
    ) -> list[selection.Candidate]:
        allowed = [c for c in candidates if self._denied(c) is None]
        return selection.order_for_mode(allowed, mode, require_tools)

    def pick(self, mode: selection.Mode, require_tools: bool = False, force: bool = False) -> list[str]:
        """Ranked model ids for the cascade (best first), honouring filters."""
        return [c.id for c in self._ordered(self._candidates(force), mode, require_tools)]

    def list_all(
        self, mode: selection.Mode, require_tools: bool = False, force: bool = False
    ) -> list[dict[str, Any]]:
        """Full list for /models: the cascade in order, then everything else by rank."""
        candidates = self._candidates(force)
        ordered = self._ordered(candidates, mode, require_tools)
        chosen = {id(c) for c in ordered}
        rows = []
        for c in ordered + [c for c in candidates if id(c) not in chosen]:
            denied = self._denied(c)
            rows.append(
                {
                    "id": c.id,
                    "rank": c.rank,
                    "tier": c.tier,
                    "tools": c.supports_tools,
                    "uptime_1d": c.uptime_1d,
                    "expires": c.expiration_date,
                    "endpoint_provider": c.endpoint_provider,
                    "reason": denied or selection.describe(c, mode, require_tools),
                    "selectable": id(c) in chosen,
                    "denied": denied is not None,
                }
            )
        return rows

    def status(self, mode: selection.Mode) -> dict[str, Any]:
        """Cache health. Never triggers a rebuild, so it can't block."""
        snap = self._snapshot
        info: dict[str, Any] = {"ttl_seconds": self.ttl, "mode": mode.name, "built": None,
                                "age_seconds": None, "count": 0, "top_auto_pick": None}
        if snap:
            ordered = self._ordered(snap["candidates"], mode, False)
            info.update(
                built=snap["built"].isoformat(timespec="seconds"),
                age_seconds=int((_now() - snap["built"]).total_seconds()),
                count=len(snap["candidates"]),
                top_auto_pick=ordered[0].id if ordered else None,
            )
        return info

    def start_background_refresh(
        self, hour: int = DEFAULT_REFRESH_HOUR, minute: int = DEFAULT_REFRESH_MINUTE
    ) -> None:
        """Warm the cache now, then force a rebuild daily at hour:minute UTC,
        so requests are served from a same-day-fresh cache without blocking."""

        def loop():
            force = False
            while True:
                try:
                    self._candidates(force=force)
                except Exception as e:
                    if self.log:
                        self.log.warning("scheduled rebuild failed: %s", e)
                force = True
                time.sleep(max(0.0, (_next_daily_run(hour, minute) - _now()).total_seconds()))

        threading.Thread(target=loop, name="ranker-refresh", daemon=True).start()
