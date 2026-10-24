"""Tests for procdiag.spikes — the spike_events JSON payload contract."""

from __future__ import annotations

import json

from procdiag.spikes import (
    culprits,
    decode_top_processes,
    encode_psi_snapshot,
    encode_top_cgroups,
    encode_top_processes,
)


def _proc(proc_id: int, cpu, rss) -> dict:
    return {
        "ts": 1.0,
        "proc_id": proc_id,
        "pid": 100 + proc_id,
        "cpu_pct": cpu,
        "rss": rss,
    }


def test_encode_top_processes_ranks_by_cpu_and_rss():
    data = json.loads(
        encode_top_processes(
            [_proc(1, 5.0, 900), _proc(2, 80.0, 10), _proc(3, None, 50)]
        )
    )
    assert [e["proc_id"] for e in data["by_cpu"]] == [2, 1, 3]  # NULL sorts as 0
    assert [e["proc_id"] for e in data["by_rss"]] == [1, 3, 2]
    assert set(data["by_cpu"][0]) == {"ts", "proc_id", "pid", "cpu_pct", "rss"}


def test_encode_top_processes_keeps_top_ten():
    data = json.loads(encode_top_processes([_proc(i, float(i), i) for i in range(15)]))
    assert len(data["by_cpu"]) == 10
    assert data["by_cpu"][0]["proc_id"] == 14


def test_encode_top_cgroups_and_psi():
    rows = [{"ts": 1.0, "cg_id": i, "mem_current": i * 10} for i in range(8)]
    data = json.loads(encode_top_cgroups(rows))
    assert [e["cg_id"] for e in data["by_mem"]] == [7, 6, 5, 4, 3]
    assert encode_psi_snapshot(None) == "{}"
    assert json.loads(encode_psi_snapshot({"cpu_some10": 1.5})) == {"cpu_some10": 1.5}


def test_decode_round_trips_encoded_payload():
    raw = encode_top_processes([_proc(1, 50.0, 1)])
    assert decode_top_processes(raw)[0]["proc_id"] == 1


def test_decode_tolerates_bad_payloads():
    for raw in (None, "", "not json", "[]", '{"by_cpu": "x"}', '{"by_cpu": [1, "a"]}'):
        assert decode_top_processes(raw) == []


def test_culprits_resolve_names_and_limit(tmp_db):
    hog = tmp_db.proc_id_for("hogd", "h", "hogd", 1.0)
    raw = encode_top_processes(
        [
            _proc(hog, 99.0, 1),
            _proc(998, 5.0, 1),
            _proc(999, 1.0, 1),
            _proc(997, 0.5, 1),
        ]
    )
    out = culprits(raw, tmp_db)
    assert [c["name"] for c in out] == ["hogd", "proc_998", "proc_999"]
    assert out[0]["pid"] == 100 + hog and out[0]["cpu_pct"] == 99.0
    assert len(culprits(raw, tmp_db, limit=1)) == 1
