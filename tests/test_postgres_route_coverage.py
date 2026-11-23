"""Guard the PostgreSQL route allowlist against accidental omissions."""

from __future__ import annotations

import os
import unittest


@unittest.skipUnless(
    os.environ.get("CARD_SCANNER_DATABASE_BACKEND") == "postgres",
    "Run with CARD_SCANNER_DATABASE_BACKEND=postgres",
)
class PostgreSQLRouteCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module

        if app_module.DATABASE_BACKEND != "postgres":
            raise RuntimeError("Route coverage test requires PostgreSQL app mode")
        cls.app_module = app_module

    def test_every_registered_route_has_an_explicit_postgres_method_policy(self):
        get_endpoints = {
            "login",
            "admin_page",
            "preferences_page",
            "server_info_page",
            "analytics_page",
            "search_guide",
            "catalog_search",
            "catalog_suggest",
            "search_suggest",
            "list_aliases",
            "index",
            "export_csv",
            "export_manabox_csv",
            "api_people",
            "api_scanner_options",
            "api_scanner_client_data_manifest",
            "api_scanner_client_data_download",
            "scanner_desktop_download",
            "scanner_mobile_index",
            "scanner_mobile_asset",
            "api_scanner_mobile_options",
            "api_scanner_mobile_search",
            "api_scanner_mobile_price",
            "api_health",
            "api_version",
            "api_price_by_card",
            "api_price",
            "api_price_history",
            "decks_page",
            "deck_cover_file",
            "deck_view",
            "deck_missing_csv",
            "api_card_rulings",
            "api_card_info",
            "vending_export_csv",
            "vending_view",
            "vending_labels",
        }
        post_endpoints = {
            "login_post",
            "logout",
            "add_alias",
            "manual_add",
            "add_person",
            "rename_person",
            "toggle_person",
            "delete_person",
            "add_user",
            "assign_user_person",
            "toggle_user",
            "admin_reset_user_password",
            "rename_user",
            "change_user_role",
            "change_user_manager",
            "delete_user",
            "set_collection_visibility",
            "admin_change_password_legacy",
            "change_password",
            "set_counts",
            "update_inventory",
            "bulk_update_inventory",
            "move_quantity",
            "import_csv",
            "api_scanner_mobile_recognize",
            "api_scanner_mobile_scan",
            "api_scan",
            "api_scan_batch",
            "test_discord",
            "decks_import",
            "update_deck_settings",
            "update_deck_cover",
            "edit_deck_card",
            "add_deck_card",
            "delete_deck_card",
            "assign_free_to_deck",
            "move_inventory_to_deck",
            "admin_refresh_scryfall_bulk",
            "enrich_metadata",
            "delete_deck",
            "refresh_prices",
        }
        delete_endpoints = {"delete_item", "delete_alias"}
        expected = {
            **{name: {"GET"} for name in get_endpoints},
            **{name: {"POST"} for name in post_endpoints},
            **{name: {"DELETE"} for name in delete_endpoints},
        }

        registered = {}
        for rule in self.app_module.app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            methods = set(rule.methods or ()) - {"HEAD", "OPTIONS"}
            registered.setdefault(rule.endpoint, set()).update(methods)

        self.assertEqual(
            registered,
            expected,
            "A route was added, removed, or changed methods without updating "
            "the PostgreSQL security-gate policy and its coverage test.",
        )

    def test_postgres_mode_refuses_the_legacy_inventory_sqlite_connection(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "Refusing to fall back to SQLite",
        ):
            self.app_module.connect_inventory()


if __name__ == "__main__":
    unittest.main()
