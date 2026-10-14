#!/usr/bin/env python3
"""Runner for the `sql` leg (Leg 01) of product-ab-funnel-analytics.

Reads `sql/01_sql.sql`, splits it into the 5 documented queries (marked by
`-- === QUERY <n>: <name> ===`), executes each against the raw CSVs with DuckDB,
prints the result, and exports it to `sql/results/01_<n>_<name>.csv`.

Reproduce:
    uv run --project "<env>" python sql/run_01_sql.py

The script chdir's into the repository root, so the relative `read_csv_auto`
paths inside the .sql file resolve to `data/raw/*.csv`.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = REPO_ROOT / "sql" / "01_sql.sql"
RESULTS_DIR = REPO_ROOT / "sql" / "results"

QUERY_HEADER = re.compile(r"^-- === QUERY (\d+): (\S+) ===$", re.MULTILINE)


def split_queries(sql_text: str) -> list[tuple[int, str, str]]:
    """Return [(num, name, sql_body), ...] in file order."""
    matches = list(QUERY_HEADER.finditer(sql_text))
    if not matches:
        raise SystemExit(f"no '-- === QUERY n: name ===' markers found in {SQL_FILE}")
    out: list[tuple[int, str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(sql_text)
        body = sql_text[start:end].strip().rstrip(";").strip()
        out.append((int(m.group(1)), m.group(2), body))
    return out


def main() -> int:
    os.chdir(REPO_ROOT)  # so relative read_csv_auto('data/raw/*.csv') paths resolve
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    queries = split_queries(SQL_FILE.read_text(encoding="utf-8"))
    print(f"Executing {len(queries)} queries from {SQL_FILE.relative_to(REPO_ROOT)}\n")

    for num, name, body in queries:
        df = con.execute(body).fetch_df()
        out_path = RESULTS_DIR / f"01_{num}_{name}.csv"
        df.to_csv(out_path, index=False, float_format="%.2f")
        print(
            f"--- Q{num} {name}  ({len(df)} rows) -> {out_path.relative_to(REPO_ROOT)}"
        )
        print(df.to_string(index=False))
        print()

    con.close()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
