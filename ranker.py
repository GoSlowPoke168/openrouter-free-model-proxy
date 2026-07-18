"""ranker.py — build and cache the ranked list of usable free models.

Wraps the copied openrouter.py fetchers + selection.py logic into a single
cached "what are the best free models right now" service the proxy can query
per request without re-scraping OpenRouter every time.

Caching model:
- The ranked selection is rebuilt at most every ``ttl_seconds`` (a cold or
  stale cache triggers a rebuild; a background thread also refreshes it so
  live requests rarely block on scrapes).
- Privacy scrapes (the expensive per-model page fetch) are cached for 24h on
  disk, mirroring the sibling rotator, so a rebuild is usually a couple of
  cheap API calls.
- A rebuild that fails (network blip, parse error) keeps serving the last
  good selection — the selection is never wiped by a transient error.

Everything lives under ~/.cache/openrouter-free-model-proxy/ (honouring
XDG_CACHE_HOME); nothing here depends on Hermes.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path
from typing import Any

import openrouter
import selection

PRIVACY_TTL_HOURS = 24


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "openrouter-free-model-proxy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


class Ranker:
    """Thread-safe, TTL-cached provider of the ranked free-model list.

    One ``Ranker`` instance is shared by all request threads. Selections are
    keyed by ``require_tools`` (the tool filter changes eligibility), so the
    proxy can ask for a tools-capable list or a don't-care list independently,
    each with its own TTL.
    """

    def __init__(self, ttl_seconds: int = 3600, denylist: dict[str, list[str]] | None = None, logger=None):
        self.ttl = ttl_seconds
        self.denylist = denylist or {"models": [], "providers": []}
        self.log = logger
        self._lock = threading.Lock()
        # keyed by require_tools -> {"built": datetime, "rows": [...]}
        self._selections: dict[bool, dict[str, Any]] = {}
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
        except (KeyError, ValueError):
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
    def _denied(self, candidate) -> str | None:
        models = {m.lower() for m in self.denylist.get("models", [])}
        providers = {p.lower() for p in self.denylist.get("providers", [])}
        if candidate.id.lower() in models or candidate.base_slug.lower() in models:
            return "denylisted (model)"
        prov = (candidate.endpoint_provider or "").lower()
        if prov and prov in providers:
            return f"denylisted (provider {candidate.endpoint_provider})"
        return None

    # -- build -------------------------------------------------------------
    def _build(self, require_tools: bool) -> list[dict[str, Any]]:
        api_models = openrouter.fetch_free_models()
        collection_order = openrouter.fetch_collection_order()
        result = selection.select_models(
            api_models,
            collection_order,
            self._privacy_lookup,
            openrouter.fetch_availability,
            today=_dt.date.today(),
            want=32,                 # want the whole qualifying list, not a top-N
            require_tools=require_tools,
        )
        self._save_privacy_cache()

        rows: list[dict[str, Any]] = []
        for c in result.candidates:
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
                    "reason": denied or c.reason,
                    "selectable": bool(c.tier in (openrouter.TIER_PRIVATE, openrouter.TIER_LOGS))
                    and denied is None,
                    "denied": denied is not None,
                }
            )
        # Best-first: ranked selectable rows lead, in rank order.
        rows.sort(key=lambda r: (not r["selectable"], r["rank"] is None, r["rank"] or 1e9))
        return rows

    def _get_rows(self, require_tools: bool, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            entry = self._selections.get(require_tools)
            fresh = (
                entry
                and not force
                and (_now() - entry["built"]).total_seconds() < self.ttl
            )
            if fresh:
                return entry["rows"]

        # Rebuild outside the lock (network I/O); tolerate failure.
        try:
            rows = self._build(require_tools)
        except Exception as e:  # keep serving last good selection
            if self.log:
                self.log.warning("ranker rebuild failed (require_tools=%s): %s", require_tools, e)
            with self._lock:
                entry = self._selections.get(require_tools)
            if entry:
                return entry["rows"]
            raise

        with self._lock:
            self._selections[require_tools] = {"built": _now(), "rows": rows}
        return rows

    # -- public API --------------------------------------------------------
    def pick(self, require_tools: bool, require_private: bool, force: bool = False) -> list[str]:
        """Ranked model ids for the cascade (best first), honouring filters."""
        rows = self._get_rows(require_tools, force=force)
        out = []
        for r in rows:
            if not r["selectable"]:
                continue
            if require_private and r["tier"] != openrouter.TIER_PRIVATE:
                continue
            out.append(r["id"])
        return out

    def list_all(self, require_tools: bool = False, force: bool = False) -> list[dict[str, Any]]:
        """Full ranked list (including skipped models + reasons) for /models."""
        return self._get_rows(require_tools, force=force)

    def status(self) -> dict[str, Any]:
        with self._lock:
            info = {
                str(k): {
                    "built": v["built"].isoformat(timespec="seconds"),
                    "age_seconds": int((_now() - v["built"]).total_seconds()),
                    "count": len(v["rows"]),
                }
                for k, v in self._selections.items()
            }
        top = None
        try:
            picks = self.pick(require_tools=False, require_private=False)
            top = picks[0] if picks else None
        except Exception:
            pass
        return {"ttl_seconds": self.ttl, "top_auto_pick": top, "selections": info}

    def start_background_refresh(self) -> None:
        """Warm both selection variants periodically so requests rarely block."""

        def loop():
            import time

            while True:
                for rt in (False, True):
                    try:
                        self._get_rows(rt, force=True)
                    except Exception:
                        pass
                time.sleep(max(60, self.ttl))

        t = threading.Thread(target=loop, name="ranker-refresh", daemon=True)
        t.start()
