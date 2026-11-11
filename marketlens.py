"""
MarketLens (NSE India) data client.

MarketLens supplies the stock universe, sector mapping and company metadata.
Yahoo Finance supplies the latest NSE price and previous close used to compute
the 1-day change.

The public MarketLens front-end is a Next.js app backed by a small JSON API.
This module wraps the endpoints needed to build a complete snapshot:

    GET /api/sectors
    GET /api/sectors/{sector}/stocks
    GET /api/stocks (fallback)

The API is public but Akamai-fronted, so requests are sent with browser-like
headers and are retried with a short backoff.  No undocumented authentication
is required at the time of writing.
"""

from __future__ import annotations

import asyncio
import math
import random
import statistics
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import quote

import httpx

from yahoo import YahooFinanceClient

DEFAULT_BASE_URL = "https://marketlens.nseindia.com"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://marketlens.nseindia.com/",
    "Origin": "https://marketlens.nseindia.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class MarketLensError(RuntimeError):
    """Raised when the upstream MarketLens API cannot be read."""


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _normalise_stock(raw: dict[str, Any], requested_sector: str, fetched_at: str) -> dict[str, Any] | None:
    ticker = _text(raw.get("ticker")) or _text(raw.get("id"))
    if not ticker:
        return None

    ticker = ticker.upper()
    sector = _text(raw.get("sector")) or requested_sector or "Unclassified"
    last_updated = _text(raw.get("lastUpdated")) or fetched_at

    return {
        "ticker": ticker,
        "name": _text(raw.get("name")) or ticker,
        "sector": sector,
        "subSector": _text(raw.get("subSector")) or "",
        "closePrice": _number(raw.get("closePrice")),
        "pchange": _number(raw.get("pchange")) if raw.get("pchange") is not None else _number(raw.get("oneDayChange")),
        "previousClose": _number(raw.get("previousClose")),
        "openPrice": _number(raw.get("openPrice")),
        "dayHigh": _number(raw.get("dayHigh")),
        "dayLow": _number(raw.get("dayLow")),
        "week52High": _number(raw.get("week52High")),
        "week52Low": _number(raw.get("week52Low")),
        "marketCap": _number(raw.get("marketCap")),
        "volume": _integer(raw.get("volume")),
        "peRatio": _number(raw.get("peRatio")),
        "pbRatio": _number(raw.get("pbRatio")),
        "divYield": _number(raw.get("divYield")),
        "oneMonthReturn": _number(raw.get("oneMonthReturn")),
        "oneYearReturn": _number(raw.get("oneYearReturn")),
        "lastUpdated": last_updated,
    }


