"""Task creation, execution/recovery, and inspection logic."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from . import _store
from ._store import Store

POLICY_SKIP = "skip"
POLICY_OVERWRITE = "overwrite"

_CHUNK = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _crashpoint(name: str, item_id: str) -> None:
    """Test hook: SIGKILL self when the configured crashpoint is reached."""
    point = os.environ.get("MIGRATE_CRASHPOINT")
    if not point or point != name:
        return
    wanted_item = os.environ.get("MIGRATE_CRASHPOINT_ITEM")
    if wanted_item and wanted_item != item_id:
        return
    import signal
    os.kill(os.getpid(), signal.SIGKILL)


def create(workdir: os.PathLike | str, destination: os.PathLike | str,
           manifest: list[dict], policy: str) -> None:
    if policy not in (POLICY_SKIP, POLICY_OVERWRITE):
        raise ValueError(f"unknown policy: {policy!r}")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    normalized = []
    seen_ids = set()
    for item in manifest:
        item_id = item["item_id"]
        if item_id in seen_ids:
            raise ValueError(f"duplicate item_id: {item_id!r}")
        seen_ids.add(item_id)
        metadata = item.get("metadata", {})
        json.dumps(metadata)  # must be JSON-serializable
        normalized.append({
            "item_id": item_id,
            "src_path": str(item["src_path"]),
            "dst_rel": str(item["dst_rel"]),
            "metadata": metadata,
        })
    Store(workdir).create_task(str(Path(destination).resolve()), policy, normalized)


def _decide(store: Store, row: _store.ItemRow, destination: Path,
            policy: str) -> _store.ItemRow:
    src_sha = _sha256_file(Path(row.src_path))
    dst = destination / row.dst_rel
    if not dst.is_file():
        action, result = _store.ACTION_PUBLISH, _store.RESULT_COPIED
    elif policy == POLICY_OVERWRITE:
        action, result = _store.ACTION_PUBLISH, _store.RESULT_OVERWRITTEN
    elif _sha256_file(dst) == src_sha:
        action, result = _store.ACTION_KEEP, _store.RESULT_DUPLICATE_SKIP
    else:
        action, result = _store.ACTION_KEEP, _store.RESULT_CONFLICT_SKIP
    token = uuid.uuid4().hex
    store.record_decision(row.seq, action, result, src_sha, token)
    _crashpoint("after_decide", row.item_id)
    return _store.ItemRow(
        seq=row.seq, item_id=row.item_id, src_path=row.src_path,
        dst_rel=row.dst_rel, metadata_json=row.metadata_json,
        state=_store.STATE_DECIDED, action=action, result=result,
        src_sha256=src_sha, publish_token=token,
    )


def _publish(row: _store.ItemRow, destination: Path) -> None:
    dst = destination / row.dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.{row.publish_token}.tmp"
    with open(row.src_path, "rb") as src, open(tmp, "wb") as out:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    _crashpoint("after_temp_write", row.item_id)
    os.replace(tmp, dst)
    dir_fd = os.open(dst.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    _crashpoint("after_rename", row.item_id)


def run(workdir: os.PathLike | str) -> list[dict]:
    store = Store(Path(workdir))
    if not store.exists():
        raise FileNotFoundError(f"no task in workdir: {workdir}")
    meta = store.task_meta()
    policy = meta["policy"]
    destination = Path(meta["destination"])

    for row in store.items():
        if row.state == _store.STATE_TERMINAL:
            continue
        if row.state == _store.STATE_PENDING:
            row = _decide(store, row, destination, policy)
        if row.action == _store.ACTION_PUBLISH:
            _publish(row, destination)
        store.record_finalize(
            row.seq, row.dst_rel, row.metadata_json,
            published=row.action == _store.ACTION_PUBLISH,
        )
        _crashpoint("after_finalize", row.item_id)

    return inspect(workdir)["items"]


def inspect(workdir: os.PathLike | str) -> dict:
    store = Store(Path(workdir))
    if not store.exists():
        raise FileNotFoundError(f"no task in workdir: {workdir}")
    meta = store.task_meta()
    items = store.items()
    saved_metadata = store.destination_metadata()
    destination = Path(meta["destination"])

    item_entries = []
    dst_paths: list[str] = []
    for row in items:
        item_entries.append({
            "item_id": row.item_id,
            "state": row.state,
            "result": row.result if row.state == _store.STATE_TERMINAL else None,
            "src_sha256": row.src_sha256 if row.state == _store.STATE_TERMINAL else None,
            "dst_rel": row.dst_rel,
        })
        if row.dst_rel not in dst_paths:
            dst_paths.append(row.dst_rel)

    destinations = {}
    for rel in dst_paths:
        destinations[rel] = {
            "exists": (destination / rel).is_file(),
            "metadata": saved_metadata.get(rel, {}),
        }

    return {
        "policy": meta["policy"],
        "destination": meta["destination"],
        "items": item_entries,
        "destinations": destinations,
    }
