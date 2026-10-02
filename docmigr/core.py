"""Core implementation: SQLite ledger + atomic file publish.

Durability model (process-kill safe, no power-loss guarantees):
- The ledger (SQLite, synchronous=FULL) is the single source of truth for
  per-item state and for the metadata "saved at" each destination path.
- File bytes are published by writing a fully-fsynced temp file in the
  destination directory and atomically rename(2)-ing it into place. An
  existing destination is never truncated in place and never disappears.
- A decided item records the staged file's (st_dev, st_ino) *before* the
  rename. After a crash we can therefore tell a file we published ourselves
  apart from a pre-existing destination file, and finish the interrupted
  ledger commit without re-reading the source or re-publishing.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

COPIED = "COPIED"
DUPLICATE_SKIP = "DUPLICATE_SKIP"
CONFLICT_SKIP = "CONFLICT_SKIP"
OVERWRITTEN = "OVERWRITTEN"

POLICY_SKIP = "skip"
POLICY_OVERWRITE = "overwrite"

_STATE_PENDING = "PENDING"
_STATE_DECIDED = "DECIDED"
_STATE_TERMINAL = "TERMINAL"

_DB_NAME = "docmigr.sqlite3"
_STAGE_MARK = ".migstage-"

_SCHEMA = """
CREATE TABLE task (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    destination TEXT NOT NULL,
    policy TEXT NOT NULL
);
CREATE TABLE items (
    seq INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    dest_rel TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    state TEXT NOT NULL,
    action TEXT,
    content_sha256 TEXT,
    staged_path TEXT,
    staged_dev INTEGER,
    staged_ino INTEGER,
    result TEXT
);
CREATE TABLE dest_metadata (
    dest_rel TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL
);
"""


class TaskExistsError(RuntimeError):
    """Raised when create() is called on a workdir that already holds a task."""


class TaskNotFoundError(RuntimeError):
    """Raised when run()/inspect() is called on a workdir without a task."""


def _db_path(workdir: str) -> str:
    return os.path.join(os.path.abspath(workdir), _DB_NAME)


def _connect(workdir: str, must_exist: bool = True) -> sqlite3.Connection:
    db_path = _db_path(workdir)
    if must_exist and not os.path.exists(db_path):
        raise TaskNotFoundError("no migration task in this workdir")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _load_task(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM task WHERE id = 1").fetchone()
    if row is None:
        raise TaskNotFoundError("no migration task in this workdir")
    return row


def create(
    workdir: str,
    destination: str,
    manifest: List[Dict[str, Any]],
    policy: str,
) -> Dict[str, Any]:
    """Persist a new migration task. One task per workdir."""
    workdir = os.path.abspath(workdir)
    destination = os.path.abspath(destination)
    if policy not in (POLICY_SKIP, POLICY_OVERWRITE):
        raise ValueError("policy must be 'skip' or 'overwrite'")
    if os.path.exists(_db_path(workdir)):
        raise TaskExistsError("workdir already holds a migration task")

    seen_ids = set()
    for entry in manifest:
        for key in ("item_id", "source", "dest"):
            if key not in entry:
                raise ValueError(f"manifest entry missing {key!r}")
        if entry["item_id"] in seen_ids:
            raise ValueError(f"duplicate item_id {entry['item_id']!r}")
        seen_ids.add(entry["item_id"])
        metadata = entry.get("metadata", {})
        json.dumps(metadata)  # must be JSON-serializable

    os.makedirs(workdir, exist_ok=True)
    conn = _connect(workdir, must_exist=False)
    try:
        with conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO task (id, destination, policy) VALUES (1, ?, ?)",
                (destination, policy),
            )
            conn.executemany(
                "INSERT INTO items (seq, item_id, source_path, dest_rel,"
                " metadata_json, state) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        seq,
                        entry["item_id"],
                        os.path.abspath(entry["source"]),
                        entry["dest"],
                        json.dumps(entry.get("metadata", {})),
                        _STATE_PENDING,
                    )
                    for seq, entry in enumerate(manifest)
                ],
            )
    finally:
        conn.close()
    return inspect(workdir)


def inspect(workdir: str) -> Dict[str, Any]:
    """Return per-item status, terminal source SHA-256, and saved metadata."""
    conn = _connect(workdir)
    try:
        task = _load_task(conn)
        rows = conn.execute(
            "SELECT * FROM items ORDER BY seq"
        ).fetchall()
        saved = {
            r["dest_rel"]: json.loads(r["metadata_json"])
            for r in conn.execute("SELECT * FROM dest_metadata")
        }
    finally:
        conn.close()

    items = []
    destinations: Dict[str, Any] = {}
    for row in rows:
        terminal = row["state"] == _STATE_TERMINAL
        items.append(
            {
                "item_id": row["item_id"],
                "state": row["state"],
                "result": row["result"] if terminal else None,
                "sha256": row["content_sha256"] if terminal else None,
            }
        )
        # Every manifest destination reports its currently saved metadata;
        # paths never published to (incl. pre-existing files) report {}.
        destinations.setdefault(row["dest_rel"], saved.get(row["dest_rel"], {}))
    return {
        "destination": task["destination"],
        "policy": task["policy"],
        "items": items,
        "destinations": destinations,
    }


# ---------------------------------------------------------------------------
# Controllable crash hook (test-only): DOCMIG_CRASH_AT="point" or "point@N"
# kills the process with SIGKILL at the Nth (default 1st) hit of `point`.
# ---------------------------------------------------------------------------

_crash_hits: Dict[str, int] = {}


def _crash_hook(point: str) -> None:
    spec = os.environ.get("DOCMIG_CRASH_AT")
    if not spec:
        return
    want_point, _, want_n = spec.partition("@")
    if point != want_point:
        return
    _crash_hits[point] = _crash_hits.get(point, 0) + 1
    want_hit = int(want_n) if want_n else 1
    if _crash_hits[point] == want_hit:
        os.kill(os.getpid(), signal.SIGKILL)


def _hash_and_stage(source: str, staged_path: str) -> str:
    """Copy source bytes to staged_path, fsync, return source SHA-256."""
    digest = hashlib.sha256()
    with open(source, "rb") as fin, open(staged_path, "wb") as fout:
        while True:
            chunk = fin.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return digest.hexdigest()


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fin:
        while True:
            chunk = fin.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _cleanup_orphan_staging(
    conn: sqlite3.Connection, destination: str
) -> None:
    """Remove staged temp files not referenced by any non-terminal item."""
    referenced = {
        row["staged_path"]
        for row in conn.execute(
            "SELECT staged_path FROM items"
            " WHERE state != ? AND staged_path IS NOT NULL",
            (_STATE_TERMINAL,),
        )
    }
    if not os.path.isdir(destination):
        return
    for root, _dirs, files in os.walk(destination):
        for name in files:
            if name.startswith(".") and _STAGE_MARK in name:
                path = os.path.join(root, name)
                if path not in referenced:
                    os.remove(path)


def _decide(conn: sqlite3.Connection, task: sqlite3.Row, item: sqlite3.Row) -> None:
    """PENDING -> DECIDED: stage bytes (if needed) and record the action."""
    dest_abs = os.path.join(task["destination"], item["dest_rel"])
    exists = os.path.lexists(dest_abs)
    policy = task["policy"]

    if exists and policy == POLICY_SKIP:
        sha = _hash_file(item["source_path"])
        dest_sha = _hash_file(dest_abs)
        action = DUPLICATE_SKIP if sha == dest_sha else CONFLICT_SKIP
        with conn:
            conn.execute(
                "UPDATE items SET state = ?, action = ?, content_sha256 = ?"
                " WHERE seq = ?",
                (_STATE_DECIDED, action, sha, item["seq"]),
            )
        _crash_hook("after_decide")
        return

    parent = os.path.dirname(dest_abs)
    os.makedirs(parent, exist_ok=True)
    staged_path = os.path.join(
        parent, f".{os.path.basename(dest_abs)}{_STAGE_MARK}{uuid.uuid4().hex}"
    )
    sha = _hash_and_stage(item["source_path"], staged_path)
    _crash_hook("after_stage")
    st = os.stat(staged_path)
    action = COPIED if not exists else OVERWRITTEN
    with conn:
        conn.execute(
            "UPDATE items SET state = ?, action = ?, content_sha256 = ?,"
            " staged_path = ?, staged_dev = ?, staged_ino = ? WHERE seq = ?",
            (
                _STATE_DECIDED,
                action,
                sha,
                staged_path,
                st.st_dev,
                st.st_ino,
                item["seq"],
            ),
        )
    _crash_hook("after_decide")


def _publish_and_terminate(
    conn: sqlite3.Connection, task: sqlite3.Row, item: sqlite3.Row
) -> None:
    """DECIDED -> TERMINAL: publish bytes (if the action requires it), then
    commit the terminal result and the destination metadata in one txn."""
    action = item["action"]
    dest_abs = os.path.join(task["destination"], item["dest_rel"])

    if action in (COPIED, OVERWRITTEN):
        published = False
        try:
            st = os.stat(dest_abs)
            published = (st.st_dev, st.st_ino) == (
                item["staged_dev"],
                item["staged_ino"],
            )
        except FileNotFoundError:
            pass
        if not published:
            staged_path = item["staged_path"]
            if not os.path.exists(staged_path):
                raise RuntimeError(
                    f"staged file missing for undecided publish of {dest_abs}"
                )
            os.rename(staged_path, dest_abs)
            _fsync_dir(os.path.dirname(dest_abs))
        _crash_hook("after_rename")

    with conn:
        if action in (COPIED, OVERWRITTEN):
            conn.execute(
                "INSERT INTO dest_metadata (dest_rel, metadata_json)"
                " VALUES (?, ?)"
                " ON CONFLICT(dest_rel) DO UPDATE SET metadata_json ="
                " excluded.metadata_json",
                (item["dest_rel"], item["metadata_json"]),
            )
        conn.execute(
            "UPDATE items SET state = ?, result = ?, staged_path = NULL,"
            " staged_dev = NULL, staged_ino = NULL WHERE seq = ?",
            (_STATE_TERMINAL, action, item["seq"]),
        )
    _crash_hook("after_commit")


def _process_item(
    conn: sqlite3.Connection, task: sqlite3.Row, item: sqlite3.Row
) -> None:
    if item["state"] == _STATE_PENDING:
        _decide(conn, task, item)
        item = conn.execute(
            "SELECT * FROM items WHERE seq = ?", (item["seq"],)
        ).fetchone()
    _publish_and_terminate(conn, task, item)


def run(workdir: str) -> Dict[str, Any]:
    """Execute or resume the task until every entry is terminal. Idempotent."""
    conn = _connect(workdir)
    try:
        task = _load_task(conn)
        _cleanup_orphan_staging(conn, task["destination"])
        while True:
            item = conn.execute(
                "SELECT * FROM items WHERE state != ? ORDER BY seq LIMIT 1",
                (_STATE_TERMINAL,),
            ).fetchone()
            if item is None:
                break
            _process_item(conn, task, item)
    finally:
        conn.close()
    return inspect(workdir)
