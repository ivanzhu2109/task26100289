import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import docmigrate

REPO_ROOT = Path(__file__).resolve().parent.parent

RUNNER = "import docmigrate, sys; docmigrate.run(sys.argv[1])"


def write_file(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def make_item(item_id, src_path, dst_rel, metadata):
    return {
        "item_id": item_id,
        "src_path": str(src_path),
        "dst_rel": dst_rel,
        "metadata": metadata,
    }


def run_in_subprocess(workdir, crashpoint=None, crash_item=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if crashpoint:
        env["MIGRATE_CRASHPOINT"] = crashpoint
    if crash_item:
        env["MIGRATE_CRASHPOINT_ITEM"] = crash_item
    return subprocess.run(
        [sys.executable, "-c", RUNNER, str(workdir)],
        env=env, capture_output=True, text=True,
    )


def assert_killed(proc):
    assert proc.returncode == -signal.SIGKILL, (
        f"expected SIGKILL, got {proc.returncode}: {proc.stderr}")


def assert_clean(proc):
    assert proc.returncode == 0, f"run failed: {proc.stderr}"


def build_case(root: Path, policy: str):
    """A 4-item manifest exercising same/different content on shared targets."""
    src = root / "src"
    write_file(src / "a.bin", b"alpha")
    write_file(src / "b.bin", b"alpha")
    write_file(src / "c.bin", b"gamma")
    write_file(src / "d.bin", b"delta")
    manifest = [
        make_item("A", src / "a.bin", "shared/out.bin", {"n": 1}),
        make_item("B", src / "b.bin", "shared/out.bin", {"n": 2}),
        make_item("C", src / "c.bin", "nested/deep/c.bin", {"n": 3}),
        make_item("D", src / "d.bin", "shared/out.bin", {"n": 4}),
    ]
    workdir = root / "work"
    dst = root / "dst"
    docmigrate.create(workdir, dst, manifest, policy)
    return workdir, dst


def normalized_report(workdir):
    report = docmigrate.inspect(workdir)
    report.pop("destination")
    return report


def dst_snapshot(dst: Path):
    if not dst.exists():
        return {}
    return {
        str(p.relative_to(dst)): p.read_bytes()
        for p in sorted(dst.rglob("*")) if p.is_file()
    }


def reference_run(root: Path, policy: str):
    workdir, dst = build_case(root / "ref", policy)
    docmigrate.run(workdir)
    return normalized_report(workdir), dst_snapshot(dst)


def assert_matches_reference(root, policy):
    workdir, dst = build_case(root / "crash", policy)
    return workdir, dst, reference_run(root, policy)


def check_converged(workdir, dst, ref_report, ref_files):
    assert normalized_report(workdir) == ref_report
    assert dst_snapshot(dst) == ref_files
    leftovers = [p for p in dst.rglob("*.tmp")]
    assert leftovers == []


def test_crash_after_rename_before_ledger_commit(tmp_path):
    for policy in ("skip", "overwrite"):
        workdir, dst, (ref_report, ref_files) = assert_matches_reference(tmp_path / policy, policy)
        # Kill between publishing A's file and persisting A's terminal state.
        assert_killed(run_in_subprocess(workdir, "after_rename", "A"))
        assert_clean(run_in_subprocess(workdir))
        check_converged(workdir, dst, ref_report, ref_files)


def test_crash_after_decide_before_publish(tmp_path):
    for policy in ("skip", "overwrite"):
        workdir, dst, (ref_report, ref_files) = assert_matches_reference(tmp_path / policy, policy)
        assert_killed(run_in_subprocess(workdir, "after_decide", "A"))
        assert_clean(run_in_subprocess(workdir))
        check_converged(workdir, dst, ref_report, ref_files)


def test_crash_after_temp_write(tmp_path):
    for policy in ("skip", "overwrite"):
        workdir, dst, (ref_report, ref_files) = assert_matches_reference(tmp_path / policy, policy)
        assert_killed(run_in_subprocess(workdir, "after_temp_write", "A"))
        assert_clean(run_in_subprocess(workdir))
        check_converged(workdir, dst, ref_report, ref_files)


def test_crash_after_finalize(tmp_path):
    for policy in ("skip", "overwrite"):
        workdir, dst, (ref_report, ref_files) = assert_matches_reference(tmp_path / policy, policy)
        assert_killed(run_in_subprocess(workdir, "after_finalize", "B"))
        assert_clean(run_in_subprocess(workdir))
        check_converged(workdir, dst, ref_report, ref_files)


def test_multiple_consecutive_crashes(tmp_path):
    for policy in ("skip", "overwrite"):
        workdir, dst, (ref_report, ref_files) = assert_matches_reference(tmp_path / policy, policy)
        assert_killed(run_in_subprocess(workdir, "after_rename", "A"))
        assert_killed(run_in_subprocess(workdir, "after_decide", "C"))
        assert_killed(run_in_subprocess(workdir, "after_finalize", "C"))
        assert_clean(run_in_subprocess(workdir))
        check_converged(workdir, dst, ref_report, ref_files)


def test_crash_window_preserves_copied_not_duplicate_skip(tmp_path):
    # Same bytes at source and (post-publish) destination: a recovery that
    # re-derives the decision from destination content would wrongly report
    # DUPLICATE_SKIP instead of COPIED.
    root = tmp_path / "case"
    src = root / "src"
    write_file(src / "a.bin", b"same-bytes")
    workdir, dst = root / "work", root / "dst"
    docmigrate.create(workdir, dst, [make_item("A", src / "a.bin", "out.bin", {"m": 1})], "skip")
    assert_killed(run_in_subprocess(workdir, "after_rename", "A"))
    assert_clean(run_in_subprocess(workdir))
    report = docmigrate.inspect(workdir)
    assert report["items"][0]["result"] == "COPIED"
    assert report["destinations"]["out.bin"]["metadata"] == {"m": 1}


def test_terminal_items_never_reread_sources(tmp_path):
    for policy in ("skip", "overwrite"):
        root = tmp_path / policy
        workdir, dst, (ref_report, ref_files) = assert_matches_reference(root, policy)
        # Stop after item B is fully terminal.
        assert_killed(run_in_subprocess(workdir, "after_finalize", "B"))
        # Sources of terminal items A and B are no longer needed: move them away.
        moved = root / "moved-sources"
        src_dir = root / "crash" / "src"
        moved.mkdir(exist_ok=True)
        shutil.move(str(src_dir / "a.bin"), moved / "a.bin")
        shutil.move(str(src_dir / "b.bin"), moved / "b.bin")
        # Resume remaining items; sources C and D are still in place.
        assert_clean(run_in_subprocess(workdir))
        # Repeated calls are safe and change nothing.
        assert_clean(run_in_subprocess(workdir))
        assert_clean(run_in_subprocess(workdir))
        check_converged(workdir, dst, ref_report, ref_files)


def test_crash_after_rename_on_second_shared_target_item(tmp_path):
    # B publishes to the same target A already published: the crash window
    # leaves B's bytes with A's metadata; recovery must converge both.
    # (Only overwrite policy publishes B; skip keeps A's file for B.)
    workdir, dst, (ref_report, ref_files) = assert_matches_reference(tmp_path, "overwrite")
    assert_killed(run_in_subprocess(workdir, "after_rename", "B"))
    assert_clean(run_in_subprocess(workdir))
    check_converged(workdir, dst, ref_report, ref_files)
