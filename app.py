"""MarketLens NSE dashboard — FastAPI application.

The app has three responsibilities:
  1. Serve the single-page dashboard in ./static
  2. Run an on-demand, background snapshot job against MarketLens
  3. Persist and export the latest snapshot (CSV)

Everything is intentionally dependency-light so it can run on a single Render
web service without a database add-on or a front-end build step.
"""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import db
from marketlens import DEFAULT_BASE_URL, MarketLensClient, fetch_static_snapshot

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
IST = timezone(timedelta(hours=5, minutes=30))

SOURCE_BASE_URL = os.getenv("MARKETLENS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
SOURCE_PROXY = os.getenv("MARKETLENS_PROXY") or None
SYNC_CONCURRENCY = max(1, int(os.getenv("SYNC_CONCURRENCY", "6")))
YAHOO_CONCURRENCY = max(1, int(os.getenv("YAHOO_CONCURRENCY", "6")))
_ON_RENDER = os.getenv("RENDER", "").strip().lower() == "true"
USE_STATIC_UNIVERSE = os.getenv(
    "USE_STATIC_UNIVERSE", "1" if _ON_RENDER else "0"
).strip().lower() in ("1", "true", "yes", "on")
STATIC_UNIVERSE_PATH = Path(os.getenv("STATIC_UNIVERSE_PATH", str(BASE_DIR / "universe.json")))
KEEP_ALIVE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
KEEP_ALIVE_ENABLED = os.getenv(
    "KEEP_ALIVE_ENABLED", "1" if KEEP_ALIVE_URL else "0"
).strip().lower() in ("1", "true", "yes", "on")
KEEP_ALIVE_INTERVAL = max(60, int(os.getenv("KEEP_ALIVE_INTERVAL_SECONDS", "600")))
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("marketlens.dashboard")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_job() -> dict[str, Any]:
    return {
        "status": "idle",
        "phase": "idle",
        "startedAt": None,
        "finishedAt": None,
        "progress": {"done": 0, "total": 0, "stocks": 0},
        "message": "Ready to fetch a fresh snapshot.",
        "logs": [],
        "error": None,
        "snapshotId": None,
        "durationSeconds": None,
    }


class DashboardState:
    def __init__(self) -> None:
        self.snapshot: dict[str, Any] | None = None
        self.job: dict[str, Any] = _new_job()
        self.job_lock = threading.Lock()
        self.sync_task: asyncio.Task[None] | None = None
        self.started_at = time.time()


state = DashboardState()


def _job_snapshot() -> dict[str, Any]:
    with state.job_lock:
        return copy.deepcopy(state.job)


def _update_job(**fields: Any) -> None:
    with state.job_lock:
        state.job.update(fields)


def _append_log(text: str, level: str = "info") -> None:
    entry = {"ts": _now_iso(), "level": level, "text": text}
    with state.job_lock:
        state.job["logs"].append(entry)
        if len(state.job["logs"]) > 300:
            state.job["logs"] = state.job["logs"][-300:]


def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Shape the response used by the dashboard (full data, no internal noise)."""
    return {
        "hasData": True,
        "snapshot": {
            "id": snapshot.get("id"),
            "fetchedAt": snapshot.get("fetchedAt"),
            "sourceUpdatedAt": snapshot.get("sourceUpdatedAt"),
            "durationSeconds": snapshot.get("durationSeconds"),
            "partial": bool(snapshot.get("partial")),
            "errors": snapshot.get("errors", []),
            "prices": snapshot.get("prices") or {},
        },
        "stats": snapshot.get("stats", {}),
        "sectors": snapshot.get("sectors", []),
        "stocks": snapshot.get("stocks", []),
        "job": _job_snapshot(),
        "source": {"name": "MarketLens · NSE India", "url": SOURCE_BASE_URL},
    }


async def _run_sync() -> None:
    started_monotonic = time.monotonic()
    _update_job(
        status="running",
        phase="sectors",
        startedAt=_now_iso(),
        finishedAt=None,
        progress={"done": 0, "total": 0, "stocks": 0},
        message="Connecting to MarketLens…",
        error=None,
        snapshotId=None,
        durationSeconds=None,
    )
    _append_log("Starting fresh snapshot sync")

    async def on_progress(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "sectors_found":
            total = int(event.get("total") or 0)
            _update_job(
                phase="sectors",
                progress={"done": 0, "total": total, "stocks": 0},
                message=f"Fetching {total} sectors…",
            )
            _append_log(f"Found {total} sectors")
        elif event_type == "universe_loaded":
            total = int(event.get("stocks") or 0)
            _update_job(
                phase="universe",
                progress={"done": 0, "total": 0, "stocks": total},
                message=str(event.get("message") or "Loaded bundled sector universe"),
            )
            _append_log(str(event.get("message") or "Loaded bundled sector universe"))
        elif event_type == "sector_done":
            _update_job(
                phase="sectors",
                progress={
                    "done": int(event.get("done") or 0),
                    "total": int(event.get("total") or 0),
                    "stocks": int(event.get("stocks") or 0),
                },
                message=(
                    f"Fetched {event.get('sector')} · "
                    f"{event.get('done')}/{event.get('total')} sectors"
                ),
            )
            warning = event.get("warning")
            log_line = f"{event.get('sector')} — {event.get('count')} stocks"
            if warning:
                log_line += f" ({warning})"
            _append_log(log_line, level="warn" if warning else "info")
        elif event_type == "prices_started":
            total = int(event.get("total") or 0)
            _update_job(
                phase="prices",
                progress={"done": 0, "total": total, "stocks": total},
                message=str(event.get("message") or "Fetching Yahoo Finance prices…"),
            )
            _append_log(f"Fetching Yahoo Finance prices for {total:,} stocks")
        elif event_type == "prices_progress":
            _update_job(
                phase="prices",
                progress={
                    "done": int(event.get("done") or 0),
                    "total": int(event.get("total") or 0),
                    "stocks": int(event.get("total") or 0),
                },
                message=str(event.get("message") or "Fetching Yahoo Finance prices…"),
            )
        elif event_type == "prices_done":
            matched = int(event.get("matched") or 0)
            fallback = int(event.get("fallback") or 0)
            _update_job(
                phase="prices",
                message=str(event.get("message") or "Yahoo Finance prices ready"),
            )
            _append_log(
                f"Yahoo Finance: {matched:,} prices matched"
                + (f", {fallback:,} MarketLens fallback" if fallback else ""),
                level="warn" if fallback else "info",
            )
        elif event_type == "complete":
            _append_log(str(event.get("message") or "MarketLens sync complete"))

    try:
        if USE_STATIC_UNIVERSE:
            _append_log(f"Using bundled sector universe: {STATIC_UNIVERSE_PATH.name}")
            snapshot = await fetch_static_snapshot(
                STATIC_UNIVERSE_PATH,
                on_progress=on_progress,
                yahoo_concurrency=YAHOO_CONCURRENCY,
            )
        else:
            client = MarketLensClient(
                base_url=SOURCE_BASE_URL,
                concurrency=SYNC_CONCURRENCY,
                proxy=SOURCE_PROXY,
                yahoo_concurrency=YAHOO_CONCURRENCY,
            )
            snapshot = await client.fetch_all(on_progress=on_progress)
        if not snapshot.get("stocks"):
            raise RuntimeError("No stocks were returned")

        saved = snapshot
        try:
            saved = await asyncio.to_thread(db.save_snapshot, snapshot)
            _append_log(f"Snapshot #{saved.get('id')} persisted to SQLite")
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Could not persist snapshot: %s", exc)
            _append_log("Snapshot could not be saved to disk; serving from memory", "warn")

        state.snapshot = saved
        duration = round(time.monotonic() - started_monotonic, 2)
        stock_count = int(saved.get("stats", {}).get("stockCount") or 0)
        sector_count = int(saved.get("stats", {}).get("sectorCount") or 0)
        partial = bool(saved.get("partial"))
        prices = saved.get("prices") or {}
        price_note = ""
        if prices.get("requested"):
            price_note = f" · Yahoo {prices.get('matched', 0):,}/{prices.get('requested', 0):,} prices"
            if prices.get("fallback"):
                price_note += f" ({prices['fallback']:,} fallback)"

        _update_job(
            status="success",
            phase="complete",
            finishedAt=_now_iso(),
            message=(
                f"Snapshot ready · {stock_count:,} stocks across {sector_count} sectors"
                + price_note
                + (" (partial)" if partial else "")
            ),
            snapshotId=saved.get("id"),
            durationSeconds=duration,
            progress={
                "done": sector_count,
                "total": sector_count,
                "stocks": stock_count,
            },
        )
        _append_log(
            f"Sync complete in {duration:.1f}s · {stock_count:,} unique stocks"
            + (f" · {len(saved.get('errors', []))} sector warnings" if partial else ""),
            level="warn" if partial else "success",
        )
        logger.info("Sync completed: %s stocks in %.1fs", stock_count, duration)
    except Exception as exc:  # noqa: BLE001 - shown in the UI
        duration = round(time.monotonic() - started_monotonic, 2)
        message = str(exc)
        _update_job(
            status="error",
            phase="error",
            finishedAt=_now_iso(),
            message="Sync failed. See the log for details.",
            error=message,
            durationSeconds=duration,
        )
        _append_log(f"Sync failed after {duration:.1f}s: {message}", level="error")
        logger.exception("MarketLens sync failed")


async def _keep_alive_loop() -> None:
    """Ping our own public URL so a free Render web service stays awake.

    Render's free plan spins a web service down after 15 minutes without
    inbound traffic. A request to the public onrender.com URL is inbound, so
    pinging /api/health every 10 minutes keeps the service warm while the
    process is alive. If the process has already been stopped, it cannot wake
    itself; an external monitor or a paid instance is needed for that.
    """
    await asyncio.sleep(60)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        while True:
            try:
                response = await client.get(f"{KEEP_ALIVE_URL}/api/health")
                if response.status_code == 200:
                    logger.info("Keep-alive ping OK (%s)", KEEP_ALIVE_URL)
                else:
                    logger.warning("Keep-alive ping returned HTTP %s", response.status_code)
            except Exception as exc:  # noqa: BLE001 - retry next interval
                logger.warning("Keep-alive ping failed: %s", exc)
            await asyncio.sleep(KEEP_ALIVE_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.init_db()
        state.snapshot = db.get_latest_snapshot()
        if state.snapshot:
            count = state.snapshot.get("stats", {}).get("stockCount", 0)
            _append_log(f"Loaded previous snapshot #{state.snapshot.get('id')} · {count:,} stocks")
            logger.info("Loaded snapshot %s with %s stocks", state.snapshot.get("id"), count)
    except Exception as exc:  # noqa: BLE001 - app can still sync in memory
        logger.warning("Could not open the local database: %s", exc)

    keep_alive_task = None
    if KEEP_ALIVE_ENABLED and KEEP_ALIVE_URL:
        keep_alive_task = asyncio.create_task(_keep_alive_loop())
        logger.info(
            "Keep-alive enabled: pinging %s every %ss",
            KEEP_ALIVE_URL,
            KEEP_ALIVE_INTERVAL,
        )
    else:
        logger.info("Keep-alive disabled")

    logger.info("MarketLens dashboard ready (source: %s)", SOURCE_BASE_URL)
    yield

    if keep_alive_task is not None:
        keep_alive_task.cancel()
        try:
            await keep_alive_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="MarketLens NSE Dashboard",
    description="On-demand NSE stock snapshot dashboard powered by MarketLens.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=1_000)


@app.middleware("http")
async def optional_password(request: Request, call_next):
    if (
        DASHBOARD_PASSWORD
        and request.url.path.startswith("/api/")
        and request.url.path not in ("/api/health",)
    ):
        supplied = request.headers.get("x-dashboard-key") or request.query_params.get("key")
        if supplied != DASHBOARD_PASSWORD:
            return JSONResponse(status_code=401, content={"detail": "Dashboard password required"})
    return await call_next(request)


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.get("/api/health")
@app.get("/health")
async def health() -> dict[str, Any]:
    snapshot = state.snapshot
    return {
        "status": "ok",
        "hasData": snapshot is not None,
        "stockCount": (snapshot or {}).get("stats", {}).get("stockCount"),
        "source": SOURCE_BASE_URL,
        "uptimeSeconds": round(time.time() - state.started_at, 1),
    }


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    snapshot = state.snapshot
    if snapshot is None:
        return {
            "hasData": False,
            "job": _job_snapshot(),
            "source": {"name": "MarketLens · NSE India", "url": SOURCE_BASE_URL},
        }
    return _public_snapshot(snapshot)


@app.post("/api/sync")
async def start_sync() -> dict[str, Any]:
    with state.job_lock:
        if state.job["status"] == "running":
            raise HTTPException(status_code=409, detail="A sync is already running")
        state.job = _new_job()
        state.job["status"] = "running"
        state.job["startedAt"] = _now_iso()
        state.job["message"] = "Starting…"

    state.sync_task = asyncio.create_task(_run_sync())
    return {"ok": True, "job": _job_snapshot()}


@app.get("/api/sync/status")
async def sync_status() -> dict[str, Any]:
    return {"job": _job_snapshot(), "hasData": state.snapshot is not None}


@app.get("/api/history")
async def history(limit: int = 12) -> dict[str, Any]:
    try:
        rows = await asyncio.to_thread(db.get_history, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read history: %s", exc)
        rows = []
    return {"history": rows}


def _filtered_stocks(
    stocks: list[dict[str, Any]],
    search: str = "",
    sector: str = "",
    sort: str = "marketCap",
    order: str = "desc",
) -> list[dict[str, Any]]:
    rows = stocks
    if sector and sector.upper() != "ALL":
        rows = [row for row in rows if row.get("sector") == sector]
    if search:
        needle = search.strip().lower()
        rows = [
            row
            for row in rows
            if needle in str(row.get("ticker", "")).lower()
            or needle in str(row.get("name", "")).lower()
            or needle in str(row.get("sector", "")).lower()
            or needle in str(row.get("subSector", "")).lower()
        ]

    allowed = {
        "ticker",
        "name",
        "sector",
        "closePrice",
        "pchange",
        "marketCap",
        "peRatio",
        "oneMonthReturn",
        "oneYearReturn",
        "volume",
        "lastUpdated",
    }
    sort_key = sort if sort in allowed else "marketCap"
    non_null = [row for row in rows if row.get(sort_key) is not None]
    nulls = [row for row in rows if row.get(sort_key) is None]
    non_null.sort(key=lambda row: row.get(sort_key), reverse=(order != "asc"))
    return non_null + nulls


@app.get("/api/export.csv")
async def export_csv(
    search: str = "",
    sector: str = "",
    sort: str = "marketCap",
    order: str = "desc",
) -> Response:
    snapshot = state.snapshot
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No snapshot available. Run a sync first.")

    rows = _filtered_stocks(snapshot.get("stocks", []), search, sector, sort, order)
    fetched_at = snapshot.get("fetchedAt") or ""
    source_updated = snapshot.get("sourceUpdatedAt") or ""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Ticker",
            "Company",
            "Sector",
            "Sub-sector",
            "Close Price (INR)",
            "Change 1D (%)",
            "Previous Close",
            "Open",
            "Day High",
            "Day Low",
            "52W High",
            "52W Low",
            "Market Cap (INR)",
            "P/E",
            "P/B",
            "Dividend Yield (%)",
            "Volume",
            "1M Return (%)",
            "1Y Return (%)",
            "Last Updated (Source)",
            "Snapshot Fetched At",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("ticker"),
                row.get("name"),
                row.get("sector"),
                row.get("subSector"),
                row.get("closePrice"),
                row.get("pchange"),
                row.get("previousClose"),
                row.get("openPrice"),
                row.get("dayHigh"),
                row.get("dayLow"),
                row.get("week52High"),
                row.get("week52Low"),
                row.get("marketCap"),
                row.get("peRatio"),
                row.get("pbRatio"),
                row.get("divYield"),
                row.get("volume"),
                row.get("oneMonthReturn"),
                row.get("oneYearReturn"),
                row.get("lastUpdated"),
                fetched_at,
            ]
        )

    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
    filename = f"marketlens_nse_{stamp}.csv"
    content = buffer.getvalue().encode("utf-8-sig")
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Snapshot-Source-Updated": source_updated,
        "X-Snapshot-Rows": str(len(rows)),
    }
    return Response(content=content, media_type="text/csv; charset=utf-8", headers=headers)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.svg")
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
