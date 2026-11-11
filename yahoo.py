"""Yahoo Finance price enrichment for NSE stocks.

MarketLens is used for the stock universe, sector mapping and company metadata.
Prices are fetched from Yahoo Finance's public spark endpoint for the NSE
symbol (``TICKER.NS``): the latest market price plus the previous close.  This
keeps the sector mapping fixed while replacing MarketLens price/change data.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

SPARK_URLS = (
    "https://query1.finance.yahoo.com/v7/finance/spark",
    "https://query2.finance.yahoo.com/v7/finance/spark",
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BATCH_SIZE = 20

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class YahooFinanceError(RuntimeError):
    """Raised when Yahoo Finance cannot return any usable prices."""


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


def yahoo_symbol(ticker: str) -> str:
    value = str(ticker).strip().upper()
    if value.endswith(".NS") or value.endswith(".BO"):
        return value
    return f"{value}.NS"


def _timestamp_iso(value: Any) -> str | None:
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


class YahooFinanceClient:
    def __init__(
        self,
        concurrency: int = 6,
        timeout: float = 30.0,
        retries: int = 3,
        batch_size: int = MAX_BATCH_SIZE,
    ) -> None:
        self.concurrency = max(1, concurrency)
        self.timeout = timeout
        self.retries = max(1, retries)
        self.batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=HEADERS,
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=self.concurrency + 2, max_keepalive_connections=self.concurrency + 2),
        )

    async def _emit(self, callback: ProgressCallback | None, event: dict[str, Any]) -> None:
        if callback is None:
            return
        result = callback(event)
        if asyncio.iscoroutine(result):
            await result

    async def _fetch_batch(
        self,
        client: httpx.AsyncClient,
        symbols: list[str],
        errors: list[dict[str, str]],
    ) -> dict[str, dict[str, Any]]:
        params = {
            "symbols": ",".join(symbols),
            "range": "2d",
            "interval": "1d",
        }
        last_error = "unknown error"

        for attempt in range(self.retries):
            url = SPARK_URLS[attempt % len(SPARK_URLS)]
            try:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    return self._parse(response.json())
                last_error = f"HTTP {response.status_code}"
                if response.status_code in (400, 401, 403, 404):
                    break
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = str(exc)
            if attempt < self.retries - 1:
                await asyncio.sleep((1.2 * (2**attempt)) + random.uniform(0.05, 0.35))

        errors.append({"symbols": f"{symbols[0]}…{symbols[-1]}", "error": last_error})
        return {}

    @staticmethod
    def _parse(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result = payload.get("spark", {}).get("result")
        if not isinstance(result, list):
            return {}

        quotes: dict[str, dict[str, Any]] = {}
        for item in result:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            response = item.get("response")
            entry: dict[str, Any] | None = None
            if isinstance(response, list) and response and isinstance(response[0], dict):
                entry = response[0]
            elif isinstance(response, dict):
                entry = response
            if not symbol or entry is None:
                continue

            meta = entry.get("meta") or {}
            currency = str(meta.get("currency") or "INR").upper()
            if currency not in ("INR", "INR."):
                continue

            today = _number(meta.get("regularMarketPrice"))
            previous = _number(meta.get("chartPreviousClose"))

            if today is None or previous is None:
                quote_series = entry.get("indicators", {}).get("quote", [])
                closes: list[float] = []
                if isinstance(quote_series, list) and quote_series:
                    raw_closes = quote_series[0].get("close", []) if isinstance(quote_series[0], dict) else []
                    if isinstance(raw_closes, list):
                        for value in raw_closes:
                            number = _number(value)
                            if number is not None and number > 0:
                                closes.append(number)
                if len(closes) >= 2:
                    today = closes[-1]
                    previous = closes[-2]
                elif len(closes) == 1 and today is None:
                    today = closes[-1]

            if today is None or previous is None or today <= 0 or previous <= 0:
                continue

            quotes[symbol] = {
                "today": today,
                "previous": previous,
                "changePercent": round((today / previous - 1) * 100, 4),
                "updatedAt": _timestamp_iso(meta.get("regularMarketTime")),
            }
        return quotes

    async def enrich_prices(
        self,
        stocks: list[dict[str, Any]],
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not stocks:
            return stocks, {
                "source": "Yahoo Finance",
                "requested": 0,
                "matched": 0,
                "fallback": 0,
                "errors": [],
                "fallbackTickers": [],
            }

        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for stock in stocks:
            by_symbol.setdefault(yahoo_symbol(stock["ticker"]), []).append(stock)

        symbols = list(by_symbol)
        batches = [symbols[index : index + self.batch_size] for index in range(0, len(symbols), self.batch_size)]
        total = len(stocks)
        started = time.monotonic()
        errors: list[dict[str, str]] = []
        quotes: dict[str, dict[str, Any]] = {}
        processed = 0
        matched_symbols = 0

        await self._emit(
            on_progress,
            {
                "type": "prices_started",
                "total": total,
                "message": f"Fetching Yahoo Finance prices for {total:,} stocks…",
            },
        )

        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_batch(batch: list[str]) -> None:
            nonlocal processed, matched_symbols
            async with semaphore:
                batch_quotes = await self._fetch_batch(client, batch, errors)
                quotes.update(batch_quotes)
                matched_symbols += sum(len(by_symbol[symbol]) for symbol in batch_quotes)
                processed += sum(len(by_symbol[symbol]) for symbol in batch)
                await self._emit(
                    on_progress,
                    {
                        "type": "prices_progress",
                        "done": processed,
                        "total": total,
                        "matched": matched_symbols,
                        "message": f"Yahoo Finance prices · {processed:,}/{total:,}",
                    },
                )

        async with self._client() as client:
            tasks = [asyncio.create_task(run_batch(batch)) for batch in batches]
            for task in asyncio.as_completed(tasks):
                await task

        fallback_tickers: list[str] = []
        matched = 0
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for stock in stocks:
            symbol = yahoo_symbol(stock["ticker"])
            quote = quotes.get(symbol)
            if quote is None:
                stock["priceSource"] = "MarketLens fallback"
                fallback_tickers.append(stock["ticker"])
                continue

            ml_close = _number(stock.get("closePrice"))
            ml_cap = _number(stock.get("marketCap"))
            today = float(quote["today"])
            previous = float(quote["previous"])

            stock["closePrice"] = round(today, 4)
            stock["previousClose"] = round(previous, 4)
            stock["pchange"] = quote["changePercent"]
            stock["priceSource"] = "Yahoo Finance"
            stock["priceUpdatedAt"] = quote.get("updatedAt") or now_iso
            stock["yahooSymbol"] = symbol

            # Keep the same implied share count, but mark the company to the
            # Yahoo market price. This keeps sector weights consistent with the
            # corrected prices.
            if ml_close and ml_cap and ml_close > 0:
                shares = ml_cap / ml_close
                stock["marketCap"] = round(shares * today, 2)

            matched += 1

        if matched == 0:
            raise YahooFinanceError("Yahoo Finance returned no usable prices")

        stats = {
            "source": "Yahoo Finance",
            "requested": total,
            "matched": matched,
            "fallback": len(fallback_tickers),
            "batches": len(batches),
            "errors": errors[-10:],
            "fallbackTickers": fallback_tickers[:50],
            "durationSeconds": round(time.monotonic() - started, 2),
        }

        await self._emit(
            on_progress,
            {
                "type": "prices_done",
                "total": total,
                "matched": matched,
                "fallback": len(fallback_tickers),
                "message": f"Yahoo Finance prices ready · {matched:,}/{total:,} matched",
            },
        )
        return stocks, stats
