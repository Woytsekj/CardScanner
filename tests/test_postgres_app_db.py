"""Tests for the staged PostgreSQL application connection adapter."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from postgres_app_db import (
    HybridRow,
    PostgresAppCursor,
    connect_app_database,
    hybrid_row_factory,
    translate_qmark_sql,
)


class HybridRowTests(unittest.TestCase):
    def test_supports_named_and_positional_access(self):
        row = HybridRow(("id", "name", "active"), (17, "Fixture", True))

        self.assertEqual(row["id"], 17)
        self.assertEqual(row[0], 17)
        self.assertEqual(row["name"], "Fixture")
        self.assertEqual(row[1], "Fixture")
        self.assertIs(row["active"], True)
        self.assertEqual(dict(row), {
            "id": 17,
            "name": "Fixture",
            "active": True,
        })

    def test_row_factory_handles_result_columns(self):
        cursor = SimpleNamespace(description=[
            SimpleNamespace(name="id"),
            SimpleNamespace(name="label"),
        ])

        make_row = hybrid_row_factory(cursor)
        row = make_row((23, "Test"))

        self.assertEqual(row["id"], 23)
        self.assertEqual(row[0], 23)
        self.assertEqual(row["label"], "Test")
        self.assertEqual(row[1], "Test")

    def test_row_factory_handles_statements_without_result_columns(self):
        cursor = SimpleNamespace(description=None)
        self.assertIsNone(hybrid_row_factory(cursor))


class CursorAdapterTests(unittest.TestCase):
    def test_cursor_adapter_preserves_context_manager_for_copy_helpers(self):
        class FakeCursor:
            def __init__(self):
                self.entered = False
                self.exited = False

            def __enter__(self):
                self.entered = True
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.exited = True
                return False

        raw = FakeCursor()
        adapter = PostgresAppCursor(raw)
        with adapter as cursor:
            self.assertIs(cursor, adapter)
            self.assertTrue(raw.entered)
        self.assertTrue(raw.exited)


class CursorAdapterTests(unittest.TestCase):
    def test_cursor_adapter_preserves_context_manager_for_copy_helpers(self):
        class FakeCursor:
            def __init__(self):
                self.entered = False
                self.exited = False

            def __enter__(self):
                self.entered = True
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.exited = True
                return False

        raw = FakeCursor()
        adapter = PostgresAppCursor(raw)
        with adapter as cursor:
            self.assertIs(cursor, adapter)
            self.assertTrue(raw.entered)
        self.assertTrue(raw.exited)


class QmarkTranslationTests(unittest.TestCase):
    def test_translates_placeholders_but_preserves_literals_and_comments(self):
        source = (
            "SELECT ?, '?' AS literal, \"?\" AS identifier "
            "-- ? stays in this comment\n"
            "WHERE id = ? /* ? stays in this comment */"
        )
        expected = (
            "SELECT %s, '?' AS literal, \"?\" AS identifier "
            "-- ? stays in this comment\n"
            "WHERE id = %s /* ? stays in this comment */"
        )

        self.assertEqual(translate_qmark_sql(source), expected)

    def test_preserves_dollar_quoted_bodies(self):
        source = "SELECT $tag$? stays here$tag$, ?"
        expected = "SELECT $tag$? stays here$tag$, %s"

        self.assertEqual(translate_qmark_sql(source), expected)

    def test_handles_escaped_single_quotes(self):
        source = "SELECT 'it''s ?' AS label WHERE id = ?"
        expected = "SELECT 'it''s ?' AS label WHERE id = %s"

        self.assertEqual(translate_qmark_sql(source), expected)


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_POSTGRES_DSN"),
    "CARD_SCANNER_POSTGRES_DSN is not configured",
)
class PostgreSQLApplicationConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from postgres_db import open_pool

        open_pool()

    @classmethod
    def tearDownClass(cls):
        from postgres_db import close_pool

        close_pool()

    def test_development_connection_search_path_rows_and_placeholders(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            row = conn.execute("""
                SELECT current_database() AS database,
                       current_schema() AS schema,
                       to_regclass('inventory') IS NOT NULL AS inventory_visible,
                       to_regclass('catalog.printings') IS NOT NULL AS printings_visible
            """).fetchone()

            self.assertEqual(row["database"], "cardscanner_dev")
            self.assertEqual(row[0], "cardscanner_dev")
            self.assertEqual(row["schema"], "app")
            self.assertTrue(row["inventory_visible"])
            self.assertTrue(row["printings_visible"])

            value = conn.execute(
                "SELECT ?::integer AS value",
                (73,),
            ).fetchone()

            self.assertEqual(value["value"], 73)
            self.assertEqual(value[0], 73)
        finally:
            conn.close()

    def test_named_server_cursor_streams_result_rows(self):
        conn = connect_app_database(expected_database="cardscanner_dev")
        try:
            cursor = conn.stream(
                "SELECT value FROM generate_series(1, 5) AS value",
                name="cardscanner_stream_test_rows",
            )
            try:
                self.assertEqual(
                    [row["value"] for row in cursor.fetchmany(2)],
                    [1, 2],
                )
                self.assertEqual(
                    [row["value"] for row in cursor.fetchmany(10)],
                    [3, 4, 5],
                )
                self.assertEqual(cursor.fetchmany(1), [])
            finally:
                cursor.close()
        finally:
            conn.close()

    def test_connection_restores_shared_pool_state(self):
        from postgres_db import connection

        with connection() as raw:
            baseline = raw.execute(
                "SELECT current_schema() AS schema"
            ).fetchone()
            baseline_schema = baseline["schema"]
            self.assertIs(type(baseline), dict)

        app_conn = connect_app_database(
            expected_database="cardscanner_dev"
        )
        try:
            row = app_conn.execute(
                "SELECT current_schema() AS schema"
            ).fetchone()
            self.assertEqual(row["schema"], "app")
            self.assertEqual(row[0], "app")
        finally:
            app_conn.close()

        with connection() as raw:
            restored = raw.execute(
                "SELECT current_schema() AS schema"
            ).fetchone()

            self.assertEqual(restored["schema"], baseline_schema)
            self.assertIs(type(restored), dict)

    def test_rejects_wrong_database_name(self):
        with self.assertRaisesRegex(RuntimeError, "safety check failed"):
            connect_app_database(expected_database="not_cardscanner_dev")


if __name__ == "__main__":
    unittest.main()
