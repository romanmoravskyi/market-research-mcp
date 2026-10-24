"""FRED (Federal Reserve Economic Data) macro series.

Endpoints: https://api.stlouisfed.org/fred/series and /fred/series/observations.
The API key goes only into request params; http.fetch redacts it from logs and errors, and
every `source_url` returned here is the public series page, which carries no key.
"""

from __future__ import annotations

import re
from datetime import date

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from market_research_mcp import http
from market_research_mcp.config import require
from market_research_mcp.errors import BadInput, NotFound, ProviderDown
from market_research_mcp.models import Sourced

__all__ = ["FredObservation", "FredSeries", "get_macro_series", "register"]

SOURCE = "FRED, Federal Reserve Bank of St. Louis"
API_BASE = "https://api.stlouisfed.org/fred"
SERIES_PAGE = "https://fred.stlouisfed.org/series/{id}"
CACHE_NS = "fred"
NOTE = "Retrieved from FRED; some series are third-party copyrighted."

MAX_SERIES = 10
MAX_LIMIT = 10_000
_ID_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


class FredObservation(BaseModel):
    date: date
    value: float | None


class FredSeries(Sourced):
    id: str
    title: str
    units: str
    frequency: str
    last_updated: str
    observations: list[FredObservation]
    limit: int
    truncated: bool = False


def _validate(series_ids: list[str], start: str | None, limit: int) -> list[str]:
    if not series_ids:
        raise BadInput("give at least one FRED series id, for example DGS10.")
    if len(series_ids) > MAX_SERIES:
        raise BadInput(f"at most {MAX_SERIES} series per call (got {len(series_ids)}).")
    ids: list[str] = []
    for raw in series_ids:
        sid = str(raw).strip().upper()
        if not _ID_RE.match(sid):
            raise BadInput(f"{raw!r} is not a valid FRED series id (letters, digits, _).")
        if sid not in ids:
            ids.append(sid)
    if start is not None:
        try:
            date.fromisoformat(start)
        except ValueError:
            raise BadInput(f"start must be YYYY-MM-DD (got {start!r}).") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise BadInput(f"limit must be between 1 and {MAX_LIMIT} (got {limit}).")
    return ids


def _value(raw: object) -> float | None:
    if raw is None or raw == ".":
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _get(path: str, sid: str, params: dict[str, str | int], key: str) -> http.FetchResult:
    cache_key = f"{path}:" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    try:
        return await http.fetch(
            "fred",
            f"{API_BASE}/{path}",
            params={**params, "api_key": key, "file_type": "json"},
            cache_ns=CACHE_NS,
            cache_key=cache_key,
        )
    except ProviderDown as exc:
        # FRED answers an unknown series id with HTTP 400 and an error_message body.
        if exc.detail == "HTTP 400":
            raise NotFound(f"FRED series {sid}", "FRED") from None
        raise
    except NotFound:
        raise NotFound(f"FRED series {sid}", "FRED") from None


async def _one(sid: str, start: str | None, limit: int, key: str) -> FredSeries:
    meta_res = await _get("series", sid, {"series_id": sid}, key)
    obs_params: dict[str, str | int] = {"series_id": sid, "sort_order": "desc", "limit": limit}
    if start is not None:
        obs_params["observation_start"] = start
    obs_res = await _get("series/observations", sid, obs_params, key)

    try:
        seriess = meta_res.data["seriess"]
        if not seriess:
            raise NotFound(f"FRED series {sid}", "FRED")
        meta = seriess[0]
        observations = [
            FredObservation(date=date.fromisoformat(o["date"]), value=_value(o.get("value")))
            for o in obs_res.data["observations"]
        ]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProviderDown("FRED", "unexpected response format") from None

    observations.sort(key=lambda o: o.date)  # newest last
    total = obs_res.data.get("count")
    truncated = len(observations) > limit or (isinstance(total, int) and total > limit)
    observations = observations[-limit:]

    return FredSeries(
        source=SOURCE,
        source_url=SERIES_PAGE.format(id=sid),
        retrieved_at=obs_res.fetched_at,
        as_of=observations[-1].date if observations else None,
        stale=meta_res.stale or obs_res.stale,
        notes=[NOTE],
        id=sid,
        title=str(meta.get("title", "")),
        units=str(meta.get("units", "")),
        frequency=str(meta.get("frequency", "")),
        last_updated=str(meta.get("last_updated", "")),
        observations=observations,
        limit=limit,
        truncated=truncated,
    )


async def get_macro_series(
    series_ids: list[str], start: str | None = None, limit: int = 120
) -> list[FredSeries]:
    """Fetch FRED macro time series (e.g. DGS10, CPIAUCSL, UNRATE, FEDFUNDS).

    Returns, per series: title, units, frequency, last_updated and observations
    (date, value; missing values are null) oldest to newest, the last `limit` points
    on or after `start` (YYYY-MM-DD). `as_of` is the date of the last observation.
    Up to 10 series per call. Source: FRED, Federal Reserve Bank of St. Louis.
    """
    ids = _validate(series_ids, start, limit)
    key = require("FRED_API_KEY")
    return [await _one(sid, start, limit, key) for sid in ids]


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_macro_series)
