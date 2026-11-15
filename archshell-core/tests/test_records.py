"""Tests of confirmation records (story 1.8, FR-53, AD-10)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from archshell.adapters import records as adapter_records
from archshell.adapters.records import InvalidRecord, append_record, read_records
from archshell.domain import layout, records

FEATURE = "cancel-appointment"
DIR = Path(".archshell") / "records" / FEATURE
CONTENT = {"decision": "cancel within 24 hours", "steps": ["notify", "refund"]}
AT = "2026-11-14T09:30:00Z"


def write(root: Path, flow: str = "plan", **overrides):
    fields = dict(feature=FEATURE, flow=flow, confirmed_by="ana", content=CONTENT, confirmed_at=AT)
    fields.update(overrides)
    return append_record(root, **fields)


def files(root: Path) -> dict[str, bytes]:
    base = root / ".archshell"
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


# --- matrix rows -------------------------------------------------------------


def test_first_record(tmp_path):
    record = write(tmp_path)
    assert record.path == ".archshell/records/cancel-appointment/0001-plan.yaml"
    fingerprint = adapter_records.fingerprint(CONTENT)
    assert (tmp_path / DIR / "0001-plan.yaml").read_bytes() == (
        "schema: archshell/record/v1\n"
        "feature: cancel-appointment\n"
        "seq: 1\n"
        "flow: plan\n"
        f'fingerprint: "{fingerprint}"\n'
        "confirmed_by: ana\n"
        'confirmed_at: "2026-11-14T09:30:00Z"\n'
        "derived: false\n"
        "content:\n"
        "  decision: cancel within 24 hours\n"
        "  steps:\n"
        "    - notify\n"
        "    - refund\n"
    ).encode("utf-8")
    [read] = read_records(tmp_path, FEATURE)
    assert read.path == record.path and read.document == record.document
    # Only the record is left: no claim, no temporary file.
    assert list(files(tmp_path)) == ["records/cancel-appointment/0001-plan.yaml"]


def test_same_flow_twice_keeps_the_first_bytes(tmp_path):
    write(tmp_path)
    first = (tmp_path / DIR / "0001-plan.yaml").read_bytes()
    record = write(tmp_path, content={"decision": "changed"})
    assert record.path.endswith("/0002-plan.yaml")
    assert (tmp_path / DIR / "0001-plan.yaml").read_bytes() == first
    assert [r.document["seq"] for r in read_records(tmp_path, FEATURE)] == [1, 2]


def test_mixed_flows_share_one_sequence(tmp_path):
    write(tmp_path, "plan")
    write(tmp_path, "contract")
    assert sorted(p.name for p in (tmp_path / DIR).iterdir()) == ["0001-plan.yaml", "0002-contract.yaml"]


RACER = """
import sys, time
from pathlib import Path
from archshell.adapters.records import append_record
root, flow, go = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
while not go.exists():
    time.sleep(0.001)
print(append_record(root, feature="cancel-appointment", flow=flow, confirmed_by="ana",
                    content={"n": sys.argv[4]}).path)
