"""Regression checks for the shared UI shell and issue #28 layout cleanup."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Issue28UIFoundationTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_main_pages_use_the_same_header_partial(self):
        for name in ("index.html", "decks.html", "deck_view.html", "vending.html",
                     "admin.html", "analytics.html", "preferences.html", "search_guide.html",
                     "server_info.html"):
            with self.subTest(template=name):
                self.assertIn("{% include '_app_header.html' %}", self.read(f"templates/{name}"))

    def test_header_has_consistent_navigation_and_active_page_support(self):
        header = self.read("templates/_app_header.html")
        for endpoint in ("index", "decks_page", "vending_view", "analytics_page",
                         "search_guide", "preferences_page", "admin_page", "server_info_page"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(f"url_for('{endpoint}')", header)
        self.assertIn('aria-current="page"', header)
        self.assertIn("url_for('logout')", header)

    def test_shared_header_is_sticky_and_server_info_is_admin_only_navigation(self):
        header = self.read("templates/_app_header.html")
        css = self.read("static/app.css")
        self.assertIn("url_for('server_info_page')", header)
        self.assertIn("position: sticky", css)
        self.assertIn("top: 0", css)
        self.assertIn("@app.get(\"/admin/server-info\")", self.read("app.py"))

    def test_inventory_grid_is_artwork_first_with_per_card_price(self):
        source = self.read("templates/index.html")
        self.assertIn('class="card-art-trigger"', source)
        self.assertIn('class="tile-price-badge"', source)
        self.assertIn("item.calculated_unit_price", source)
        self.assertIn("cardDetailPriceHistoryChart", source)
        self.assertIn('id="oracleTcgplayerLink"', source)
        self.assertNotIn('<div class="tile-body">', source)
        self.assertNotIn('class="meta-row"', source)

    def test_inventory_transfer_actions_are_grouped_inside_the_control_panel(self):
        source = self.read("templates/index.html")
        panel_start = source.index('class="inventory-control-panel"')
        actions_start = source.index('class="page-actions-bar inventory-page-actions"')
        toolbar_start = source.index('class="toolbar"')
        self.assertLess(panel_start, actions_start)
        self.assertLess(actions_start, toolbar_start)
        self.assertIn('class="inventory-transfer-menu"', source[panel_start:toolbar_start])
        self.assertIn("Import / Export", source[panel_start:toolbar_start])
        self.assertIn("Export ManaBox CSV", source[panel_start:toolbar_start])

    def test_grid_and_list_views_are_mutually_exclusive(self):
        source = self.read("templates/index.html")
        self.assertIn(
            '    </section>\n    {% else %}\n    <section class="table-card">',
            source,
        )
        self.assertEqual(source.count("{% if view == 'grid' %}"), 3)

    def test_superbar_is_open_on_desktop_and_collapses_on_small_screens(self):
        header = self.read("templates/_app_header.html")
        self.assertIn('id="siteNavMenu" open', header)
        self.assertIn("smallScreen.matches", header)
        for label in ("Inventory", "Decks", "Vending", "Collection analytics", "Search guide", "Administration", "Server info"):
            with self.subTest(label=label):
                self.assertIn(f">{label}</a>", header)

    def test_grid_sizing_card_details_mode_and_image_zoom_are_available(self):
        source = self.read("templates/index.html")
        css = self.read("static/app.css")
        self.assertIn('id="gridCardSize"', source)
        self.assertIn("cardscanner-grid-card-size", source)
        self.assertIn('id="cardDetailMode"', source)
        self.assertIn('option value="sidebar"', source)
        self.assertIn('option value="popup"', source)
        self.assertIn("openImage(activeOracleImageUrl", source)
        self.assertIn('data-display-mode="sidebar"', css)
        self.assertIn('data-display-mode="popup"', css)

    def test_card_details_are_docked_and_inventory_reflows_without_modal_backdrop(self):
        source = self.read("templates/index.html")
        css = self.read("static/app.css")
        self.assertIn('<aside id="oracleDialog"', source)
        self.assertIn('id="inventoryResultsLayout"', source)
        self.assertIn('id="cardDetailBackdrop"', source)
        self.assertIn("function closeOraclePanel()", source)
        self.assertNotIn("oracleDialog.showModal()", source)
        self.assertIn(".inventory-results-layout.has-card-details", css)
        self.assertIn('data-display-mode="sidebar"', css)

    def test_card_details_support_face_switching_rotation_and_zoom(self):
        source = self.read("templates/index.html")
        card_info = self.read("postgres_card_info.py")
        self.assertIn('id="oracleFaceControls"', source)
        self.assertIn("setOracleFace(index)", source)
        self.assertIn("oracleRotateRight", source)
        self.assertIn("openImage(activeOracleImageUrl", source)
        self.assertIn('"faces": faces', card_info)
        self.assertIn("face_image.image_uri", card_info)

    def test_inventory_price_trends_use_last_two_saved_prices(self):
        app = self.read("app.py")
        pg_history = self.read("postgres_price_history.py")
        inventory = self.read("templates/index.html")
        self.assertIn("def _calculate_price_trend", app)
        self.assertIn("ROW_NUMBER()", app)
        self.assertIn("fetch_latest_price_trends", pg_history)
        self.assertIn("price-trend-indicator", inventory)
        self.assertIn("trend-up", self.read("static/app.css"))
        self.assertIn("trend-down", self.read("static/app.css"))
        self.assertIn("trend-flat", self.read("static/app.css"))

    def test_server_status_is_separated_from_inventory(self):
        inventory = self.read("templates/index.html")
        server_info = self.read("templates/server_info.html")
        self.assertNotIn("Last price sync:", inventory)
        self.assertNotIn("Scanner API:", inventory)
        self.assertIn("Last price sync", server_info)
        self.assertIn("Scanner API", server_info)

    def test_self_service_password_change_is_in_preferences(self):
        preferences = self.read("templates/preferences.html")
        admin = self.read("templates/admin.html")
        app = self.read("app.py")
        self.assertIn("url_for('change_password')", preferences)
        self.assertNotIn("Admin password", admin)
        self.assertIn('@app.post("/account/password")', app)
        self.assertIn('@login_required\ndef change_password', app)

    def test_theme_selector_is_only_rendered_on_preferences_page(self):
        script = self.read("static/theme.js")
        self.assertIn("data-theme-select", script)
        self.assertNotIn("querySelector('.topbar')", script)
        self.assertNotIn("insertBefore", script)
        for name in ("index.html", "decks.html", "deck_view.html", "vending.html", "admin.html"):
            with self.subTest(template=name):
                self.assertNotIn("data-theme-select", self.read(f"templates/{name}"))
        self.assertIn("data-theme-select", self.read("templates/preferences.html"))

    def test_inventory_summary_values_move_to_analytics(self):
        source = self.read("templates/index.html")
        self.assertNotIn("mobile-summary-menu", source)
        self.assertNotIn("owner_totals.items()", source)
        self.assertNotIn("Collection analytics", source)
        self.assertIn("url_for('analytics_page')", self.read("templates/_app_header.html"))

    def test_inventory_owns_the_manual_add_workflow(self):
        inventory = self.read("templates/index.html")
        admin = self.read("templates/admin.html")
        self.assertIn('id="openManualAdd"', inventory)
        self.assertIn('id="manualAddDialog"', inventory)
        self.assertIn("url_for('manual_add')", inventory)
        self.assertNotIn("Add Card Manually", admin)
        self.assertIn("Catalog search aliases", admin)

    def test_deck_import_form_is_collapsed_above_full_width_deck_list(self):
        source = self.read("templates/decks.html")
        self.assertIn('<details class="deck-import-disclosure admin-panel">', source)
        self.assertIn('<section class="admin-panel stored-decks-panel">', source)
        self.assertIn("deck-dashboard-grid", source)

    def test_search_guide_documents_supported_local_operators(self):
        guide = self.read("templates/search_guide.html")
        for operator in ("set:", "cn:", "name:", "t:", "o:", "c:g", "r&gt;=", "is:foil",
                         "owner:", "loc:", "deck:", "vending:", "art:", "otag:",
                         "order:", "direction:"):
            with self.subTest(operator=operator):
                self.assertIn(operator, guide)
        self.assertIn("not the full Scryfall search engine", guide)

    def test_search_guide_examples_are_generic(self):
        guide = self.read("templates/search_guide.html")
        self.assertIn("Sample Collector", guide)
        self.assertIn("Sample Deck", guide)

    def test_analytics_only_renders_the_signed_in_users_personal_total(self):
        route = self.read("app.py")
        template = self.read("templates/analytics.html")
        self.assertIn("owner_totals.get(person_name)", route)
        self.assertNotIn("owner_totals.items()", template)
        self.assertIn("Historical snapshots are not available yet", template)


if __name__ == "__main__":
    unittest.main()
