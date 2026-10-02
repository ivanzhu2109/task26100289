import hashlib
import json

import pytest

import docmigrate


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


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def results_by_id(report):
    return {entry["item_id"]: entry for entry in report["items"]}


@pytest.fixture
def paths(tmp_path):
    return {
        "workdir": tmp_path / "work",
        "src": tmp_path / "src",
        "dst": tmp_path / "dst",
    }


def test_same_destination_same_content_different_metadata_skip(paths):
    data = b"identical-bytes"
    src_a = paths["src"] / "a.bin"
    src_b = paths["src"] / "b.bin"
    write_file(src_a, data)
    write_file(src_b, data)
    manifest = [
        make_item("A", src_a, "out.bin", {"version": 1}),
        make_item("B", src_b, "out.bin", {"version": 2}),
    ]
    docmigrate.create(paths["workdir"], paths["dst"], manifest, "skip")
    run_report = docmigrate.run(paths["workdir"])
    report = docmigrate.inspect(paths["workdir"])

    results = results_by_id(report)
    assert results["A"]["result"] == "COPIED"
    assert results["B"]["result"] == "DUPLICATE_SKIP"
    assert results["A"]["src_sha256"] == sha256(data)
    assert results["B"]["src_sha256"] == sha256(data)
    assert [e["item_id"] for e in run_report] == ["A", "B"]

    assert (paths["dst"] / "out.bin").read_bytes() == data
    assert report["destinations"]["out.bin"]["metadata"] == {"version": 1}


def test_same_destination_same_content_different_metadata_overwrite(paths):
    data = b"identical-bytes"
    src_a = paths["src"] / "a.bin"
    src_b = paths["src"] / "b.bin"
    write_file(src_a, data)
    write_file(src_b, data)
    manifest = [
        make_item("A", src_a, "out.bin", {"version": 1}),
        make_item("B", src_b, "out.bin", {"version": 2}),
    ]
    docmigrate.create(paths["workdir"], paths["dst"], manifest, "overwrite")
    docmigrate.run(paths["workdir"])
    report = docmigrate.inspect(paths["workdir"])

    results = results_by_id(report)
    assert results["A"]["result"] == "COPIED"
    assert results["B"]["result"] == "OVERWRITTEN"
    assert (paths["dst"] / "out.bin").read_bytes() == data
    assert report["destinations"]["out.bin"]["metadata"] == {"version": 2}


def test_same_destination_different_content_skip(paths):
    src_a = paths["src"] / "a.bin"
    src_b = paths["src"] / "b.bin"
    write_file(src_a, b"content-A")
    write_file(src_b, b"content-B-different")
    manifest = [
        make_item("A", src_a, "out.bin", {"who": "A"}),
        make_item("B", src_b, "out.bin", {"who": "B"}),
    ]
    docmigrate.create(paths["workdir"], paths["dst"], manifest, "skip")
    docmigrate.run(paths["workdir"])
    report = docmigrate.inspect(paths["workdir"])

    results = results_by_id(report)
    assert results["A"]["result"] == "COPIED"
    assert results["B"]["result"] == "CONFLICT_SKIP"
    assert (paths["dst"] / "out.bin").read_bytes() == b"content-A"
    assert report["destinations"]["out.bin"]["metadata"] == {"who": "A"}


def test_same_destination_different_content_overwrite(paths):
    src_a = paths["src"] / "a.bin"
    src_b = paths["src"] / "b.bin"
    write_file(src_a, b"content-A")
    write_file(src_b, b"content-B-different")
    manifest = [
        make_item("A", src_a, "out.bin", {"who": "A"}),
        make_item("B", src_b, "out.bin", {"who": "B"}),
    ]
    docmigrate.create(paths["workdir"], paths["dst"], manifest, "overwrite")
    docmigrate.run(paths["workdir"])
    report = docmigrate.inspect(paths["workdir"])

    results = results_by_id(report)
    assert results["A"]["result"] == "COPIED"
    assert results["B"]["result"] == "OVERWRITTEN"
    assert (paths["dst"] / "out.bin").read_bytes() == b"content-B-different"
    assert report["destinations"]["out.bin"]["metadata"] == {"who": "B"}


def test_preexisting_destination_skip_and_overwrite(tmp_path):
    for policy, expected_result in [("skip", "CONFLICT_SKIP"), ("overwrite", "OVERWRITTEN")]:
        workdir = tmp_path / policy / "work"
        dst = tmp_path / policy / "dst"
        src = tmp_path / policy / "src" / "new.bin"
        write_file(dst / "out.bin", b"preexisting")
        write_file(src, b"new-content")
        manifest = [make_item("X", src, "out.bin", {"m": 1})]
        docmigrate.create(workdir, dst, manifest, policy)
        docmigrate.run(workdir)
        report = docmigrate.inspect(workdir)
        results = results_by_id(report)
        assert results["X"]["result"] == expected_result
        if policy == "skip":
            assert (dst / "out.bin").read_bytes() == b"preexisting"
            assert report["destinations"]["out.bin"]["metadata"] == {}
        else:
            assert (dst / "out.bin").read_bytes() == b"new-content"
            assert report["destinations"]["out.bin"]["metadata"] == {"m": 1}


def test_preexisting_destination_duplicate_skip(tmp_path):
    workdir, dst = tmp_path / "work", tmp_path / "dst"
    src = tmp_path / "src" / "same.bin"
    write_file(dst / "out.bin", b"same")
    write_file(src, b"same")
    docmigrate.create(workdir, dst, [make_item("X", src, "out.bin", {"m": 1})], "skip")
    docmigrate.run(workdir)
    report = docmigrate.inspect(workdir)
    assert results_by_id(report)["X"]["result"] == "DUPLICATE_SKIP"
    assert report["destinations"]["out.bin"]["metadata"] == {}


def test_run_is_idempotent_and_create_rejects_second_task(paths):
    src = paths["src"] / "a.bin"
    write_file(src, b"data")
    manifest = [make_item("A", src, "out.bin", {})]
    docmigrate.create(paths["workdir"], paths["dst"], manifest, "skip")
    first = docmigrate.run(paths["workdir"])
    second = docmigrate.run(paths["workdir"])
    assert first == second
    with pytest.raises(FileExistsError):
        docmigrate.create(paths["workdir"], paths["dst"], manifest, "skip")


def test_history_preserved_across_later_overwrites(paths):
    src_a = paths["src"] / "a.bin"
    src_b = paths["src"] / "b.bin"
    src_c = paths["src"] / "c.bin"
    write_file(src_a, b"one")
    write_file(src_b, b"two")
    write_file(src_c, b"one")
    manifest = [
        make_item("A", src_a, "out.bin", {"n": 1}),
        make_item("B", src_b, "out.bin", {"n": 2}),
        make_item("C", src_c, "out.bin", {"n": 3}),
    ]
    docmigrate.create(paths["workdir"], paths["dst"], manifest, "overwrite")
    docmigrate.run(paths["workdir"])
    report = docmigrate.inspect(paths["workdir"])
    results = results_by_id(report)
    assert [results[k]["result"] for k in "ABC"] == ["COPIED", "OVERWRITTEN", "OVERWRITTEN"]
    assert results["A"]["src_sha256"] == sha256(b"one")
    assert results["B"]["src_sha256"] == sha256(b"two")
    assert (paths["dst"] / "out.bin").read_bytes() == b"one"
    assert report["destinations"]["out.bin"]["metadata"] == {"n": 3}