"""


def test_concurrent_writers_get_distinct_seqs(tmp_path):
    go = tmp_path / "go"
    root = tmp_path / "project"
    flows = ["plan", "contract"] * 4
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", RACER, str(root), flow, str(go), str(n)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for n, flow in enumerate(flows)
    ]
    time.sleep(1.0)  # let every interpreter start and wait on `go`
    go.touch()
    paths = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()
        paths.append(out.decode().strip())
    assert len(set(paths)) == len(flows)
    read = read_records(root, FEATURE)
    assert [r.document["seq"] for r in read] == list(range(1, len(flows) + 1))
    assert sorted(r.path for r in read) == sorted(paths)
    assert all(p.startswith("records/") for p in files(root))  # no claims or temp files left


@pytest.mark.parametrize(
    ("overrides", "path"),
    [
        ({"flow": "approval"}, "$.flow"),
        ({"feature": "Cancel_Appointment"}, "$.feature"),
        ({"feature": "../escape"}, "$.feature"),
        ({"feature": "abc\n"}, "$.feature"),
        ({"fingerprint": "sha256:" + "0" * 64 + "\n", "content": None}, "$.fingerprint"),
        ({"confirmed_by": "\x0b"}, "$.confirmed_by"),
        ({"confirmed_by": "\u00a0"}, "$.confirmed_by"),
        ({"content": None, "fingerprint": "sha256:abc"}, "$.fingerprint"),
        ({"content": None, "fingerprint": None}, "$.fingerprint"),
        ({"fingerprint": "sha256:" + "0" * 64}, "$.fingerprint"),
        ({"confirmed_by": ""}, "$.confirmed_by"),
        ({"confirmed_by": "  "}, "$.confirmed_by"),
        ({"confirmed_at": "2026-11-14 09:30:00"}, "$.confirmed_at"),
        ({"confirmed_at": "2026-13-45T09:30:00Z"}, "$.confirmed_at"),
        ({"derived": "yes"}, "$.derived"),
        ({"content": {"ratio": 0.5}}, "$.content"),
    ],
)
def test_bad_input_writes_nothing(tmp_path, overrides, path):
    with pytest.raises(InvalidRecord) as caught:
        write(tmp_path, **overrides)
    assert caught.value.errors and caught.value.errors[0][0] == path
    assert caught.value.file is None
    assert not (tmp_path / ".archshell").exists()


def test_read_in_seq_order(tmp_path):
    for flow in ("plan", "growth", "vocabulary"):
        write(tmp_path, flow)
    assert [(r.document["seq"], r.document["flow"]) for r in read_records(tmp_path, FEATURE)] == [
        (1, "plan"),
        (2, "growth"),
        (3, "vocabulary"),
    ]


def _drop_fingerprint(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(line for line in lines if not line.startswith("fingerprint:")), encoding="utf-8")


def _newline_fingerprint(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index('fingerprint: "') + len('fingerprint: "') + len("sha256:") + 64
    path.write_text(text[:start] + "\\n" + text[start:], encoding="utf-8")  # YAML escape: value ends in LF


@pytest.mark.parametrize(
    ("corrupt", "name", "error_path"),
    [
        (_drop_fingerprint, "0001-plan.yaml", "$"),
        (lambda p: p.rename(p.with_name("0002-plan.yaml")), "0002-plan.yaml", "$.seq"),
        (lambda p: p.rename(p.with_name("0001-contract.yaml")), "0001-contract.yaml", "$.flow"),
        (lambda p: p.write_bytes(b"schema: [unclosed\n"), "0001-plan.yaml", "$"),
        (lambda p: p.write_bytes(b"\xff\n"), "0001-plan.yaml", "$"),
        (lambda p: p.with_name("notes.txt").write_bytes(b"x\n"), "notes.txt", "$"),
        (lambda p: p.with_name("0001-approval.yaml").write_bytes(b"x\n"), "0001-approval.yaml", "$"),
        (_newline_fingerprint, "0001-plan.yaml", "$.fingerprint"),
        (lambda p: p.with_name("0001-contract.yaml").write_bytes(
            p.read_bytes().replace(b"flow: plan", b"flow: contract")), "0001-plan.yaml", "$.seq"),
    ],
)
def test_corrupt_record_names_the_file(tmp_path, corrupt, name, error_path):
    write(tmp_path)
    corrupt(tmp_path / DIR / "0001-plan.yaml")
    with pytest.raises(InvalidRecord) as caught:
        read_records(tmp_path, FEATURE)
    assert caught.value.file == f".archshell/records/{FEATURE}/{name}"
    assert caught.value.errors[0][0] == error_path
    assert str(tmp_path) not in str(caught.value)


# --- acceptance criteria -----------------------------------------------------


def test_plan_written_twice_both_read_back(tmp_path):
    write(tmp_path)
    before = (tmp_path / DIR / "0001-plan.yaml").read_bytes()
    write(tmp_path)
    assert sorted(p.name for p in (tmp_path / DIR).iterdir()) == ["0001-plan.yaml", "0002-plan.yaml"]
    assert (tmp_path / DIR / "0001-plan.yaml").read_bytes() == before
    assert len(read_records(tmp_path, FEATURE)) == 2


# --- edges -------------------------------------------------------------------


def test_bootstrap_is_a_valid_slug(tmp_path):
    assert write(tmp_path, feature="bootstrap").path == ".archshell/records/bootstrap/0001-plan.yaml"


def test_held_claim_and_existing_seq_are_skipped(tmp_path):
    (tmp_path / ".archshell" / "locks" / "records" / FEATURE).mkdir(parents=True)
    claim = tmp_path / ".archshell" / "locks" / "records" / FEATURE / "0001.claim"
    claim.touch()  # a writer crashed holding seq 1
    assert write(tmp_path).path.endswith("/0002-plan.yaml")
    assert claim.exists()  # another writer's claim is never removed


def test_existing_target_is_never_replaced(tmp_path, monkeypatch):
    write(tmp_path)
    # Hide existing seqs from the scan so the writer aims at 0001-plan.yaml again.
    import archshell.adapters.records as module

    real = module._used_seqs
    hidden = [2]  # the initial scan and the post-claim check see no records

    def scan(directory):
        if hidden[0]:
            hidden[0] -= 1
            return set()
        return real(directory)

    monkeypatch.setattr(module, "_used_seqs", scan)
    before = (tmp_path / DIR / "0001-plan.yaml").read_bytes()
    assert write(tmp_path, content={"other": "x"}).path.endswith("/0002-plan.yaml")
    assert (tmp_path / DIR / "0001-plan.yaml").read_bytes() == before


def test_fallback_without_hard_links(tmp_path, monkeypatch):
    def no_link(*args):
        raise OSError("hard links unsupported")

    monkeypatch.setattr("os.link", no_link)
    write(tmp_path)
    write(tmp_path)
    assert len(read_records(tmp_path, FEATURE)) == 2
    assert list(files(tmp_path)) == [f"records/{FEATURE}/0001-plan.yaml", f"records/{FEATURE}/0002-plan.yaml"]


def test_fingerprint_given_must_match_content(tmp_path):
    good = adapter_records.fingerprint(CONTENT)
    assert write(tmp_path, fingerprint=good).document["fingerprint"] == good
    record = write(tmp_path, content=None, fingerprint=good, derived=True)
    assert "content" not in record.document and record.document["derived"] is True


def test_fingerprint_is_of_canonical_bytes():
    assert adapter_records.fingerprint({"b": 1, "a": "x"}) == adapter_records.fingerprint({"a": "x", "b": 1})
    assert adapter_records.fingerprint("x") == "sha256:" + __import__("hashlib").sha256(b"x\n").hexdigest()


def test_default_timestamp_is_utc_now(tmp_path):
    stamp = append_record(tmp_path, feature=FEATURE, flow="plan", confirmed_by="ana", content=CONTENT)
    assert records.semantic_errors(stamp.document) == []
    assert stamp.document["confirmed_at"].endswith("Z")


def test_read_unknown_feature_is_empty_and_bad_slug_refused(tmp_path):
    assert read_records(tmp_path, FEATURE) == []
    with pytest.raises(InvalidRecord):
        read_records(tmp_path, "../x")


def test_schema_flows_match_the_vocabulary():
    schema = json.loads((Path(records.__file__).parents[1] / "schemas" / "record-v1.json").read_text("utf-8"))
    assert tuple(schema["properties"]["flow"]["enum"]) == layout.RECORD_FLOWS
    assert schema["$id"] == layout.RECORD_SCHEMA
