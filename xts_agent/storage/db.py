"""Minimal SQL layer: SQLite by default, PostgreSQL when given a postgres:// URL.

Callers write portable SQL with ``?`` placeholders and these markers in DDL:
``{pk}`` (auto-increment primary key) and ``{float}`` (double precision).
Upserts use ``INSERT ... ON CONFLICT``, which both engines support (SQLite
>= 3.24; Ubuntu 20.04 ships 3.31). ``insert_id`` hides the lack of
``RETURNING`` in that SQLite. PostgreSQL needs the ``[postgres]`` extra
(psycopg 3); one database can then be shared by every agent host.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_DDL_TYPES = {
    "sqlite": {"pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "float": "REAL"},
    "postgres": {"pk": "BIGSERIAL PRIMARY KEY", "float": "DOUBLE PRECISION"},
}


def is_postgres_url(url: str) -> bool:
    return str(url).startswith(("postgres://", "postgresql://"))


class Database:
    def __init__(self, url: str | Path):
        url = str(url)
        if is_postgres_url(url):
            self.dialect = "postgres"
            self.url = url
        else:
            self.dialect = "sqlite"
            self.url = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
            Path(self.url).parent.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        # Never expose credentials from a postgres URL
        if self.dialect == "postgres":
            return f"Database(postgres://***@{self.url.rsplit('@', 1)[-1]})"
        return f"Database(sqlite:{self.url})"

    def _raw_connect(self):
        if self.dialect == "postgres":
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("PostgreSQL support needs: pip install 'xts-agent[postgres]'") from exc
            return psycopg.connect(self.url)
        return sqlite3.connect(self.url, timeout=30)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    @contextmanager
    def transaction(self) -> Iterator["_Tx"]:
        conn = self._raw_connect()
        try:
            yield _Tx(self, conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ddl(self, script: str) -> None:
        types = _DDL_TYPES[self.dialect]
        with self.transaction() as tx:
            for statement in script.format(**types).split(";"):
                if statement.strip():
                    tx.execute(statement)

    # one-shot helpers
    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.transaction() as tx:
            tx.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple]:
        with self.transaction() as tx:
            return tx.query(sql, params)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Tuple]:
        rows = self.query(sql, params)
        return rows[0] if rows else None


class _Tx:
    def __init__(self, db: Database, conn: Any):
        self.db = db
        self.conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.conn.cursor()
        cur.execute(self.db._sql(sql), tuple(params))
        return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = [tuple(r) for r in rows]
        if rows:
            self.conn.cursor().executemany(self.db._sql(sql), rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple]:
        cur = self.conn.cursor()
        cur.execute(self.db._sql(sql), tuple(params))
        return [tuple(r) for r in cur.fetchall()]

    def insert_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run an INSERT and return the new row's ``id``."""
        cur = self.conn.cursor()
        if self.db.dialect == "postgres":
            cur.execute(self.db._sql(sql) + " RETURNING id", tuple(params))
            return cur.fetchone()[0]
        cur.execute(sql, tuple(params))
        return cur.lastrowid