def _mean(values: Iterable[float]) -> float | None:
    values = [value for value in values if value is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _median(values: Iterable[float]) -> float | None:
    values = [value for value in values if value is not None]
    if not values:
        return None
    return round(statistics.median(values), 4)


def _build_snapshot(
    stocks: list[dict[str, Any]],
    source_sectors: list[dict[str, Any]],
    fetched_at: str,
    duration_seconds: float,
    errors: list[dict[str, str]],
    price_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # MarketLens's sector pages should be disjoint, but de-duplicate defensively.
    unique: dict[str, dict[str, Any]] = {}
    for stock in stocks:
        unique.setdefault(stock["ticker"], stock)

    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item["marketCap"] is None,
            -(item["marketCap"] or 0),
            item["ticker"],
        ),
    )

    source_counts = {item.get("name"): item.get("stocks") for item in source_sectors if item.get("name")}
    source_performance = {item.get("name"): item.get("performance") for item in source_sectors if item.get("name")}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for stock in ordered:
        grouped.setdefault(stock["sector"], []).append(stock)

    sectors: list[dict[str, Any]] = []
    for name, members in grouped.items():
        caps = [member["marketCap"] for member in members if member["marketCap"] is not None]
        changes = [member["pchange"] for member in members if member["pchange"] is not None]
        weighted_pairs = [
            (member["marketCap"], member["pchange"])
            for member in members
            if member["marketCap"] and member["pchange"] is not None and (1 + member["pchange"] / 100) > 0
        ]
        # Aggregate market-cap return: compare the sector's current total market
        # cap with its implied previous total market cap. This is the market-cap
        # weighted overall percentage change, not a simple average of percentages.
        current_cap = sum(cap for cap, _ in weighted_pairs)
        previous_cap = sum(cap / (1 + change / 100) for cap, change in weighted_pairs)
        weighted_change = (
            round((current_cap / previous_cap - 1) * 100, 4)
            if previous_cap
            else None
        )
        market_cap_change = (
            round(current_cap - previous_cap, 2)
            if previous_cap
            else None
        )
        top = max(members, key=lambda item: item["marketCap"] or 0)
        sectors.append(
            {
                "name": name,
                "count": len(members),
                "sourceCount": source_counts.get(name),
                "marketCap": round(sum(caps), 2) if caps else None,
                "avgChange": _mean(changes),
                "weightedChange": weighted_change,
                "marketCapChange": market_cap_change,
                "medianChange": _median(changes),
                "topStock": top["ticker"],
                "sourcePerformance": _number(source_performance.get(name)),
            }
        )

    sectors.sort(key=lambda item: (-(item["marketCap"] or 0), item["name"]))

    changes = [stock["pchange"] for stock in ordered if stock["pchange"] is not None]
    caps = [stock["marketCap"] for stock in ordered if stock["marketCap"] is not None]
    weighted_pairs = [
        (stock["marketCap"], stock["pchange"])
        for stock in ordered
        if stock["marketCap"] and stock["pchange"] is not None and (1 + stock["pchange"] / 100) > 0
    ]
    current_cap = sum(cap for cap, _ in weighted_pairs)
    previous_cap = sum(cap / (1 + change / 100) for cap, change in weighted_pairs)
    weighted_change = (
        round((current_cap / previous_cap - 1) * 100, 4)
        if previous_cap
        else None
    )
    advancers = sum(1 for change in changes if change > 0)
    decliners = sum(1 for change in changes if change < 0)
    unchanged = sum(1 for change in changes if change == 0)
    source_updated = max((stock["lastUpdated"] for stock in ordered), default=fetched_at)

    return {
        "fetchedAt": fetched_at,
        "sourceUpdatedAt": source_updated,
        "durationSeconds": round(duration_seconds, 2),
        "partial": bool(errors),
        "errors": errors,
        "prices": price_stats or {},
        "stocks": ordered,
        "sectors": sectors,
        "stats": {
            "stockCount": len(ordered),
            "sectorCount": len(sectors),
            "totalMarketCap": round(sum(caps), 2) if caps else None,
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "avgChange": _mean(changes),
            "weightedChange": weighted_change,
            "marketCapChange": round(current_cap - previous_cap, 2) if previous_cap else None,
            "medianChange": _median(changes),
        },
    }


class MarketLensClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        concurrency: int = 6,
        timeout: float = 35.0,
        retries: int = 3,
        proxy: str | None = None,
        yahoo_concurrency: int = 6,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.retries = max(1, retries)
        self.proxy = proxy
        self.price_client = YahooFinanceClient(concurrency=yahoo_concurrency)

    def _client(self) -> httpx.AsyncClient:
        headers = dict(BROWSER_HEADERS)
        headers["Referer"] = f"{self.base_url}/"
        headers["Origin"] = self.base_url
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            follow_redirects=True,
            proxy=self.proxy,
        )

    async def _warm_up(self, client: httpx.AsyncClient) -> None:
        """Fetch the HTML shell once so Akamai can issue its cookies."""
        try:
            await client.get("/")
        except httpx.HTTPError:
            pass

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self.retries):
            try:
                response = await client.get(path, params=params)
                if response.status_code in (403, 429, 500, 502, 503, 504):
                    raise MarketLensError(f"MarketLens returned HTTP {response.status_code} for {path}")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    message = payload.get("error") if isinstance(payload, dict) else None
                    raise MarketLensError(str(message or f"Unexpected response from {path}"))
                return payload
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt < self.retries - 1:
                    if isinstance(exc, MarketLensError) and "403" in str(exc):
                        await self._warm_up(client)
                    await asyncio.sleep((1.25 * (2**attempt)) + random.uniform(0.05, 0.45))

        raise MarketLensError(f"Could not read {path}: {last_error}") from last_error

    async def fetch_sectors(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        payload = await self._get_json(client, "/api/sectors")
        data = payload.get("data")
        if not isinstance(data, list):
            raise MarketLensError("The sector list from MarketLens was not a list")
        sectors = [item for item in data if isinstance(item, dict) and _text(item.get("name"))]
        if not sectors:
            raise MarketLensError("MarketLens returned an empty sector list")
        return sectors

    async def fetch_sector_stocks(
        self, client: httpx.AsyncClient, sector_name: str
    ) -> list[dict[str, Any]]:
        encoded = quote(sector_name, safe="")
        payload = await self._get_json(
            client,
            f"/api/sectors/{encoded}/stocks",
            params={"sortBy": "marketCap", "sortOrder": "desc"},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise MarketLensError(f"Unexpected stock list for sector {sector_name}")
        return [item for item in data if isinstance(item, dict)]

    async def _fetch_sector_with_fallback(
        self,
        client: httpx.AsyncClient,
        sector: dict[str, Any],
        semaphore: asyncio.Semaphore,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
        name = str(sector["name"])
        async with semaphore:
            try:
                raw = await self.fetch_sector_stocks(client, name)
                if raw or not sector.get("stocks"):
                    return sector, raw, None

                # Occasionally a sector page can be empty while the general stock
                # endpoint still knows the scrips. This fallback is capped at 100
                # by the upstream API, but it is better than a hole in the data.
                payload = await self._get_json(
                    client,
                    "/api/stocks",
                    params={
                        "sectors": name,
                        "limit": 100,
                        "sortBy": "marketCap",
                        "sortOrder": "desc",
                    },
                )
                data = payload.get("data", {})
                stocks = data.get("stocks") if isinstance(data, dict) else None
                if isinstance(stocks, list) and stocks:
                    return sector, [item for item in stocks if isinstance(item, dict)], "used fallback endpoint"
                return sector, [], None
            except Exception as exc:  # noqa: BLE001 - surfaced in the snapshot
                return sector, [], str(exc)

    async def fetch_all(self, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        fetched_at = started.isoformat(timespec="seconds")
        errors: list[dict[str, str]] = []

        async with self._client() as client:
            await self._warm_up(client)
            sectors = await self.fetch_sectors(client)

            if on_progress:
                result = on_progress(
                    {
                        "type": "sectors_found",
                        "total": len(sectors),
                        "message": f"Found {len(sectors)} sectors on MarketLens",
                    }
                )
                if asyncio.iscoroutine(result):
                    await result

            semaphore = asyncio.Semaphore(self.concurrency)
            tasks = [
                asyncio.create_task(self._fetch_sector_with_fallback(client, sector, semaphore))
                for sector in sectors
            ]

            all_stocks: list[dict[str, Any]] = []
            done = 0
            for task in asyncio.as_completed(tasks):
                sector, raw_stocks, warning = await task
                name = str(sector["name"])
                if warning:
                    errors.append({"sector": name, "error": warning})
                normalised = [
                    stock
                    for raw in raw_stocks
                    if (stock := _normalise_stock(raw, name, fetched_at)) is not None
                ]
                all_stocks.extend(normalised)
                done += 1

                if on_progress:
                    result = on_progress(
                        {
                            "type": "sector_done",
                            "sector": name,
                            "count": len(normalised),
                            "done": done,
                            "total": len(sectors),
                            "stocks": len(all_stocks),
                            "warning": warning,
                        }
                    )
                    if asyncio.iscoroutine(result):
                        await result

        price_stats = None
        if self.price_client:
            all_stocks, price_stats = await self.price_client.enrich_prices(all_stocks, on_progress)

        snapshot = _build_snapshot(
            stocks=all_stocks,
            source_sectors=sectors,
            fetched_at=fetched_at,
            duration_seconds=(datetime.now(timezone.utc) - started).total_seconds(),
            errors=errors,
            price_stats=price_stats,
        )
        if on_progress:
            result = on_progress(
                {
                    "type": "complete",
                    "message": f"Collected {snapshot['stats']['stockCount']} stocks",
                    "stocks": snapshot["stats"]["stockCount"],
                }
            )
            if asyncio.iscoroutine(result):
                await result
        return snapshot
