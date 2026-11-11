"""Export the current local snapshot as the bundled Render universe.

Run the app locally with live MarketLens access, then:

    python scripts/build_universe.py

The resulting ``universe.json`` contains the fixed ticker/sector mapping that
Render uses when MarketLens blocks cloud IPs. Prices in that file are only a
baseline; every Render sync replaces them with fresh Yahoo Finance quotes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

FIELDS = [
    "ticker",
    "name",
    "sector",
    "subSector",
    "closePrice",
    "previousClose",
    "pchange",
    "marketCap",
    "openPrice",
    "dayHigh",
    "dayLow",
    "week52High",
    "week52Low",
    "volume",
    "peRatio",
    "pbRatio",
    "divYield",
    "oneMonthReturn",
    "oneYearReturn",
    "lastUpdated",
    "priceSource",
]


async def export(url: str, output: Path) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f"{url.rstrip('/')}/api/dashboard")
        response.raise_for_status()
        payload = response.json()

    if not payload.get("hasData"):
        raise SystemExit("No local snapshot yet. Run a sync in the local dashboard first.")

    stocks = []
    for raw in payload.get("stocks", []):
        row = {field: raw.get(field) for field in FIELDS}
        if row.get("ticker") and row.get("sector"):
            stocks.append(row)

    if not stocks:
        raise SystemExit("The local snapshot returned no stocks.")

    universe = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "MarketLens sector mapping; Yahoo Finance prices at generation time",
        "stockCount": len(stocks),
        "stocks": stocks,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(universe, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote {len(stocks):,} stocks to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export local dashboard data to universe.json")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Local dashboard base URL")
    parser.add_argument(
        "--output",
        default="universe.json",
        help="Output path (default: ./universe.json)",
    )
    args = parser.parse_args()
    asyncio.run(export(args.url, Path(args.output)))


if __name__ == "__main__":
    main()
