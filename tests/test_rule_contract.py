from __future__ import annotations

import sqlite3

import pytest

import procdiag.diagnosis  # noqa: F401
from procdiag.core import all_rules
from procdiag.storage import Storage

_EVENT_TABLES = {"spike_events", "ops_events", "service_events", "kernel_events"}
_BUILTIN = sorted(
    (cls for cls in all_rules().values() if cls.__module__.startswith("procdiag.")),
    key=lambda c: c.rule_id,
)


@pytest.fixture(scope="module")
def schema(tmp_path_factory) -> dict[str, set[str]]:
    path = tmp_path_factory.mktemp("schema") / "current.db"
    Storage(path).close()
    conn = sqlite3.connect(path)
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    cols = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}
    conn.close()
    return cols


def test_all_twenty_builtin_rules_are_checked():
    assert len(_BUILTIN) == 20


@pytest.mark.parametrize("cls", _BUILTIN, ids=lambda c: c.rule_id)
def test_builtin_rule_declares_its_inputs_in_its_own_body(cls):
    declared = {"requires_schema", "requires_rows"} & set(vars(cls))
    assert declared, f"{cls.rule_id} declares neither requires_schema nor requires_rows"


@pytest.mark.parametrize("cls", _BUILTIN, ids=lambda c: c.rule_id)
def test_declared_inputs_exist_in_the_current_schema(cls, schema):
    for name in (*cls.requires_schema, *cls.requires_rows):
        table, _, column = name.partition(".")
        assert table in schema, f"{cls.rule_id}: unknown table {table}"
        if column:
            assert column in schema[table], f"{cls.rule_id}: unknown column {name}"
    tables = {n for n in cls.requires_rows if "." not in n}
    assert all(n.partition(".")[0] in tables for n in cls.requires_rows)


@pytest.mark.parametrize("cls", _BUILTIN, ids=lambda c: c.rule_id)
def test_only_sampled_tables_gate_on_rows(cls, schema):
    tables = {n.partition(".")[0] for n in cls.requires_rows}
    assert not tables & _EVENT_TABLES
    for table in tables:
        assert {"ts", "ts_min"} & schema[table], f"{table} has no time column"
