"""Append-only raw-response log: SQLite offline, PostgreSQL via explicit DSN."""

import hashlib
import json
import sqlite3
from datetime import datetime


class Journal:
    def __init__(self, location: str):
        self.postgres = location.startswith(("postgresql://", "postgres://"))
        if self.postgres:
            import psycopg

            self.connection = psycopg.connect(location)
        else:
            self.connection = sqlite3.connect(location)
        pk = "BIGSERIAL PRIMARY KEY" if self.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        self.connection.execute(f"""CREATE TABLE IF NOT EXISTS raw_quotes (
            id {pk}, source TEXT NOT NULL, received_at TEXT NOT NULL,
            sha256 TEXT NOT NULL, payload TEXT NOT NULL)""")
        self.connection.commit()

    def append(self, source: str, received_at: datetime, payload: object):
        if received_at.tzinfo is None:
            raise ValueError("timezone required")
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        values = (source, received_at.isoformat(), hashlib.sha256(raw.encode()).hexdigest(), raw)
        placeholder = "%s" if self.postgres else "?"
        try:
            self.connection.execute(
                "INSERT INTO raw_quotes(source,received_at,sha256,payload) VALUES ("
                + ",".join([placeholder] * 4)
                + ")",
                values,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def rows(self):
        return self.connection.execute(
            "SELECT source,received_at,sha256,payload FROM raw_quotes ORDER BY id"
        ).fetchall()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
