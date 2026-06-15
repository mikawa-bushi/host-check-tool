"""CSV and JSON export for procdiag time-series data.

Provides a single entry-point function ``export_rows`` that reads a rollup
or raw-sample table from a ``Storage`` instance and writes it to an
``IO[str]`` sink.  The caller (``cli.py``) handles opening files or using
``sys.stdout``; this module stays IO-agnostic.

Supported output formats:

- ``csv``  — RFC 4180, header row with column names, one data row per sample.
- ``json`` — JSON array of objects (one object per row), streamed as a single
  ``json.dumps`` call so the result is always valid JSON.

Column names come from ``storage.TABLE_COLUMNS[table]`` so the export
contract is always in sync with the DDL without duplication.
"""

from __future__ import annotations

import csv
import json
from typing import IO, Any

from .storage import TABLE_COLUMNS, Storage


def export_rows(
    store: Storage,
    *,
    table: str = "sys_rollup_1m",
    fmt: str = "csv",
    out: IO[str],
) -> int:
    """Export *table* from *store* to *out* in *fmt* format.

    Parameters
    ----------
    store:
        Open ``Storage`` instance (read_only is fine).
    table:
        Table name.  Must be a key in ``storage.TABLE_COLUMNS``.
        Pass ``"sys_samples"`` to export raw T1 samples.
    fmt:
        ``"csv"`` (default) or ``"json"``.
    out:
        Writable text stream; the caller manages opening/closing.

    Returns
    -------
    int
        Number of data rows written (header not counted for CSV).

    Raises
    ------
    ValueError
        When *table* is not recognised or *fmt* is unsupported.
    """
    columns = TABLE_COLUMNS.get(table)
    if columns is None:
        raise ValueError(
            f"不明なテーブルです: '{table}'  (利用可能: {list(TABLE_COLUMNS.keys())})"
        )

    if fmt not in ("csv", "json"):
        raise ValueError(f"不明なフォーマットです: '{fmt}'  (利用可能: csv / json)")

    # Fetch all rows from the table using the Storage.rows() read-path.
    raw_rows = store.rows(table)

    if fmt == "csv":
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(columns)
        count = 0
        for row in raw_rows:
            writer.writerow([row[col] for col in columns])
            count += 1
        return count

    # JSON: build a list of dicts, then dump atomically.
    records: list[dict[str, Any]] = []
    for row in raw_rows:
        records.append({col: row[col] for col in columns})
    out.write(json.dumps(records, ensure_ascii=False, indent=2))
    out.write("\n")
    return len(records)
