"""SQLite-backed ledger for a migration task."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

STATE_PENDING = "pending"
STATE_DECIDED = "decided"    # decision committed, publish/finalize may be incomplete
STATE_TERMINAL = "terminal"  # final, never touched again

ACTION_PUBLISH = "publish"  # copy bytes + metadata to destination
ACTION_KEEP = "keep"        # leave destination bytes/metadata untouched

RESULT_COPIED = "COPIED"
RESULT_DUPLICATE_SKIP = "DUPLICATE_SKIP"
RESULT_CONFLICT_SKIP = "CONFLICT_SKIP"
RESULT_OVERWRITTEN = "OVERWRITTEN"

_SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE items (
    seq INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE,
    src_path TEXT NOT NULL,
    dst_rel TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    state TEXT NOT NULL,
    action TEXT,
    result TEXT,
    src_sha256 TEXT,
    publish_token TEXT
);
CREATE TABLE dst_state (
    path TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL
);
"""

_ITEM_COLS = "seq, item_id, src_path, dst_rel, metadata_json, state, action, result, src_sha256, publish_token"


@dataclass(frozen=True)
class ItemRow:
    seq: int
    item_id: str
    src_path: str
    dst_rel: str
    metadata_json: str
    state: str
    action: Optional[str]
    result: Optional[str]
    src_sha256: Optional[str]
    publish_token: Optional[str]


class Store:
    """Owns the SQLite ledger inside a workdir."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self.db_path = self.workdir / "task.db"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def exists(self) -> bool:
        return self.db_path.exists()

    def create_task(self, destination: str, policy: str, manifest: list[dict]) -> None:
        if self.exists():
            raise FileExistsError(f"task already exists in workdir: {self.workdir}")
        conn = self._connect()
        try:
            with conn:
                conn.executescript(_SCHEMA)
                conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("destination", destination))
                conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("policy", policy))
                rows = [
                    (seq, item["item_id"], item["src_path"], item["dst_rel"],
                     json.dumps(item["metadata"], sort_keys=True), STATE_PENDING)
                    for seq, item in enumerate(manifest)
                ]
                conn.executemany(
                    "INSERT INTO items(seq, item_id, src_path, dst_rel, metadata_json, state)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
        finally:
            conn.close()

    def task_meta(self) -> dict:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            return {row["key"]: row["value"] for row in rows}
        finally:
            conn.close()

    def items(self) -> list[ItemRow]:
        conn = self._connect()
        try:
            rows = conn.execute(f"SELECT {_ITEM_COLS} FROM items ORDER BY seq").fetchall()
            return [ItemRow(**dict(row)) for row in rows]
        finally:
            conn.close()

    def record_decision(self, seq: int, action: str, result: str,
                        src_sha256: str, publish_token: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE items SET state = ?, action = ?, result = ?,"
                    " src_sha256 = ?, publish_token = ? WHERE seq = ?",
                    (STATE_DECIDED, action, result, src_sha256, publish_token, seq),
                )
        finally:
            conn.close()

    def record_finalize(self, seq: int, dst_rel: str, metadata_json: str,
                        published: bool) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE items SET state = ? WHERE seq = ?",
                    (STATE_TERMINAL, seq),
                )
                if published:
                    conn.execute(
                        "INSERT INTO dst_state(path, metadata_json) VALUES (?, ?)"
                        " ON CONFLICT(path) DO UPDATE SET metadata_json = excluded.metadata_json",
                        (dst_rel, metadata_json),
                    )
        finally:
            conn.close()

    def destination_metadata(self) -> dict[str, dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT path, metadata_json FROM dst_state").fetchall()
            return {row["path"]: json.loads(row["metadata_json"]) for row in rows}
        finally:
            conn.close()
