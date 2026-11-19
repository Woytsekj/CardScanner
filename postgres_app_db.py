"""Psycopg connection adapter for the staged PostgreSQL application migration.

This module is intentionally separate from app.py. It handles driver-level
compatibility only; SQLite-specific SQL and legacy table references must be
converted explicitly before application routes use PostgreSQL.
"""
from __future__ import annotations

import os
import re
from typing import Any

from psycopg.pq import TransactionStatus

from postgres_db import get_pool


class HybridRow(dict):
    """Dictionary row that also supports SQLite-style integer indexing."""

    def __init__(self, column_names, values):
        ordered = tuple(values)
        super().__init__(zip(column_names, ordered))
        self._ordered_values = ordered

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._ordered_values[key]
        return super().__getitem__(key)


def hybrid_row_factory(cursor):
    """Psycopg row factory supporting both row['column'] and row[0]."""
    if cursor.description is None:
        return None

    column_names = tuple(column.name for column in cursor.description)

    def make_row(values):
        return HybridRow(column_names, values)

    return make_row


_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def translate_qmark_sql(query: str) -> str:
    """Translate SQLite qmark parameters outside SQL literals/comments.

    This does not translate SQLite SQL syntax or PostgreSQL JSON '?' operators.
    """
    if not isinstance(query, str):
        raise TypeError("SQL query must be a string")

    output: list[str] = []
    index = 0
    length = len(query)
    state = "normal"
    block_depth = 0

    while index < length:
        char = query[index]
        following = query[index + 1] if index + 1 < length else ""

        if state == "single":
            if char == "\\" and following:
                output.append(char + following)
                index += 2
                continue
            output.append(char)
            if char == "'":
                if following == "'":
                    output.append(following)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue

        if state == "double":
            output.append(char)
            if char == '"':
                if following == '"':
                    output.append(following)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue

        if state == "line_comment":
            output.append(char)
            if char == "\n":
                state = "normal"
            index += 1
            continue

        if state == "block_comment":
            if char == "/" and following == "*":
                output.append("/*")
                block_depth += 1
                index += 2
            elif char == "*" and following == "/":
                output.append("*/")
                block_depth -= 1
                index += 2
                if block_depth == 0:
                    state = "normal"
            else:
                output.append(char)
                index += 1
            continue

        # Normal SQL state.
        if char == "-" and following == "-":
            output.append("--")
            state = "line_comment"
            index += 2
        elif char == "/" and following == "*":
            output.append("/*")
            state = "block_comment"
            block_depth = 1
            index += 2
        elif char == "'":
            output.append(char)
            state = "single"
            index += 1
        elif char == '"':
            output.append(char)
            state = "double"
            index += 1
        elif char == "$":
            match = _DOLLAR_QUOTE.match(query, index)
            if match:
                delimiter = match.group(0)
                end = query.find(delimiter, match.end())
                if end == -1:
                    output.append(query[index:])
                    break
                end += len(delimiter)
                output.append(query[index:end])
                index = end
            else:
                output.append(char)
                index += 1
        elif char == "?":
            output.append("%s")
            index += 1
        else:
            output.append(char)
            index += 1

    return "".join(output)


class PostgresAppCursor:
    """Small cursor facade that translates qmark placeholders."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        self._cursor.execute(translate_qmark_sql(query), params)
        return self

    def executemany(self, query, params_seq):
        self._cursor.executemany(translate_qmark_sql(query), params_seq)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)


class PostgresAppConnection:
    """Pooled connection facade with explicit close/return semantics."""

    def __init__(self, raw_connection, pool):
        self._raw = raw_connection
        self._pool = pool
        self._closed = False

    def execute(self, query, params=None):
        cursor = self._raw.cursor(row_factory=hybrid_row_factory)
        cursor.execute(translate_qmark_sql(query), params)
        return PostgresAppCursor(cursor)

    def executemany(self, query, params_seq):
        cursor = self._raw.cursor(row_factory=hybrid_row_factory)
        return PostgresAppCursor(cursor).executemany(query, params_seq)

    def cursor(self):
        return PostgresAppCursor(
            self._raw.cursor(row_factory=hybrid_row_factory)
        )

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        """Rollback any unfinished transaction and return the connection."""
        if self._closed:
            return

        try:
            if self._raw.info.transaction_status != TransactionStatus.IDLE:
                self._raw.rollback()

            # search_path is session-scoped, so restore the pooled connection
            # before another module borrows it.
            self._raw.execute("RESET search_path")
            self._raw.commit()
        except BaseException:
            # Never return a connection whose session state could not be reset.
            self._raw.close()
            raise
        finally:
            self._pool.putconn(self._raw)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # Match sqlite3.Connection's context-manager behavior: transaction
        # handling occurs here, but the connection is not closed.
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __getattr__(self, name):
        return getattr(self._raw, name)


def connect_app_database(expected_database: str | None = None) -> PostgresAppConnection:
    """Borrow a PostgreSQL connection after validating its database identity.

    The default expected database is cardscanner_dev. A different database
    requires an explicit CARD_SCANNER_POSTGRES_EXPECTED_DATABASE setting.
    """
    pool = get_pool()
    raw = pool.getconn()

    try:
        # HybridRow is selected per adapter cursor. Do not mutate the shared
        # pool connection's configured row factory.
        actual_database = raw.execute(
            "SELECT current_database() AS database"
        ).fetchone()["database"]

        expected = (
            expected_database
            or os.environ.get(
                "CARD_SCANNER_POSTGRES_EXPECTED_DATABASE",
                "cardscanner_dev",
            ).strip()
        )

        if not expected:
            raise RuntimeError("Expected PostgreSQL database name is not configured")

        if actual_database != expected:
            raise RuntimeError(
                "PostgreSQL database safety check failed: "
                f"expected {expected!r}, connected to {actual_database!r}"
            )

        raw.execute("SET search_path TO app, catalog, public")
        raw.commit()

        return PostgresAppConnection(raw, pool)

    except BaseException:
        try:
            raw.rollback()
        finally:
            pool.putconn(raw)
        raise
