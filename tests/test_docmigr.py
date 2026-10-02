"""Automated tests for the resumable document migration component."""

import hashlib
import os
import shutil
import signal
import subprocess
import sys

import pytest

import docmigr

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CRASH_POINTS = ["after_stage", "after_decide", "after_rename", "after_commit"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)


def sha256_of(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def make_layout(tmp_path, policy="skip", preexisting=None):
    """Create src/dst/workdir plus a manifest exercising same-dest entries."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    wd = tmp_path / "wd"
    src.mkdir(parents=True)
    dst.mkdir()
    write_file(str(src / "a.txt"), "hello")
    write_file(str(src / "b.txt"), "hello")   # same bytes as a, other metadata
    write_file(str(src / "c.txt"), "world")   # different bytes, same dest
    write_file(str(src / "d.txt"), "deep")    # nested dest dir
    manifest = [
        {"item_id": "A", "source": str(src / "a.txt"), "dest": "x",
         "metadata": {"v": 1}},
        {"item_id": "B", "source": str(src / "b.txt"), "dest": "x",
         "metadata": {"v": 2}},
        {"item_id": "C", "source": str(src / "c.txt"), "dest": "x",
         "metadata": {"v": 3}},
        {"item_id": "D", "source": str(src / "d.txt"), "dest": "sub/y",
         "metadata": {"v": 4}},
    ]
    if preexisting:
        write_file(str(dst / "pre.txt"), preexisting)
        write_file(str(src / "e.txt"), preexisting)
        manifest.append(
            {"item_id": "E", "source": str(src / "e.txt"), "dest": "pre.txt",
             "metadata": {"v": 5}}
        )
    docmigr.create(str(wd), str(dst), manifest, policy)
    return src, dst, wd


def snapshot(dst):
    """Full byte-level snapshot of every file under the destination."""
    snap = {}
    for root, _dirs, files in os.walk(dst):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, dst)
            with open(path, "rb") as fh:
                snap[rel] = fh.read()
    return snap


def run_in_subprocess(wd, crash_at=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    if crash_at is not None:
        env["DOCMIG_CRASH_AT"] = crash_at
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; import docmigr; docmigr.run(sys.argv[1])", str(wd)],
        env=env,
        capture_output=True,
    )


def assert_killed(proc):
    assert proc.returncode == -signal.SIGKILL, (
        f"expected SIGKILL, got {proc.returncode}: {proc.stderr!r}")


def results_of(report):
    return {item["item_id"]: item["result"] for item in report["items"]}


def comparable(report):
    """Report without environment-specific absolute paths."""
    return {k: v for k, v in report.items() if k != "destination"}


# --------------------------------------------------------------------------
# 1. full history for same-destination entries, both policies
# --------------------------------------------------------------------------

def test_skip_policy_same_dest_history(tmp_path):
    src, dst, wd = make_layout(tmp_path, policy="skip", preexisting="hello")
    report = docmigr.run(str(wd))

    assert results_of(report) == {
        "A": docmigr.COPIED,
        "B": docmigr.DUPLICATE_SKIP,   # same bytes as A's publish
        "C": docmigr.CONFLICT_SKIP,    # different bytes, keep A's
        "D": docmigr.COPIED,
        "E": docmigr.DUPLICATE_SKIP,   # matches pre-existing bytes
    }
    # original destination bytes and metadata preserved
    assert (dst / "x").read_text() == "hello"
    assert (dst / "sub" / "y").read_text() == "deep"
    assert (dst / "pre.txt").read_text() == "hello"
    assert report["destinations"] == {
        "x": {"v": 1}, "sub/y": {"v": 4}, "pre.txt": {}}
    # terminal items carry their source content hash
    for item in report["items"]:
        assert item["state"] == "TERMINAL"
        assert item["sha256"] == sha256_of(
            str(src / (item["item_id"].lower() + ".txt")))
    # run() again is a safe no-op
    before = snapshot(str(dst))
    assert docmigr.run(str(wd)) == report
    assert snapshot(str(dst)) == before


def test_overwrite_policy_same_dest_history(tmp_path):
    src, dst, wd = make_layout(tmp_path, policy="overwrite",
                               preexisting="hello")
    report = docmigr.run(str(wd))

    assert results_of(report) == {
        "A": docmigr.COPIED,
        "B": docmigr.OVERWRITTEN,      # same bytes, still overwritten
        "C": docmigr.OVERWRITTEN,
        "D": docmigr.COPIED,
        "E": docmigr.OVERWRITTEN,      # pre-existing file replaced
    }
    assert (dst / "x").read_text() == "world"          # last writer wins
    assert (dst / "pre.txt").read_text() == "hello"
    assert report["destinations"] == {
        "x": {"v": 3}, "sub/y": {"v": 4}, "pre.txt": {"v": 5}}
    assert docmigr.run(str(wd)) == report


def test_create_rejects_second_task(tmp_path):
    src, dst, wd = make_layout(tmp_path)
    with pytest.raises(docmigr.TaskExistsError):
        docmigr.create(str(wd), str(dst), [], "skip")


def test_run_without_task_raises(tmp_path):
    with pytest.raises(docmigr.TaskNotFoundError):
        docmigr.run(str(tmp_path / "empty"))
    with pytest.raises(docmigr.TaskNotFoundError):
        docmigr.inspect(str(tmp_path / "empty"))


# --------------------------------------------------------------------------
# 2. crash windows: real SIGKILL between publish and ledger persistence
# --------------------------------------------------------------------------

@pytest.mark.parametrize("policy", ["skip", "overwrite"])
@pytest.mark.parametrize("point", CRASH_POINTS)
@pytest.mark.parametrize("hit", [1, 2])
def test_crash_recovery_matches_uninterrupted(tmp_path, policy, point, hit):
    # golden: uninterrupted run
    g_src, g_dst, g_wd = make_layout(tmp_path / "golden", policy=policy,
                                     preexisting="hello")
    golden_report = docmigr.run(str(g_wd))
    golden_snap = snapshot(str(g_dst))

    # crashed: kill at the chosen window, then recover to completion
    c_src, c_dst, c_wd = make_layout(tmp_path / "crash", policy=policy,
                                     preexisting="hello")
    proc = run_in_subprocess(c_wd, crash_at=f"{point}@{hit}")
    assert_killed(proc)
    final = docmigr.run(str(c_wd))

    # per-item results and saved metadata identical to uninterrupted run
    assert comparable(final) == comparable(golden_report)
    # final destination bytes identical, no partial/staged files left
    assert snapshot(str(c_dst)) == golden_snap


def test_multiple_crashes_in_one_task(tmp_path):
    g_src, g_dst, g_wd = make_layout(tmp_path / "golden", policy="overwrite",
                                     preexisting="hello")
    golden_report = docmigr.run(str(g_wd))
    golden_snap = snapshot(str(g_dst))

    c_src, c_dst, c_wd = make_layout(tmp_path / "crash", policy="overwrite",
                                     preexisting="hello")
    # three consecutive interruptions at different windows
    for crash_at in ("after_rename@1", "after_decide@2", "after_commit@3"):
        proc = run_in_subprocess(c_wd, crash_at=crash_at)
        assert_killed(proc)
    final = docmigr.run(str(c_wd))

    assert comparable(final) == comparable(golden_report)
    assert snapshot(str(c_dst)) == golden_snap


def test_crash_never_loses_existing_destination(tmp_path):
    # pre-existing destination must survive a kill in the publish window
    src, dst, wd = make_layout(tmp_path, policy="overwrite",
                               preexisting="original")
    write_file(str(src / "e.txt"), "replacement")
    # item E is the 5th publish: kill between its rename and ledger commit
    proc = run_in_subprocess(wd, crash_at="after_rename@5")
    assert_killed(proc)
    # the destination was never missing or partially written
    assert (dst / "pre.txt").read_text() in ("original", "replacement")
    # after recovery the destination converges to the interrupted item
    docmigr.run(str(wd))
    assert (dst / "pre.txt").read_text() == "replacement"
    assert docmigr.inspect(str(wd))["destinations"]["pre.txt"] == {"v": 5}


# --------------------------------------------------------------------------
# 3. terminated items never re-read their source
# --------------------------------------------------------------------------

def test_sources_moved_after_termination(tmp_path):
    g_src, g_dst, g_wd = make_layout(tmp_path / "golden", policy="overwrite")
    golden_report = docmigr.run(str(g_wd))
    golden_snap = snapshot(str(g_dst))

    src, dst, wd = make_layout(tmp_path / "crash", policy="overwrite")
    # kill right after the first item's terminal commit
    proc = run_in_subprocess(wd, crash_at="after_commit@1")
    assert_killed(proc)

    mid = docmigr.inspect(str(wd))
    assert results_of(mid)["A"] == docmigr.COPIED
    assert mid["items"][1]["state"] != "TERMINAL"

    # move away the source of the terminated item, then resume
    shutil.move(str(src / "a.txt"), str(src / "a.gone"))
    final = docmigr.run(str(wd))
    again = docmigr.run(str(wd))  # repeated run: still no source dependency

    assert comparable(final) == comparable(golden_report)
    assert comparable(again) == comparable(golden_report)
    assert snapshot(str(dst)) == golden_snap
    # terminated item's hash still reported without its source file
    assert results_of(final)["A"] == docmigr.COPIED
    assert final["items"][0]["sha256"] == hashlib.sha256(b"hello").hexdigest()
