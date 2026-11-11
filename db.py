"""Tiny SQLite persistence layer for dashboard snapshots."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "marketlens.db"

_lock = threading.Lock()
_initialised = False


def init_db() -> None:
    global _initialised
    with _lock:
        if _initialised:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fetched_at TEXT NOT NULL,
                    source_updated_at TEXT,
                    stock_count INTEGER NOT NULL,
                    sector_count INTEGER NOT NULL,
                    partial INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.commit()
        _initialised = True


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def save_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist a snapshot and return it with its database id attached."""
    init_db()
    payload = dict(snapshot)
    payload.pop("id", None)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stats = payload.get("stats", {})

    with _lock, _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO snapshots
                (fetched_at, source_updated_at, stock_count, sector_count, partial, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("fetchedAt"),
                payload.get("sourceUpdatedAt"),
                int(stats.get("stockCount") or 0),
                int(stats.get("sectorCount") or 0),
                1 if payload.get("partial") else 0,
                encoded,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        connection.execute(
            """
            DELETE FROM snapshots
            WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 25)
            """
        )
        connection.commit()

    saved = dict(payload)
    saved["id"] = snapshot_id
    return saved


def get_latest_snapshot() -> dict[str, Any] | None:
    init_db()
    with _lock, _connect() as connection:
        row = connection.execute(
            "SELECT id, payload FROM snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    payload["id"] = row["id"]
    return payload


def get_history(limit: int = 12) -> list[dict[str, Any]]:
    init_db()
    limit = max(1, min(limit, 50))
    with _lock, _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, fetched_at, source_updated_at, stock_count, sector_count, partial
            FROM snapshots
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "fetchedAt": row["fetched_at"],
            "sourceUpdatedAt": row["source_updated_at"],
            "stockCount": row["stock_count"],
            "sectorCount": row["sector_count"],
            "partial": bool(row["partial"]),
        }
        for row in rows
    ]
