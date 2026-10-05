"""DuckDB persistence: append-only writes buffered in memory and flushed in batches."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pandas as pd

SQL_DIR = Path(__file__).parent / "sql"


def _cell(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, (Decimal, date)):
        return str(v)
    return v


class Store:
    def __init__(self, path: str | Path | None = None):
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if Path(path).exists():
                Path(path).unlink()
        self.con = duckdb.connect(str(path) if path else ":memory:")
        self.con.execute("SET TimeZone = 'UTC'")
        self.con.execute((SQL_DIR / "schema.sql").read_text())
        self.con.execute((SQL_DIR / "views.sql").read_text())
        self._pending: dict[str, list[dict]] = defaultdict(list)

    def add(self, table: str, **row) -> None:
        self._pending[table].append(row)

    def flush(self, tables: tuple[str, ...] | None = None) -> None:
        for table, rows in self._pending.items():
            if not rows or (tables is not None and table not in tables):
                continue
            df = pd.DataFrame([{k: _cell(v) for k, v in r.items()} for r in rows])
            self.con.register("_batch", df)
            self.con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _batch")
            self.con.unregister("_batch")
            rows.clear()

    def replace_table(self, table: str, rows: list[dict]) -> None:
        self.flush()
        self.con.execute(f"DELETE FROM {table}")
        for r in rows:
            self.add(table, **r)
        self.flush()

    def set_clock(self, now: datetime) -> None:
        self.con.execute("DELETE FROM sim_clock")
        self.con.execute("INSERT INTO sim_clock VALUES (?)", [_cell(now)])

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        self.flush()
        return self.con.execute(sql, params or []).df()

    def rows(self, sql: str, params: list | None = None, tables: tuple[str, ...] | None = None) -> list[tuple]:
        """Run a query after flushing pending writes (only ``tables`` if given)."""
        self.flush(tables)
        return self.con.execute(sql, params or []).fetchall()

    def close(self) -> None:
        self.flush()
        self.con.close()
